"""
HARP sweep server: a web UI + REST API for running the generate phase as
many TAPIS jobs across several systems.

Run with:  uvicorn harp_server.app:app --host 0.0.0.0 --port 8000
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
SESSION_COOKIE = "harp_session"


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

    def gateway(request: Request) -> TapisGateway:
        gw = sessions.get(request.cookies.get(SESSION_COOKIE, ""))
        if gw is None:
            raise HTTPException(401, "log in to TAPIS first")
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
    def login_route(response: Response, body: dict = Body(...)):
        base_url = (body.get("base_url") or "").rstrip("/")
        if not base_url.startswith("https://"):
            raise HTTPException(400, "base_url must be an https TAPIS tenant URL, e.g. https://icicle.tapis.io")
        try:
            gw = login(base_url, username=body.get("username"), password=body.get("password"),
                       access_token=body.get("access_token"))
        except TapisError as e:
            raise HTTPException(401, str(e))
        token = secrets.token_urlsafe(32)
        sessions[token] = gw
        manager.register_gateway(gw)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict")
        return {"username": gw.username, "base_url": gw.base_url}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(gw: TapisGateway = Depends(gateway)):
        return {"username": gw.username, "base_url": gw.base_url}

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

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    return app


app = create_app() if os.environ.get("HARP_SERVER_NO_DEFAULT_APP") is None else None
