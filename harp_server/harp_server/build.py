"""
Build phase from the profiling page: pool -> standardize -> build.

1. Pool: the profiling rows of the sweeps you pick for an app (read through TAPIS).
2. Standardize: one table in the format the HARP pipeline's build phase reads
   (run_config, run_type, sys_*, run_<param>, walltime), with the hardware each
   job was given (sys_alloc_cores, sys_alloc_mem_mb), every column filled in
   every row, and enough SD / FS / test_data rows to train on.
3. Build: job_runner/harp_build_runner.py runs the pipeline's own build modules
   (pipeline/modules/pipeline.py: data_preprocessor = outliers, scaling, PCA;
   model_trainer = LR / NN / DTR on SD, SD+25FS, SD+50FS, SD+75FS), either as a
   TAPIS job with the HARP build app (DockerFiles/Dockerfile_HARP_Build), which
   archives its results to the TAPIS folder you chose, or on this computer, after
   which the server copies them there through TAPIS.
"""

import base64
import csv
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid as uuidlib
from datetime import datetime, timezone

from .tapis_gateway import TapisError

RUN_TYPES = ("SD", "FS", "test_data")
# What the pipeline needs: SD to train, FS split in quarters, test_data to score.
MIN_ROWS = {"SD": 2, "FS": 4, "test_data": 1}
# Page / bookkeeping columns that are not features.
NOT_FEATURES = {"campaign", "system", "hardware", "run_config", "run_type", "walltime"}
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNNER = os.path.join(REPO_ROOT, "job_runner", "harp_build_runner.py")
BUILD_TIMEOUT = int(os.environ.get("HARP_BUILD_TIMEOUT", "3600"))
MODELS = {"LR": "Linear regression", "NN": "Neural network", "DTR": "Decision tree"}
TRAINING_SETS = {"SD": "SD only", "SD+25FS": "SD + 25% of FS", "SD+50FS": "SD + 50% of FS", "SD+75FS": "SD + 75% of FS"}
SUMMARY_FILE_NAME = "harp_build_summary.json"
PROGRESS_FILE_NAME = "harp_progress.json"
DONE_JOB = ("FINISHED", "FAILED", "CANCELLED")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_name(label):
    """The pipeline lowercases the app name in some places and not others, so keep it lowercase."""
    out = "".join(ch if ch.isalnum() else "_" for ch in (label or "app").lower()).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out[:40] or "app"


def _number(v):
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    if s.lower() in ("true", "false"):        # the preprocessor cannot take bool columns
        return int(s.lower() == "true")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() and "." not in s and "e" not in s.lower() else f


def standardize(columns, rows):
    """Pooled profiling rows -> (header, table, report) in the pipeline's input format."""
    report = {"rows_in": len(rows), "dropped_rows": {}, "dropped_columns": {}, "constant_columns": [],
              "by_run_type": {t: 0 for t in RUN_TYPES}, "problems": []}

    def drop_row(reason):
        report["dropped_rows"][reason] = report["dropped_rows"].get(reason, 0) + 1

    kept = []
    for r in rows:
        if r.get("run_type") not in RUN_TYPES:
            drop_row("run type is not SD, FS or test_data")
            continue
        w = _number(r.get("walltime", ""))
        if w is None or w <= 0:
            drop_row("no walltime")
            continue
        kept.append(r)

    features = [c for c in columns if c not in NOT_FEATURES and (c.startswith("run_") or c.startswith("sys_"))]
    usable = []
    for c in features:
        missing = sum(1 for r in kept if str(r.get(c, "")).strip() == "")
        if kept and missing:
            # a column only some sweeps have (e.g. a parameter added later): the pipeline cannot use gaps
            report["dropped_columns"][c] = ("not recorded by these sweeps" if missing == len(kept)
                                            else f"missing in {missing} of {len(kept)} rows")
            continue
        usable.append(c)

    table = []
    for r in kept:
        out = {"run_config": r.get("run_config") or "", "run_type": r["run_type"]}
        for c in usable:
            v = r.get(c)
            n = _number(v)
            out[c] = n if n is not None else str(v).strip()
        out["walltime"] = _number(r["walltime"])
        table.append(out)
        report["by_run_type"][r["run_type"]] += 1

    for c in usable:
        if len({row[c] for row in table}) <= 1 and c not in ("sys_name", "sys_processor"):
            report["constant_columns"].append(c)
    report["rows_out"] = len(table)
    report["features"] = [c for c in usable if c not in ("sys_name", "sys_processor")]
    report["varied_features"] = [c for c in report["features"] if c not in report["constant_columns"]]
    report["minimum"] = MIN_ROWS
    for t, n in MIN_ROWS.items():
        if report["by_run_type"][t] < n:
            report["problems"].append(f"needs at least {n} {t} row{'s' if n > 1 else ''}; has {report['by_run_type'][t]}")
    if not report["varied_features"]:
        report["problems"].append("nothing varies between runs, so there is nothing to learn from")
    report["ready"] = not report["problems"]
    header = ["run_config", "run_type"] + usable + ["walltime"]
    return header, table, report


def to_csv(header, table):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=header)
    w.writeheader()
    w.writerows(table)
    return out.getvalue().encode()


class BuildManager:
    """Builds, one record each (JSON, survives restarts). A build runs as a TAPIS job
    (polled by tick()) or on this computer (a background thread, one at a time)."""

    def __init__(self, data_dir, python=None):
        self.dir = os.path.abspath(os.path.join(data_dir, "builds"))   # the pipeline runs from its own folder
        os.makedirs(self.dir, exist_ok=True)
        self.python = python or os.environ.get("HARP_BUILD_PYTHON") or sys.executable
        self.builds = {}
        for name in os.listdir(self.dir):
            if name.endswith(".json"):
                with open(os.path.join(self.dir, name)) as f:
                    b = json.load(f)
                if b["status"] in ("QUEUED", "RUNNING") and b.get("where", {}).get("mode") != "tapis":
                    b.update(status="FAILED", error="the server stopped during this build; start it again")
                self.builds[b["id"]] = b
        self._lock = threading.Lock()

    def _save(self, b):
        tmp = os.path.join(self.dir, f".{b['id']}.json.tmp")
        with open(tmp, "w") as f:
            json.dump(b, f, indent=1)
        os.replace(tmp, os.path.join(self.dir, f"{b['id']}.json"))

    def list(self, owner, app_id=None):
        return sorted((b for b in self.builds.values() if b["owner"] == owner and (not app_id or b["app_id"] == app_id)),
                      key=lambda b: b["created_at"], reverse=True)

    def start(self, owner, app_id, label, sweeps, header, table, report, storage, gateway,
              models=None, training_sets=None, where=None, run=True):
        """where: {"mode": "local"} or {"mode": "tapis", "app_id", "app_version", "system_id", "queue",
        "cores_per_node", "memory_mb", "max_minutes", "scheduler_options"}"""
        name = build_name(label)
        stamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        where = where or {"mode": "local"}
        b = {"id": uuidlib.uuid4().hex[:8], "owner": owner, "app_id": app_id, "name": name, "created_at": _now(),
             "sweeps": sweeps, "report": report, "storage": storage, "where": where,
             "models": [m for m in (models or MODELS) if m in MODELS] or list(MODELS),
             "training_sets": [t for t in (training_sets or TRAINING_SETS) if t in TRAINING_SETS] or list(TRAINING_SETS),
             "dest": f"{storage['path'].rstrip('/')}/builds/{name}_{stamp}",
             "dataset_file": f"{name}_{stamp}.csv", "status": "QUEUED", "step": "preprocess",
             "steps": {"pool": "done", "standardize": "done", "preprocess": "waiting", "train": "waiting", "save": "waiting"},
             "job": None, "metrics": [], "best": None, "files": [], "log": [], "error": None, "ended_at": None}
        work = os.path.join(self.dir, b["id"])
        os.makedirs(work, exist_ok=True)
        data = to_csv(header, table)
        with open(os.path.join(work, b["dataset_file"]), "wb") as f:
            f.write(data)
        if where["mode"] == "tapis":
            self._submit(b, gateway, data)          # raises TapisError: nothing saved, the caller reports it
        self.builds[b["id"]] = b
        self._save(b)
        if run and where["mode"] != "tapis":
            threading.Thread(target=self._run_local, args=(b, gateway), name=f"harp-build-{b['id']}", daemon=True).start()
        return b

    def spec_arg(self, b):
        spec = {"name": b["name"], "dataset_file": b["dataset_file"], "models": b["models"], "training_sets": b["training_sets"]}
        return base64.urlsafe_b64encode(json.dumps(spec, separators=(",", ":")).encode()).decode()

    # ------------------------------------------------------------ as a TAPIS job
    def _submit(self, b, gateway, data):
        w, sys_id, dest = b["where"], b["storage"]["system_id"], b["dest"]
        gateway.mkdir(sys_id, f"{dest}/input")
        gateway.upload(sys_id, f"{dest}/input/{b['dataset_file']}", data)
        params = {"appArgs": [{"arg": self.spec_arg(b)}], "archiveFilter": {"includeLaunchFiles": False}}
        if w.get("scheduler_options"):
            params["schedulerOptions"] = [{"arg": w["scheduler_options"]}]
        request = {"name": f"harp-build-{b['name']}-{b['id']}"[:64],
                   "description": f"HARP build: {', '.join(b['models'])} for {b['name']}",
                   "appId": w["app_id"], "appVersion": w["app_version"], "execSystemId": w["system_id"],
                   "nodeCount": 1, "coresPerNode": w["cores_per_node"], "memoryMB": w["memory_mb"],
                   "maxMinutes": w["max_minutes"], "archiveSystemId": sys_id, "archiveSystemDir": dest,
                   "archiveOnAppError": True,
                   "fileInputs": [{"name": "training-data", "sourceUrl": f"tapis://{sys_id}{dest}/input/{b['dataset_file']}",
                                   "targetPath": b["dataset_file"]}],
                   "parameterSet": params, "tags": ["harp", "harp-build", f"harp-build-{b['id']}"]}
        if w.get("queue"):
            request["execSystemLogicalQueue"] = w["queue"]
        uuid = gateway.submit_job(request)
        b["job"] = {"uuid": uuid, "name": request["name"], "status": "PENDING", "system_id": w["system_id"],
                    "queue": w.get("queue"), "submitted_at": _now(), "ended_at": None, "exec_output": None}

    def tick(self, gateways):
        """Follow TAPIS build jobs (called by the campaign poller)."""
        for b in list(self.builds.values()):
            if b["where"].get("mode") != "tapis" or b["status"] not in ("QUEUED", "RUNNING"):
                continue
            gw = gateways.get(b["owner"])
            if gw is None or gw.expired():
                continue
            try:
                self._poll(b, gw)
            except Exception as e:   # never let one build stop the poller
                b["error"] = f"status check failed: {e}"
            self._save(b)

    def _poll(self, b, gw):
        job = b["job"]
        status = gw.job_status(job["uuid"])
        job["status"] = status
        if status == "RUNNING":
            b["status"] = "RUNNING"
            try:
                if not job.get("exec_output"):
                    job["exec_output"] = list(gw.job_output_dir(job["uuid"]))
                s, path = job["exec_output"]
                p = json.loads(gw.download(s, f"{path}/{PROGRESS_FILE_NAME}"))
                for k, v in (p.get("steps") or {}).items():
                    b["steps"][k] = v
                b["step"] = p.get("step") or b["step"]
            except (TapisError, ValueError, TypeError):
                pass
        elif status in DONE_JOB:
            job["ended_at"] = b["ended_at"] = _now()
            self._collect(b, gw, status)

    def _collect(self, b, gw, status):
        """The job archived its results to the destination folder: read the summary and scores."""
        sys_id, dest = b["storage"]["system_id"], b["dest"]
        try:
            summary = json.loads(gw.download(sys_id, f"{dest}/{SUMMARY_FILE_NAME}"))
        except (TapisError, ValueError):
            summary = {"ok": False, "error": f"the TAPIS job ended {status} without a build summary"}
        if summary.get("ok"):
            b["metrics"] = parse_metrics(gw.download(sys_id, f"{dest}/model_commons.csv").decode())
            b["best"] = best_of(b["metrics"])
            b["files"] = summary.get("files", [])
            b["steps"].update(preprocess="done", train="done", save="done")
            b["status"] = "DONE"
        else:
            if b["steps"].get(b["step"]) == "running":
                b["steps"][b["step"]] = "failed"
            b.update(status="FAILED", error=summary.get("error") or f"TAPIS job {status}")

    # ------------------------------------------------------------ on this computer
    def _run_local(self, b, gateway):
        with self._lock:     # one at a time: training is heavy
            work = os.path.join(self.dir, b["id"])
            out = os.path.join(work, "out")
            b["status"] = "RUNNING"
            b["steps"]["preprocess"] = "running"
            self._save(b)
            try:
                p = subprocess.run([self.python, RUNNER, self.spec_arg(b), "--input", os.path.join(work, b["dataset_file"]),
                                    "--output", out], capture_output=True, text=True, timeout=BUILD_TIMEOUT)
                b["log"] = [ln for ln in (p.stdout + "\n" + p.stderr).splitlines() if ln.strip()][-80:]
                try:
                    with open(os.path.join(out, PROGRESS_FILE_NAME)) as f:
                        b["steps"].update(json.load(f).get("steps") or {})
                    with open(os.path.join(out, SUMMARY_FILE_NAME)) as f:
                        summary = json.load(f)
                except (OSError, ValueError):
                    summary = {"ok": False, "error": (b["log"] or ["the build runner did not start"])[-1]}
                if not summary.get("ok"):
                    raise RuntimeError(summary.get("error") or "build failed")
                with open(os.path.join(out, "model_commons.csv")) as f:
                    b["metrics"] = parse_metrics(f.read())
                b["best"] = best_of(b["metrics"])
                b["step"], b["steps"]["save"] = "save", "running"
                self._save(b)
                b["files"] = self._upload(b, gateway, out, summary["files"])
                b["steps"]["save"] = "done"
                b["status"] = "DONE"
            except subprocess.TimeoutExpired:
                b.update(status="FAILED", error=f"the build took longer than {BUILD_TIMEOUT // 60} minutes")
            except (RuntimeError, OSError, TapisError) as e:
                for k, v in b["steps"].items():
                    if v == "running":
                        b["steps"][k] = "failed"
                b.update(status="FAILED", error=str(e))
            b["ended_at"] = _now()
            self._save(b)

    def _upload(self, b, gateway, out, files):
        if gateway is None or gateway.expired():
            raise RuntimeError(f"models are built (in {out}) but your TAPIS session expired before they were saved")
        sys_id, dest = b["storage"]["system_id"], b["dest"]
        gateway.mkdir(sys_id, f"{dest}/models")
        for rel in files + [SUMMARY_FILE_NAME]:
            with open(os.path.join(out, rel), "rb") as f:
                gateway.upload(sys_id, f"{dest}/{rel}", f.read())
        return files


def parse_metrics(text):
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        m = {k: r.get(k) for k in ("DataSet", "RegModel", "MODEL_NAME")}
        for k in ("ADJ_FACTOR", "MSE", "MAE", "MAPE", "UPP", "UP_MAPE", "OV_MAPE"):
            n = _number(r.get(k, ""))
            m[k] = n if isinstance(n, (int, float)) and n == n else None   # NaN -> None
        out.append(m)
    return out


def best_of(metrics):
    ok = [m for m in metrics if isinstance(m.get("MAPE"), (int, float))]
    return min(ok, key=lambda m: m["MAPE"]) if ok else None


def build_view(b):
    keep = ("id", "app_id", "name", "created_at", "ended_at", "sweeps", "storage", "dest", "dataset_file", "status",
            "step", "steps", "metrics", "best", "error", "report", "models", "training_sets", "where", "job", "files")
    return {k: b.get(k) for k in keep} | {"log": b["log"][-25:]}


def wait_for(manager, build_id, timeout=600, tick=None):
    """Tests: block until a build ends."""
    end = time.time() + timeout
    while time.time() < end:
        if tick:
            tick()
        if manager.builds[build_id]["status"] in ("DONE", "FAILED"):
            return manager.builds[build_id]
        time.sleep(0.2)
    raise TimeoutError(build_id)
