"""
Build phase from the profiling page: pool -> standardize -> build.

1. Pool: the profiling rows of the sweeps you pick for an app (read through TAPIS).
2. Standardize: one table in the format the HARP pipeline's build phase reads
   (run_config, run_type, sys_*, run_<param>, walltime), with the hardware each
   job was given (sys_alloc_cores, sys_alloc_mem_mb), every column filled in
   every row, and enough SD / FS / test_data rows to train on.
3. Build: the pipeline's own build modules (pipeline/modules/pipeline.py:
   data_preprocessor = StandardScaler + PCA, then model_trainer = LR / NN / DTR
   on SD, SD+25FS, SD+50FS, SD+75FS) run on this machine, and the dataset,
   models and scores are copied to a TAPIS folder.
"""

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
PIPELINE_HOME = os.environ.get("HARP_PIPELINE_HOME", os.path.join(REPO_ROOT, "pipeline"))
BUILD_TIMEOUT = int(os.environ.get("HARP_BUILD_TIMEOUT", "3600"))


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
    """Runs builds one at a time in a background thread; state is kept as JSON so it survives restarts."""

    def __init__(self, data_dir, python=None):
        self.dir = os.path.abspath(os.path.join(data_dir, "builds"))   # the pipeline runs from its own folder
        os.makedirs(self.dir, exist_ok=True)
        self.python = python or os.environ.get("HARP_BUILD_PYTHON") or sys.executable
        self.builds = {}
        for name in os.listdir(self.dir):
            if name.endswith(".json"):
                with open(os.path.join(self.dir, name)) as f:
                    b = json.load(f)
                if b["status"] in ("QUEUED", "RUNNING"):   # the server stopped mid-build
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

    def start(self, owner, app_id, label, sweeps, header, table, report, storage, gateway, run=True):
        name = build_name(label)
        stamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        b = {"id": uuidlib.uuid4().hex[:8], "owner": owner, "app_id": app_id, "name": name, "created_at": _now(),
             "sweeps": sweeps, "report": report, "storage": storage,
             "dest": f"{storage['path'].rstrip('/')}/builds/{name}_{stamp}",
             "dataset_file": f"{name}_{stamp}.csv", "status": "QUEUED", "step": "standardize",
             "steps": {"pool": "done", "standardize": "done", "preprocess": "waiting", "train": "waiting", "upload": "waiting"},
             "metrics": [], "best": None, "log": [], "error": None, "ended_at": None}
        work = os.path.join(self.dir, b["id"])
        app_dir = os.path.join(work, "TARGET_APP", name)
        os.makedirs(app_dir, exist_ok=True)
        with open(os.path.join(app_dir, b["dataset_file"]), "wb") as f:
            f.write(to_csv(header, table))
        config = {"application": name, "application_category": "basic", "cheetah_app_directory": name,
                  "modules_to_run": ["build"], "runs": [], "project_account": "NA"}
        with open(os.path.join(app_dir, "pipeline_config.json"), "w") as f:
            json.dump(config, f, indent=2)
        self.builds[b["id"]] = b
        self._save(b)
        if run:
            threading.Thread(target=self.run, args=(b, gateway), name=f"harp-build-{b['id']}", daemon=True).start()
        return b

    # ----------------------------------------------------------------- running
    def _log(self, b, text):
        lines = [ln for ln in text.splitlines() if ln.strip()]
        b["log"] = (b["log"] + lines)[-80:]

    def _module(self, b, module, env, cfg):
        cmd = [self.python, os.path.join(PIPELINE_HOME, "modules", "pipeline.py"), "--module", module, "--config", cfg]
        p = subprocess.run(cmd, cwd=os.path.join(PIPELINE_HOME, "modules"), env=env, capture_output=True,
                           text=True, timeout=BUILD_TIMEOUT)
        self._log(b, p.stdout + "\n" + p.stderr)
        if p.returncode != 0:
            tail = [ln for ln in (p.stderr or p.stdout).splitlines() if ln.strip()][-1:] or ["no output"]
            raise RuntimeError(f"{module} failed: {tail[0]}")

    def run(self, b, gateway):
        with self._lock:     # one build at a time: training is heavy
            self._run(b, gateway)

    def _run(self, b, gateway):
        work = os.path.join(self.dir, b["id"])
        store = os.path.join(work, "HARP_STORE")
        app_home = os.path.join(store, "applications", "basic", b["name"])
        cfg = os.path.join(work, "TARGET_APP", b["name"], "pipeline_config.json")
        env = {**os.environ, "TARGET_APP": os.path.join(work, "TARGET_APP"), "HARP_STORE": store,
               "PIPELINE_HOME": PIPELINE_HOME, "TF_CPP_MIN_LOG_LEVEL": "2", "PYTHONUNBUFFERED": "1"}
        b["status"] = "RUNNING"
        try:
            for step, module, out in (("preprocess", "data_preprocessor", os.path.join(app_home, "train", "full_dataset_pca.csv")),
                                      ("train", "model_trainer", os.path.join(app_home, "model_commons.csv"))):
                b["step"], b["steps"][step] = step, "running"
                self._save(b)
                self._module(b, module, env, cfg)
                if not os.path.isfile(out):   # the pipeline prints errors and still exits 0
                    errs = [ln for ln in b["log"] if "ERROR" in ln or "Error" in ln]
                    raise RuntimeError(f"{module} wrote no {os.path.basename(out)}" + (f": {errs[-1]}" if errs else ""))
                b["steps"][step] = "done"
            b["metrics"] = read_metrics(os.path.join(app_home, "model_commons.csv"))
            ok = [m for m in b["metrics"] if isinstance(m.get("MAPE"), (int, float))]
            b["best"] = min(ok, key=lambda m: m["MAPE"]) if ok else None
            b["step"], b["steps"]["upload"] = "upload", "running"
            self._save(b)
            b["files"] = self._upload(b, gateway, app_home, work)
            b["steps"]["upload"] = "done"
            b["status"] = "DONE"
        except subprocess.TimeoutExpired:
            b["steps"][b["step"]] = "failed"
            b.update(status="FAILED", error=f"{b['step']} took longer than {BUILD_TIMEOUT // 60} minutes")
        except (RuntimeError, OSError, TapisError) as e:
            b["steps"][b["step"]] = "failed"
            b.update(status="FAILED", error=str(e))
        b["ended_at"] = _now()
        self._save(b)

    def _upload(self, b, gateway, app_home, work):
        """Dataset, PCA dataset, scores and models -> <results folder>/builds/<name>_<stamp>/ on TAPIS."""
        if gateway is None or gateway.expired():
            raise RuntimeError(f"models are built (in {app_home}) but your TAPIS session expired before upload")
        sys_id, dest = b["storage"]["system_id"], b["dest"]
        files = [(os.path.join(work, "TARGET_APP", b["name"], b["dataset_file"]), b["dataset_file"]),
                 (os.path.join(work, "TARGET_APP", b["name"], "pipeline_config.json"), "pipeline_config.json"),
                 (os.path.join(app_home, "train", "full_dataset_pca.csv"), "full_dataset_pca.csv"),
                 (os.path.join(app_home, "model_commons.csv"), "model_commons.csv")]
        models = os.path.join(app_home, "models")
        files += [(os.path.join(models, f), f"models/{f}") for f in sorted(os.listdir(models))]
        gateway.mkdir(sys_id, f"{dest}/models")
        done = []
        for local, rel in files:
            with open(local, "rb") as f:
                gateway.upload(sys_id, f"{dest}/{rel}", f.read())
            done.append(rel)
        return done


def read_metrics(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        m = {k: r.get(k) for k in ("DataSet", "RegModel", "MODEL_NAME")}
        for k in ("ADJ_FACTOR", "MSE", "MAE", "MAPE", "UPP", "UP_MAPE", "OV_MAPE"):
            n = _number(r.get(k, ""))
            m[k] = n if isinstance(n, (int, float)) and n == n else None   # NaN -> None
        out.append(m)
    return out


def build_view(b):
    keep = ("id", "app_id", "name", "created_at", "ended_at", "sweeps", "storage", "dest", "dataset_file", "status",
            "step", "steps", "metrics", "best", "error", "report")
    return {k: b.get(k) for k in keep} | {"files": b.get("files", []), "log": b["log"][-25:]}


def wait_for(manager, build_id, timeout=600):
    """Tests: block until a build ends."""
    end = time.time() + timeout
    while time.time() < end:
        if manager.builds[build_id]["status"] in ("DONE", "FAILED"):
            return manager.builds[build_id]
        time.sleep(0.2)
    raise TimeoutError(build_id)
