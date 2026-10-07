"""
Every access to a compute system or storage location goes through this
module, using the TAPIS Systems, Files, Apps and Jobs services. The server
never touches cluster file systems directly.
"""

import threading
import time

TERMINAL_STATUSES = {"FINISHED", "FAILED", "CANCELLED"}


def _plain(obj):
    """tapipy returns nested TapisResult objects; turn them into plain JSON data."""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        return {k: _plain(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


class TapisError(RuntimeError):
    pass


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class TapisGateway:
    """Thin, thread-safe wrapper around a logged-in tapipy client."""

    def __init__(self, client, username, base_url, expires_at=None):
        self.client = client
        self.username = username
        self.base_url = base_url
        self._fixed_expiry = expires_at  # only used when there is no client (tests)
        self._lock = threading.Lock()

    @property
    def expires_at(self):
        """When this session stops working, in epoch seconds (None = unknown).

        If TAPIS issued a refresh token, tapipy renews the access token by
        itself, so the session lasts until the refresh token expires.
        """
        if self.client is None:
            return self._fixed_expiry
        for token in (getattr(self.client, "refresh_token", None), getattr(self.client, "access_token", None)):
            exp = _get(_get(token, "claims", {}), "exp") if token else None
            if exp:
                return float(exp)
        return None

    def expired(self, margin=60):
        exp = self.expires_at
        return exp is not None and time.time() > exp - margin

    @classmethod
    def login(cls, base_url, username=None, password=None, access_token=None):
        """Log in to TAPIS on the server side with tapipy.

        Either a username/password (exchanged for a token with get_tokens())
        or an existing access token. Any token is checked with TAPIS
        (get_userinfo), so a forged one is rejected. The password is dropped
        from memory as soon as the token is issued; the token stays on the
        server and is never sent to the browser.
        """
        try:
            from tapipy.tapis import Tapis
        except ImportError as e:
            raise TapisError("tapipy is not installed on the HARP server") from e
        try:
            if access_token:
                client = Tapis(base_url=base_url, access_token=access_token)
                username = _get(client.authenticator.get_userinfo(), "username")
            else:
                client = Tapis(base_url=base_url, username=username, password=password)
                client.get_tokens()
        except Exception as e:
            msg = str(e).replace(password, "***") if password else str(e)
            raise TapisError(f"TAPIS login failed: {msg}") from None
        finally:
            password = None
        client.password = None
        if not username:
            raise TapisError("TAPIS did not return a username for this token")
        return cls(client, username, base_url)

    def _call(self, fn, *args, **kwargs):
        # tapipy refreshes expiring tokens itself; we only serialize calls and
        # turn its many exception types into one.
        with self._lock:
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                raise TapisError(str(e)) from e

    # ----------------------------------------------------------------- systems
    def list_systems(self):
        systems = self._call(self.client.systems.getSystems, listType="ALL", limit=-1)
        return [{"id": _get(s, "id"), "host": _get(s, "host"), "system_type": _get(s, "systemType"),
                 "can_exec": bool(_get(s, "canExec")), "effective_user": _get(s, "effectiveUserId")}
                for s in systems]

    def get_system(self, system_id):
        """A system and its batch queues, as TAPIS defines them (LogicalQueue)."""
        s = self._call(self.client.systems.getSystem, systemId=system_id)
        default = _get(s, "batchDefaultLogicalQueue")
        queues = [{"name": _get(q, "name"), "hpc_queue": _get(q, "hpcQueueName"),
                   "description": _get(q, "description"), "default": _get(q, "name") == default,
                   "max_jobs": _get(q, "maxJobs"), "max_jobs_per_user": _get(q, "maxJobsPerUser"),
                   "min_nodes": _get(q, "minNodeCount"), "max_nodes": _get(q, "maxNodeCount"),
                   "min_cores": _get(q, "minCoresPerNode"), "max_cores": _get(q, "maxCoresPerNode"),
                   "min_memory_mb": _get(q, "minMemoryMB"), "max_memory_mb": _get(q, "maxMemoryMB"),
                   "min_minutes": _get(q, "minMinutes"), "max_minutes": _get(q, "maxMinutes")}
                  for q in (_get(s, "batchLogicalQueues") or [])]
        return {"id": _get(s, "id"), "host": _get(s, "host"), "description": _get(s, "description") or "",
                "system_type": _get(s, "systemType"), "enabled": _get(s, "enabled", True) is not False,
                "can_exec": bool(_get(s, "canExec")), "can_run_batch": bool(_get(s, "canRunBatch")),
                "batch_scheduler": _get(s, "batchScheduler"),
                "runtimes": [_get(r, "runtimeType") for r in (_get(s, "jobRuntimes") or [])],
                "default_queue": default, "queues": queues,
                "job_working_dir": _get(s, "jobWorkingDir"), "root_dir": _get(s, "rootDir")}

    def exec_systems(self):
        """Every system the user can run jobs on, each with its queues."""
        out = []
        for s in self.list_systems():
            if not s["can_exec"]:
                continue
            try:
                out.append(self.get_system(s["id"]))
            except TapisError as e:
                out.append({**s, "queues": [], "error": str(e)})
        return out

    def list_apps(self):
        apps = self._call(self.client.apps.getApps, listType="ALL", limit=-1, select="allAttributes")
        out = []
        for a in apps:
            params = _get(_get(a, "jobAttributes"), "parameterSet")
            out.append({
                "id": _get(a, "id"), "version": _get(a, "version"),
                "image": _get(a, "containerImage"), "runtime": _get(a, "runtime"),
                "description": _get(a, "description") or "",
                "notes": _plain(_get(a, "notes")) or {},
                "app_args": [{"name": _get(x, "name"), "arg": _get(x, "arg")}
                             for x in (_get(params, "appArgs") or [])],
            })
        return out

    # ------------------------------------------------------------------- files
    def list_files(self, system_id, path):
        items = self._call(self.client.files.listFiles, systemId=system_id, path=path, limit=1000)
        return [{"name": _get(f, "name"), "path": _get(f, "path"), "type": _get(f, "type"),
                 "size": _get(f, "size")} for f in items]

    def mkdir(self, system_id, path):
        self._call(self.client.files.mkdir, systemId=system_id, path=path)

    def upload(self, system_id, path, content: bytes):
        name = path.rstrip("/").rsplit("/", 1)[-1]
        self._call(self.client.files.insert, systemId=system_id, path=path, file=(name, content))

    def download(self, system_id, path) -> bytes:
        data = self._call(self.client.files.getContents, systemId=system_id, path=path)
        return data if isinstance(data, bytes) else str(data).encode()

    def delete(self, system_id, path):
        self._call(self.client.files.delete, systemId=system_id, path=path)

    # -------------------------------------------------------------------- jobs
    def submit_job(self, request: dict) -> str:
        job = self._call(self.client.jobs.submitJob, **request)
        return _get(job, "uuid")

    def job_status(self, uuid) -> str:
        return _get(self._call(self.client.jobs.getJobStatus, jobUuid=uuid), "status")

    def cancel_job(self, uuid):
        self._call(self.client.jobs.cancelJob, jobUuid=uuid)

    # --------------------------------------------------------- access checks
    def check_storage(self, system_id, path):
        """Prove the storage location is writable and readable through TAPIS."""
        probe_dir = f"{path.rstrip('/')}/.harp_access_check_{int(time.time())}"
        steps = []
        try:
            self.mkdir(system_id, probe_dir)
            steps.append("mkdir")
            self.upload(system_id, f"{probe_dir}/probe.txt", b"harp")
            steps.append("write")
            ok = self.download(system_id, f"{probe_dir}/probe.txt") == b"harp"
            steps.append("read")
            self.delete(system_id, probe_dir)
            steps.append("delete")
            return {"ok": ok, "steps": steps, "error": None if ok else "read back mismatch"}
        except TapisError as e:
            return {"ok": False, "steps": steps, "error": str(e)}

    def check_target(self, target):
        """Prove an execution system and its HARP app are usable through TAPIS."""
        result = {"system_id": target["system_id"], "ok": False, "error": None}
        try:
            system = self.get_system(target["system_id"])
            if not system["can_exec"]:
                raise TapisError("system cannot execute jobs (canExec is false)")
            queue = target.get("queue")
            if queue and system["queues"] and queue not in [q["name"] for q in system["queues"]]:
                raise TapisError(f"queue '{queue}' is not defined on this system")
            self.list_files(target["system_id"], "/")  # proves credentials are registered
            self._call(self.client.apps.getApp, appId=target["app_id"], appVersion=target["app_version"])
            result["ok"] = True
        except TapisError as e:
            result["error"] = str(e)
        return result
