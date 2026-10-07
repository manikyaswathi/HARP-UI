"""In-memory stand-in for TapisGateway: systems, files and jobs."""
import itertools

from harp_server.tapis_gateway import TapisError


class FakeGateway:
    def __init__(self, username="alice"):
        self.username = username
        self.base_url = "https://fake.tapis.io"
        self.files = {}          # (system, path) -> bytes ; dirs map to None
        self.jobs = {}           # uuid -> {"request":..., "status":...}
        self.submitted = []
        self._ids = itertools.count(1)
        self.fail_submit_for = set()
        self.readonly_systems = set()
        self.expires_at = None

    def expired(self, margin=60):
        import time
        return self.expires_at is not None and time.time() > self.expires_at - margin

    # systems / apps
    def list_systems(self):
        return [{"id": "pitzer", "host": "pitzer.osc.edu", "system_type": "LINUX", "can_exec": True, "effective_user": "a"},
                {"id": "stampede", "host": "stampede.tacc", "system_type": "LINUX", "can_exec": True, "effective_user": "a"},
                {"id": "storage", "host": "data.osc.edu", "system_type": "LINUX", "can_exec": False, "effective_user": "a"}]

    def get_system(self, system_id):
        return {"id": system_id, "host": "h", "can_exec": system_id != "storage", "default_queue": "serial",
                "description": "", "system_type": "LINUX", "batch_scheduler": "SLURM", "runtimes": ["SINGULARITY"],
                "queues": [{"name": "serial", "hpc_queue": "serial", "default": True, "max_cores": 40,
                            "max_memory_mb": 1, "max_minutes": 60, "max_jobs_per_user": 2}],
                "job_working_dir": "HOST_EVAL($SCRATCH)", "root_dir": "/"}

    def exec_systems(self):
        return [self.get_system(s["id"]) for s in self.list_systems() if s["can_exec"]]

    def list_apps(self):
        return [{"id": "harp-sweep-euler", "version": "1.0.0", "image": "docker://x", "runtime": "SINGULARITY",
                 "description": "Euler", "app_args": [],
                 "notes": {"harp": {"label": "Euler number", "entrypoint": "calc_e.py",
                                    "command": "python3 calc_e.py {method} {n}", "workdir": "/app/01-eulers_number",
                                    "params": [{"name": "method", "arg": "{method}", "kind": "string", "default": "pow"},
                                               {"name": "n", "arg": "{n}", "kind": "int", "default": "1000"}]}}}]

    # files
    def _check_write(self, system_id):
        if system_id in self.readonly_systems:
            raise TapisError("permission denied")

    def list_files(self, system_id, path):
        prefix = path.rstrip("/") + "/"
        names = {p[len(prefix):].split("/")[0] for (s, p) in self.files if s == system_id and p.startswith(prefix)}
        return [{"name": n, "path": prefix + n, "type": "dir", "size": 0} for n in sorted(names)]

    def mkdir(self, system_id, path):
        self._check_write(system_id)
        self.files[(system_id, path)] = None

    def upload(self, system_id, path, content):
        self._check_write(system_id)
        self.files[(system_id, path)] = content

    def download(self, system_id, path):
        data = self.files.get((system_id, path))
        if data is None:
            raise TapisError(f"not found: {path}")
        return data

    def delete(self, system_id, path):
        for key in [k for k in self.files if k[0] == system_id and k[1].startswith(path)]:
            del self.files[key]

    # jobs
    def submit_job(self, request):
        if request["execSystemId"] in self.fail_submit_for:
            raise TapisError("queue limit exceeded")
        uuid = f"job-{next(self._ids)}"
        self.jobs[uuid] = {"request": request, "status": "PENDING"}
        self.submitted.append(uuid)
        return uuid

    def job_status(self, uuid):
        return self.jobs[uuid]["status"]

    def cancel_job(self, uuid):
        self.jobs[uuid]["status"] = "CANCELLED"

    def job_output_dir(self, uuid):
        return self.jobs[uuid]["request"]["execSystemId"], f"/exec/{uuid}"

    # helpers for tests
    def finish(self, uuid, csv_bytes=None, status="FINISHED"):
        job = self.jobs[uuid]
        job["status"] = status
        if csv_bytes is not None:
            req = job["request"]
            self.files[(req["archiveSystemId"], req["archiveSystemDir"] + "/harp_profile.csv")] = csv_bytes

    def check_storage(self, system_id, path):
        try:
            self.mkdir(system_id, path + "/.probe")
            return {"ok": True, "steps": ["mkdir", "write", "read", "delete"], "error": None}
        except TapisError as e:
            return {"ok": False, "steps": [], "error": str(e)}

    def check_target(self, target):
        ok = target["system_id"] != "storage"
        return {"system_id": target["system_id"], "ok": ok, "error": None if ok else "cannot exec"}
