"""
Campaign lifecycle: submit the planned jobs to TAPIS (respecting each
system's concurrency limit), track their status, and once every job has
ended merge the profiling CSVs into one file on the chosen storage system.
"""

import base64
import csv
import io
import json
import os
import threading
import time
import uuid as uuidlib
from datetime import datetime, timezone

from .sweep import plan_jobs, summarize, validate_spec
from .tapis_gateway import TERMINAL_STATUSES, TapisError

PROFILE_FILE_NAME = "harp_profile.csv"
NOT_SUBMITTED = "NOT_SUBMITTED"
SUBMIT_FAILED = "SUBMIT_FAILED"
JOB_DONE_STATUSES = TERMINAL_STATUSES | {SUBMIT_FAILED}
MAX_SPEC_ARG_BYTES = 32 * 1024

# Campaign statuses
RUNNING = "RUNNING"
WAITING_FOR_LOGIN = "WAITING_FOR_LOGIN"
FINALIZING = "FINALIZING"
DONE = "DONE"
DONE_WITH_ERRORS = "DONE_WITH_ERRORS"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
ACTIVE_CAMPAIGN_STATUSES = {RUNNING, WAITING_FOR_LOGIN, FINALIZING}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode_job_spec(spec, job):
    payload = {"job_name": job["name"], "command": spec["command"], "workdir": spec["workdir"],
               "repetitions": spec["repetitions"], "run_timeout_sec": spec["run_timeout_sec"],
               "combinations": job["combinations"]}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    if len(encoded) > MAX_SPEC_ARG_BYTES:
        raise ValueError(f"job {job['name']} spec is too large; lower combos_per_job")
    return encoded


def build_job_request(campaign, job):
    spec = campaign["spec"]
    target = next(t for t in spec["targets"] if t["key"] == job["target"])
    parameter_set = {"appArgs": [{"arg": encode_job_spec(spec, job)}],
                     "archiveFilter": {"includeLaunchFiles": False}}
    if target.get("scheduler_options"):
        parameter_set["schedulerOptions"] = [{"arg": target["scheduler_options"]}]
    if target.get("container_args"):
        # e.g. "--nv" so Singularity exposes the node's GPUs to the container
        parameter_set["containerArgs"] = [{"arg": target["container_args"]}]
    request = {
        "name": job["name"],
        "description": f"HARP sweep {spec['name']} ({job['run_type']}, {len(job['combinations'])} combinations)",
        "appId": target["app_id"],
        "appVersion": target["app_version"],
        "execSystemId": target["system_id"],
        "archiveSystemId": spec["storage"]["system_id"],
        "archiveSystemDir": job["archive_dir"],
        "archiveOnAppError": True,
        "parameterSet": parameter_set,
        "tags": ["harp", f"harp-campaign-{campaign['id']}"],
    }
    if target.get("queue"):
        request["execSystemLogicalQueue"] = target["queue"]
    for key, field in (("node_count", "nodeCount"), ("cores_per_node", "coresPerNode"),
                       ("memory_mb", "memoryMB"), ("max_minutes", "maxMinutes")):
        if key in target:
            request[field] = target[key]
    return request


def merge_csvs(csv_blobs):
    """Merge CSV files that may have different columns (different parameters)."""
    fieldnames, rows = [], []
    for blob in csv_blobs:
        reader = csv.DictReader(io.StringIO(blob.decode("utf-8")))
        for name in reader.fieldnames or []:
            if name not in fieldnames:
                fieldnames.append(name)
        rows.extend(reader)
    # keep walltime as the last column, like DataScrapper.py
    if "walltime" in fieldnames:
        fieldnames.remove("walltime")
        fieldnames.append("walltime")
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, restval="")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode(), len(rows)


class CampaignStore:
    """One JSON file per campaign so state survives server restarts."""

    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "campaigns")
        os.makedirs(self.dir, exist_ok=True)

    def save(self, campaign):
        path = os.path.join(self.dir, f"{campaign['id']}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(campaign, f, indent=1)
        os.replace(tmp, path)

    def load_all(self):
        campaigns = {}
        for name in os.listdir(self.dir):
            if name.endswith(".json"):
                with open(os.path.join(self.dir, name)) as f:
                    c = json.load(f)
                    campaigns[c["id"]] = c
        return campaigns


class CampaignManager:
    def __init__(self, store: CampaignStore, poll_interval=15):
        self.store = store
        self.poll_interval = poll_interval
        self.campaigns = store.load_all()
        self.gateways = {}  # owner username -> latest logged-in TapisGateway
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------- sessions
    def register_gateway(self, gateway):
        with self._lock:
            self.gateways[gateway.username] = gateway

    # ------------------------------------------------------------ campaigns
    def preview(self, raw_spec):
        spec = validate_spec(raw_spec)
        jobs = plan_jobs(spec)
        for job in jobs:
            encode_job_spec(spec, job)
        return spec, jobs, summarize(spec, jobs)

    def create(self, raw_spec, gateway):
        spec, jobs, summary = self.preview(raw_spec)
        cid = uuidlib.uuid4().hex[:8]
        campaign_dir = f"{spec['storage']['path']}/{spec['name']}-{cid}"
        for job in jobs:
            job.update({"archive_dir": f"{campaign_dir}/jobs/{job['name']}", "status": NOT_SUBMITTED,
                        "uuid": None, "error": None, "submitted_at": None, "ended_at": None})
        campaign = {"id": cid, "owner": gateway.username, "created_at": _now(), "status": RUNNING,
                    "spec": spec, "summary": summary, "campaign_dir": campaign_dir, "jobs": jobs,
                    "result": None, "error": None, "events": []}
        # Write the spec to storage first: this fails fast if the location is not writable.
        storage = spec["storage"]
        gateway.mkdir(storage["system_id"], campaign_dir)
        gateway.upload(storage["system_id"], f"{campaign_dir}/campaign_spec.json",
                       json.dumps(spec, indent=2).encode())
        self.register_gateway(gateway)
        with self._lock:
            self.campaigns[cid] = campaign
            self._event(campaign, f"created with {len(jobs)} jobs")
            self.store.save(campaign)
        return campaign

    def get(self, cid, owner):
        c = self.campaigns.get(cid)
        if c is None or c["owner"] != owner:
            return None
        return c

    def list(self, owner):
        return sorted((c for c in self.campaigns.values() if c["owner"] == owner),
                      key=lambda c: c["created_at"], reverse=True)

    def cancel(self, campaign):
        gateway = self.gateways.get(campaign["owner"])
        with self._lock:
            for job in campaign["jobs"]:
                if job["status"] == NOT_SUBMITTED:
                    job["status"] = "CANCELLED"
                elif job["status"] not in JOB_DONE_STATUSES and gateway:
                    try:
                        gateway.cancel_job(job["uuid"])
                    except TapisError as e:
                        job["error"] = f"cancel failed: {e}"
            campaign["status"] = CANCELLED
            self._event(campaign, "cancelled by user")
            self.store.save(campaign)

    def resubmit_failed(self, campaign):
        with self._lock:
            count = 0
            for job in campaign["jobs"]:
                if job["status"] in ("FAILED", "CANCELLED", SUBMIT_FAILED):
                    job.update({"status": NOT_SUBMITTED, "uuid": None, "error": None,
                                "submitted_at": None, "ended_at": None})
                    count += 1
            if count:
                campaign["status"] = RUNNING
                campaign["result"] = None
                self._event(campaign, f"resubmitting {count} jobs")
                self.store.save(campaign)
            return count

    # ---------------------------------------------------------- the engine
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="harp-poller", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_interval)

    def tick(self):
        for campaign in list(self.campaigns.values()):
            if campaign["status"] in ACTIVE_CAMPAIGN_STATUSES:
                try:
                    self._advance(campaign)
                except Exception as e:  # never let one campaign kill the poller
                    self._event(campaign, f"poller error: {e}")
                    self.store.save(campaign)

    def _advance(self, campaign):
        gateway = self.gateways.get(campaign["owner"])
        with self._lock:
            if gateway is None or gateway.expired():
                if campaign["status"] != WAITING_FOR_LOGIN:
                    campaign["status"] = WAITING_FOR_LOGIN
                    self._event(campaign, "paused: owner must log in to TAPIS again")
                    self.store.save(campaign)
                return
            if campaign["status"] == WAITING_FOR_LOGIN:
                campaign["status"] = RUNNING
                self._event(campaign, "resumed")
            self._poll_jobs(campaign, gateway)
            self._submit_jobs(campaign, gateway)
            if all(j["status"] in JOB_DONE_STATUSES for j in campaign["jobs"]):
                self._finalize(campaign, gateway)
            self.store.save(campaign)

    def _poll_jobs(self, campaign, gateway):
        for job in campaign["jobs"]:
            if job["uuid"] and job["status"] not in JOB_DONE_STATUSES:
                try:
                    status = gateway.job_status(job["uuid"])
                except TapisError as e:
                    job["error"] = f"status check failed: {e}"
                    continue
                if status != job["status"]:
                    job["status"] = status
                    if status in TERMINAL_STATUSES:
                        job["ended_at"] = _now()
                        self._event(campaign, f"{job['name']} {status} on {job['system_id']}")

    def _submit_jobs(self, campaign, gateway):
        active = {}
        for job in campaign["jobs"]:
            if job["uuid"] and job["status"] not in JOB_DONE_STATUSES:
                active[job["target"]] = active.get(job["target"], 0) + 1
        limits = {t["key"]: t["max_concurrent_jobs"] for t in campaign["spec"]["targets"]}
        for job in campaign["jobs"]:
            if job["status"] != NOT_SUBMITTED or active.get(job["target"], 0) >= limits[job["target"]]:
                continue
            try:
                job["uuid"] = gateway.submit_job(build_job_request(campaign, job))
                job["status"] = "PENDING"
                job["submitted_at"] = _now()
                job["error"] = None
                active[job["target"]] = active.get(job["target"], 0) + 1
            except (TapisError, ValueError) as e:
                job["status"] = SUBMIT_FAILED
                job["error"] = str(e)
                self._event(campaign, f"{job['name']} could not be submitted: {e}")

    def _finalize(self, campaign, gateway):
        campaign["status"] = FINALIZING
        storage = campaign["spec"]["storage"]
        blobs, manifest = [], []
        for job in campaign["jobs"]:
            entry = {"name": job["name"], "system_id": job["system_id"], "uuid": job["uuid"],
                     "status": job["status"], "archive_dir": job["archive_dir"], "rows": 0}
            if job["uuid"]:
                try:
                    blob = gateway.download(storage["system_id"], f"{job['archive_dir']}/{PROFILE_FILE_NAME}")
                    blobs.append(blob)
                    entry["rows"] = max(blob.count(b"\n") - 1, 0)
                except TapisError as e:
                    entry["error"] = f"no profiling data: {e}"
            manifest.append(entry)

        succeeded = sum(1 for j in campaign["jobs"] if j["status"] == "FINISHED")
        result = {"jobs_finished": succeeded, "jobs_total": len(campaign["jobs"]), "rows": 0,
                  "csv_path": None, "manifest_path": f"{campaign['campaign_dir']}/manifest.json"}
        try:
            if blobs:
                merged, rows = merge_csvs(blobs)
                stamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
                # <application>_<timestamp>.csv is the name the build phase picks up.
                csv_path = f"{campaign['campaign_dir']}/{campaign['spec']['application']}_{stamp}.csv"
                gateway.upload(storage["system_id"], csv_path, merged)
                result.update({"rows": rows, "csv_path": csv_path})
            gateway.upload(storage["system_id"], result["manifest_path"],
                           json.dumps({"campaign": campaign["id"], "jobs": manifest}, indent=2).encode())
        except TapisError as e:
            # Stay in FINALIZING; the next tick retries the upload.
            campaign["error"] = f"could not write results to storage: {e}"
            self._event(campaign, campaign["error"])
            return

        campaign["result"] = result
        campaign["error"] = None
        if result["rows"] == 0:
            campaign["status"] = FAILED
        elif succeeded == len(campaign["jobs"]):
            campaign["status"] = DONE
        else:
            campaign["status"] = DONE_WITH_ERRORS
        self._event(campaign, f"{campaign['status']}: {result['rows']} profiling rows -> {result['csv_path']}")

    def _event(self, campaign, message):
        campaign["events"].append({"at": _now(), "message": message})
        del campaign["events"][:-200]


def campaign_view(c, include_jobs=True):
    counts = {}
    for j in c["jobs"]:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    done = sum(v for k, v in counts.items() if k in JOB_DONE_STATUSES)
    view = {k: c[k] for k in ("id", "owner", "created_at", "status", "summary",
                              "campaign_dir", "result", "error")}
    view.update({"name": c["spec"]["name"], "application": c["spec"]["application"],
                 "storage": c["spec"]["storage"], "job_counts": counts,
                 "jobs_done": done, "jobs_total": len(c["jobs"]),
                 "app_ids": sorted({t["app_id"] for t in c["spec"]["targets"]}),
                 "events": c["events"][-30:]})
    if include_jobs:
        view["jobs"] = [{k: j[k] for k in ("name", "system_id", "run_type", "status", "uuid",
                                            "error", "submitted_at", "ended_at")}
                        | {"combinations": len(j["combinations"])} for j in c["jobs"]]
    return view
