"""
HARP sweep server: the REST API behind the profiling page (profiling.html),
running the generate phase as many TAPIS jobs across several systems.

Run with HTTPS:  python3 -m harp_server.serve   (see README for certificates)
"""

import csv
import io
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from .campaign import ACTIVE_CAMPAIGN_STATUSES, PROFILE_FILE_NAME, CampaignManager, CampaignStore, campaign_view
from .build import BuildManager, build_view, standardize, to_csv
from .plan import build_plan, harp_app, is_gpu
from .tapis_gateway import TapisError, TapisGateway

# The profiling page lives at the repository root.
PROFILING_PAGE = os.environ.get(
    "HARP_PROFILING_PAGE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "profiling.html"))
MAX_PROFILE_ROWS = 5000
# That page is a single file with inline script/style and Google Fonts.
PROFILING_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                 "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                 "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
                 "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
SESSION_COOKIE = "harp_session"
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                                "form-action 'self'; base-uri 'none'"),
}


def is_https(request: Request) -> bool:
    # Behind a reverse proxy, run uvicorn with --proxy-headers so the
    # X-Forwarded-Proto header sets the scheme.
    return request.url.scheme == "https"


def is_local(request: Request) -> bool:
    return bool(request.client) and request.client.host in LOCAL_CLIENTS


def create_app(data_dir=None, poll_interval=None, login=TapisGateway.login, start_poller=True, build_python=None):
    data_dir = data_dir or os.environ.get("HARP_SERVER_DATA", os.path.expanduser("~/.harp_server"))
    poll_interval = poll_interval or int(os.environ.get("HARP_POLL_SECONDS", "15"))
    manager = CampaignManager(CampaignStore(data_dir), poll_interval=poll_interval)
    builds = BuildManager(data_dir, python=build_python)
    sessions = {}  # session token -> TapisGateway

    @asynccontextmanager
    async def lifespan(_app):
        if start_poller:
            manager.start()
        yield
        manager.stop()

    app = FastAPI(title="HARP sweep server", lifespan=lifespan)
    app.state.manager = manager
    app.state.builds = builds
    allow_http = os.environ.get("HARP_ALLOW_HTTP") == "1"

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            if name not in response.headers:  # a route may set a stricter/looser one
                response.headers[name] = value
        if is_https(request):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def gateway(request: Request) -> TapisGateway:
        key = request.cookies.get(SESSION_COOKIE, "")
        gw = sessions.get(key)
        if gw is None:
            raise HTTPException(401, "log in to TAPIS first")
        if gw.expired():
            sessions.pop(key, None)
            raise HTTPException(401, "your TAPIS token expired, log in again")
        return gw

    def tapis(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TapisError as e:
            raise HTTPException(502, f"TAPIS: {e}")

    def owned(cid, gw):
        c = manager.get(cid, gw.username)
        if c is None:
            raise HTTPException(404, "campaign not found")
        return c

    # ------------------------------------------------------------ auth
    @app.post("/api/login")
    def login_route(request: Request, response: Response, body: dict = Body(...)):
        """The page sends the TAPIS credentials; the server gets the token
        with tapipy and keeps it. The browser only receives a session cookie."""
        if not (is_https(request) or is_local(request) or allow_http):
            raise HTTPException(403, "log in over https:// - credentials are never accepted over plain http")
        base_url = (body.get("base_url") or "").strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise HTTPException(400, "base_url must be an https TAPIS tenant URL, e.g. https://icicle.tapis.io")
        username, password = (body.get("username") or "").strip(), body.get("password") or ""
        access_token = (body.get("access_token") or "").strip()
        if not access_token and not (username and password):
            raise HTTPException(400, "enter your TAPIS username and password (or an access token)")
        try:
            gw = login(base_url, username=username or None, password=password or None,
                       access_token=access_token or None)
        except TapisError as e:
            raise HTTPException(401, str(e))
        token = secrets.token_urlsafe(32)
        sessions[token] = gw
        manager.register_gateway(gw)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=is_https(request))
        return {"username": gw.username, "base_url": gw.base_url, "expires_at": gw.expires_at}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(gw: TapisGateway = Depends(gateway)):
        return {"username": gw.username, "base_url": gw.base_url, "expires_at": gw.expires_at}

    # ----------------------------------------------------------- TAPIS browse
    @app.get("/api/tapis/systems")
    def systems(gw: TapisGateway = Depends(gateway)):
        return tapis(gw.list_systems)

    @app.get("/api/tapis/exec-systems")
    def exec_systems(gw: TapisGateway = Depends(gateway)):
        """Systems the user can run jobs on, each with its queues (TAPIS batchLogicalQueues)."""
        out = tapis(gw.exec_systems)
        for s in out:
            for q in s.get("queues") or []:
                q["gpu"] = is_gpu(q)
        return out

    @app.get("/api/tapis/apps")
    def apps(gw: TapisGateway = Depends(gateway)):
        """The user's TAPIS apps with their sweep parameters (from the app notes)."""
        return [harp_app(a) for a in tapis(gw.list_apps)]

    # ------------------------------------------------------------ campaigns
    def plan_or_400(plan, gw):
        if not isinstance(plan, dict):
            raise HTTPException(400, "send the plan as a JSON object")
        if not plan.get("sweeps"):
            raise HTTPException(400, "the plan has no sweeps")
        return tapis(build_plan, plan, gw)

    def checked(item):
        """A built sweep, also run through the campaign validator and splitter."""
        if "error" in item:
            return {"name": item["name"], "ok": False, "error": item["error"], "targets": item["targets"]}
        spec = item["spec"]
        if not (spec["storage"]["system_id"] and spec["storage"]["path"].startswith("/")):
            # the results folder is reported on its own (storage_error); check the sweep without it
            spec = {**spec, "storage": {"system_id": "unset", "path": "/unset"}}
        try:
            _, _, summary = manager.preview(spec)
        except ValueError as e:
            return {"name": item["name"], "ok": False, "error": str(e), "targets": item["targets"]}
        return {"name": item["name"], "ok": True, "summary": summary, "targets": item["targets"],
                "sweep_name": item["spec"]["name"]}

    @app.post("/api/plan/preview")
    def preview_plan(plan: dict = Body(...), gw: TapisGateway = Depends(gateway)):
        """Check every sweep of the plan against TAPIS (apps, systems, queue limits).
        Each comes back with its summary and the exact hardware each job will ask for."""
        built, storage_error = plan_or_400(plan, gw)
        sweeps = [checked(b) for b in built]
        return {"ok": not storage_error and all(x["ok"] for x in sweeps),
                "storage_error": storage_error, "sweeps": sweeps}

    @app.post("/api/plan/submit")
    def submit_plan(plan: dict = Body(...), gw: TapisGateway = Depends(gateway)):
        """Launch every sweep of the plan. Nothing is submitted unless every sweep is valid."""
        built, storage_error = plan_or_400(plan, gw)
        if storage_error:
            raise HTTPException(400, storage_error)
        for x in map(checked, built):
            if not x["ok"]:
                raise HTTPException(400, f"sweep {x['name']}: {x['error']}")
        created = []
        for b in built:
            try:
                created.append(campaign_view(manager.create(b["spec"], gw), include_jobs=False))
            except TapisError as e:
                raise HTTPException(502, f"TAPIS refused sweep {b['name']} after "
                                         f"{len(created)} were submitted: {e}")
        return created

    @app.get("/api/jobs")
    def all_jobs(gw: TapisGateway = Depends(gateway)):
        """Every TAPIS job of every sweep, for the jobs table."""
        rows = []
        for c in manager.list(gw.username):
            target_of = {t["key"]: t for t in c["spec"]["targets"]}
            for j in c["jobs"]:
                t = target_of.get(j["target"], {})
                rows.append({"name": j["name"], "sweep": c["spec"]["name"], "sweep_id": c["id"],
                             "app_id": t.get("app_id"), "system_id": j["system_id"],
                             "queue": t.get("queue"), "cores_per_node": t.get("cores_per_node"),
                             "memory_mb": t.get("memory_mb"), "run_type": j["run_type"],
                             "combinations": len(j["combinations"]), "status": j["status"],
                             "runs_total": len(j["combinations"]) * c["spec"]["repetitions"],
                             "progress": j.get("progress"),
                             "uuid": j["uuid"], "error": j["error"],
                             "submitted_at": j["submitted_at"], "ended_at": j["ended_at"]})
        return rows

    @app.get("/api/campaigns")
    def list_campaigns(gw: TapisGateway = Depends(gateway)):
        return [campaign_view(c, include_jobs=False) for c in manager.list(gw.username)]

    @app.post("/api/campaigns/{cid}/cancel")
    def cancel(cid: str, gw: TapisGateway = Depends(gateway)):
        c = owned(cid, gw)
        if c["status"] not in ACTIVE_CAMPAIGN_STATUSES:
            raise HTTPException(409, f"campaign is already {c['status']}")
        manager.cancel(c)
        return campaign_view(c)

    # ------------------------------------------------------- per-app results
    csv_cache = {}  # (system, path) -> bytes; a merged campaign CSV never changes

    def app_campaigns(app_id, gw):
        return [c for c in manager.list(gw.username)
                if app_id in {t["app_id"] for t in c["spec"]["targets"]}]

    def hardware_label(t):
        """One hardware configuration as text, e.g. "pitzer/serial · 40 cores · 156 GB"."""
        parts = [t["system_id"] + (f"/{t['queue']}" if t.get("queue") else "")]
        if t.get("cores_per_node"):
            parts.append(f"{t['cores_per_node']} cores")
        if t.get("memory_mb"):
            parts.append(f"{round(t['memory_mb'] / 1024)} GB")
        if "--nv" in (t.get("container_args") or ""):
            parts.append("GPU")
        return " · ".join(parts)

    def app_rows(app_id, gw, campaign_ids=None):
        """Every profiling row collected for an app, across all its campaigns (or the ones given).
        Each row also says what its job was given: sys_alloc_cores, sys_alloc_mem_mb."""
        columns, rows, missing = ["campaign", "system", "hardware", "sys_alloc_cores", "sys_alloc_mem_mb"], [], []
        for c in app_campaigns(app_id, gw):
            if campaign_ids is not None and c["id"] not in campaign_ids:
                continue
            # run_config is "<job name>.run-<i>.iteration-<r>"; map it back to the job's system
            job_system = {j["name"]: j["system_id"] for j in c["jobs"]}
            targets = {t["key"]: t for t in c["spec"]["targets"]}
            job_hw = {j["name"]: hardware_label(targets[j["target"]]) for j in c["jobs"] if j["target"] in targets}
            job_t = {j["name"]: targets.get(j["target"], {}) for j in c["jobs"]}
            system = c["spec"]["storage"]["system_id"]
            merged = (c.get("result") or {}).get("csv_path")
            # Once a sweep is merged read its one CSV; while it runs, read the CSV
            # of every job that has finished so progress shows up straight away.
            paths = [merged] if merged else [f"{j['archive_dir']}/{PROFILE_FILE_NAME}"
                                             for j in c["jobs"] if j["status"] == "FINISHED"]
            for path in paths:
                key = (system, path)
                if key not in csv_cache:
                    try:
                        csv_cache[key] = gw.download(*key)
                    except TapisError as e:
                        missing.append({"campaign": c["spec"]["name"], "error": str(e)})
                        continue
                reader = csv.DictReader(io.StringIO(csv_cache[key].decode("utf-8")))
                for name in reader.fieldnames or []:
                    if name not in columns:
                        columns.append(name)
                for r in reader:
                    job = (r.get("run_config") or "").split(".run-")[0]
                    t = job_t.get(job, {})
                    rows.append({"campaign": c["spec"]["name"], "system": job_system.get(job, ""),
                                 "hardware": job_hw.get(job, ""), "sys_alloc_cores": t.get("cores_per_node", ""),
                                 "sys_alloc_mem_mb": t.get("memory_mb", ""), **r})
        if "walltime" in columns:  # keep walltime last, like the CSVs
            columns.remove("walltime")
            columns.append("walltime")
        return columns, rows, missing

    @app.get("/api/apps/{app_id}/profile")
    def app_profile(app_id: str, gw: TapisGateway = Depends(gateway)):
        columns, rows, missing = app_rows(app_id, gw)
        return {"app_id": app_id,
                "campaigns": [campaign_view(c, include_jobs=False) for c in app_campaigns(app_id, gw)],
                "columns": columns, "rows": rows[:MAX_PROFILE_ROWS], "total_rows": len(rows),
                "missing": missing}

    @app.get("/api/apps/{app_id}/profile.csv")
    def app_profile_csv(app_id: str, gw: TapisGateway = Depends(gateway)):
        columns, rows, _ = app_rows(app_id, gw)
        if not rows:
            raise HTTPException(404, "no profiling data for this app yet")
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in app_id)
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{safe}_profile.csv"'})

    # ------------------------------------------------------------- build phase
    def training_data(app_id, sweeps, gw):
        if not isinstance(sweeps, list) or not sweeps:
            raise HTTPException(400, "pick at least one sweep")
        mine = {c["id"] for c in app_campaigns(app_id, gw)}
        unknown = [x for x in sweeps if x not in mine]
        if unknown:
            raise HTTPException(404, f"no sweep {unknown[0]!r} for this app")
        columns, rows, missing = app_rows(app_id, gw, set(sweeps))
        header, table, report = standardize(columns, rows)
        report["unreadable"] = missing
        return header, table, report

    @app.post("/api/apps/{app_id}/training-data")
    def training_data_preview(app_id: str, body: dict = Body(...), gw: TapisGateway = Depends(gateway)):
        """Pool the chosen sweeps and standardize them for the build phase: what it would train on."""
        header, table, report = training_data(app_id, body.get("sweeps"), gw)
        return {"columns": header, "sample": table[:8], **report}

    @app.get("/api/apps/{app_id}/training-data.csv")
    def training_data_csv(app_id: str, sweeps: str = "", gw: TapisGateway = Depends(gateway)):
        header, table, _ = training_data(app_id, [x for x in sweeps.split(",") if x], gw)
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in app_id)
        return Response(to_csv(header, table), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{safe}_training.csv"'})

    @app.post("/api/apps/{app_id}/builds")
    def start_build(app_id: str, body: dict = Body(...), gw: TapisGateway = Depends(gateway)):
        """Standardize the chosen sweeps and build models with the HARP pipeline; results go to TAPIS."""
        storage = body.get("storage") or {}
        if not storage.get("system_id") or not str(storage.get("path", "")).startswith("/"):
            raise HTTPException(400, "pick a TAPIS system and an absolute folder for the models")
        header, table, report = training_data(app_id, body.get("sweeps"), gw)
        if not report["ready"]:
            raise HTTPException(400, "not enough data to build: " + "; ".join(report["problems"]))
        label = next((a["label"] for a in map(harp_app, tapis(gw.list_apps)) if a["id"] == app_id), app_id)
        b = builds.start(gw.username, app_id, label, body["sweeps"], header, table, report,
                         {"system_id": storage["system_id"], "path": storage["path"].strip()}, gw)
        return build_view(b)

    @app.get("/api/builds")
    def list_builds(app_id: str = "", gw: TapisGateway = Depends(gateway)):
        return [build_view(b) for b in builds.list(gw.username, app_id or None)]

    # ------------------------------------------------------------------ page
    @app.get("/")
    @app.get("/profiling")
    @app.get("/profiling.html")
    def profiling_page():
        if not os.path.isfile(PROFILING_PAGE):
            raise HTTPException(404, "profiling.html not found")
        return FileResponse(PROFILING_PAGE, headers={"Content-Security-Policy": PROFILING_CSP})

    return app


app = create_app() if os.environ.get("HARP_SERVER_NO_DEFAULT_APP") is None else None
