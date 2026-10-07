"""
HARP sweep server: a web UI + REST API for running the generate phase as
many TAPIS jobs across several systems.

Run with HTTPS:  python3 -m harp_server.serve   (see README for certificates)
"""

import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .campaign import CampaignManager, CampaignStore, campaign_view, ACTIVE_CAMPAIGN_STATUSES
from .sweep import SpecError
from .tapis_gateway import TapisError, TapisGateway

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
# The iScheduler profiling page lives at the repository root.
PROFILING_PAGE = os.environ.get(
    "HARP_PROFILING_PAGE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "profiling.html"))
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


def create_app(data_dir=None, poll_interval=None, login=TapisGateway.login, start_poller=True):
    data_dir = data_dir or os.environ.get("HARP_SERVER_DATA", os.path.expanduser("~/.harp_server"))
    poll_interval = poll_interval or int(os.environ.get("HARP_POLL_SECONDS", "15"))
    manager = CampaignManager(CampaignStore(data_dir), poll_interval=poll_interval)
    sessions = {}  # session token -> TapisGateway

    @asynccontextmanager
    async def lifespan(_app):
        if start_poller:
            manager.start()
        yield
        manager.stop()

    app = FastAPI(title="HARP sweep server", lifespan=lifespan)
    app.state.manager = manager
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

    @app.get("/api/tapis/systems/{system_id}")
    def system(system_id: str, gw: TapisGateway = Depends(gateway)):
        return tapis(gw.get_system, system_id)

    @app.get("/api/tapis/apps")
    def apps(gw: TapisGateway = Depends(gateway)):
        return tapis(gw.list_apps)

    @app.get("/api/tapis/files")
    def files(system_id: str, path: str = "/", gw: TapisGateway = Depends(gateway)):
        return tapis(gw.list_files, system_id, path)

    @app.post("/api/tapis/mkdir")
    def mkdir(body: dict = Body(...), gw: TapisGateway = Depends(gateway)):
        tapis(gw.mkdir, body["system_id"], body["path"])
        return {"ok": True}

    @app.post("/api/tapis/check")
    def check(body: dict = Body(...), gw: TapisGateway = Depends(gateway)):
        """Test every location the campaign will touch, through TAPIS."""
        storage = body.get("storage") or {}
        result = {"targets": [gw.check_target({"app_version": "1.0.0", **t}) for t in body.get("targets", [])],
                  "storage": None}
        if storage.get("system_id") and storage.get("path"):
            result["storage"] = gw.check_storage(storage["system_id"], storage["path"])
        result["ok"] = (all(t["ok"] for t in result["targets"]) and bool(result["targets"])
                        and bool(result["storage"] and result["storage"]["ok"]))
        return result

    # ------------------------------------------------------------ campaigns
    @app.post("/api/campaigns/preview")
    def preview(spec: dict = Body(...), gw: TapisGateway = Depends(gateway)):
        try:
            _, jobs, summary = manager.preview(spec)
        except (SpecError, ValueError) as e:
            raise HTTPException(400, str(e))
        return {"summary": summary,
                "jobs": [{"name": j["name"], "system_id": j["system_id"], "run_type": j["run_type"],
                          "combinations": [c["parameters"] for c in j["combinations"]]} for j in jobs]}

    @app.post("/api/campaigns")
    def create(spec: dict = Body(...), gw: TapisGateway = Depends(gateway)):
        try:
            c = manager.create(spec, gw)
        except (SpecError, ValueError) as e:
            raise HTTPException(400, str(e))
        except TapisError as e:
            raise HTTPException(502, f"TAPIS storage is not writable: {e}")
        return campaign_view(c)

    @app.get("/api/campaigns")
    def list_campaigns(gw: TapisGateway = Depends(gateway)):
        return [campaign_view(c, include_jobs=False) for c in manager.list(gw.username)]

    @app.get("/api/campaigns/{cid}")
    def get_campaign(cid: str, gw: TapisGateway = Depends(gateway)):
        return campaign_view(owned(cid, gw))

    @app.post("/api/campaigns/{cid}/cancel")
    def cancel(cid: str, gw: TapisGateway = Depends(gateway)):
        c = owned(cid, gw)
        if c["status"] not in ACTIVE_CAMPAIGN_STATUSES:
            raise HTTPException(409, f"campaign is already {c['status']}")
        manager.cancel(c)
        return campaign_view(c)

    @app.post("/api/campaigns/{cid}/resubmit")
    def resubmit(cid: str, gw: TapisGateway = Depends(gateway)):
        c = owned(cid, gw)
        if c["status"] in ACTIVE_CAMPAIGN_STATUSES:
            raise HTTPException(409, "wait for the campaign to finish before resubmitting")
        return {"resubmitted": manager.resubmit_failed(c)}

    @app.get("/api/campaigns/{cid}/csv")
    def download_csv(cid: str, gw: TapisGateway = Depends(gateway)):
        c = owned(cid, gw)
        path = (c.get("result") or {}).get("csv_path")
        if not path:
            raise HTTPException(404, "no merged profiling CSV yet")
        data = tapis(gw.download, c["spec"]["storage"]["system_id"], path)
        name = path.rsplit("/", 1)[-1]
        return Response(data, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ UI
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/profiling")
    @app.get("/profiling.html")
    def profiling_page():
        if not os.path.isfile(PROFILING_PAGE):
            raise HTTPException(404, "profiling.html not found")
        return FileResponse(PROFILING_PAGE, headers={"Content-Security-Policy": PROFILING_CSP})

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    return app


app = create_app() if os.environ.get("HARP_SERVER_NO_DEFAULT_APP") is None else None
