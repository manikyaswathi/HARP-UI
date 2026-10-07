import pytest
from fastapi.testclient import TestClient

from harp_server.app import create_app
from harp_server.tapis_gateway import TapisError
from fake_tapis import FakeGateway
from specs import euler_spec


class Ctx(tuple):
    """(client, manager, gateways), plus .app for building extra clients."""


@pytest.fixture
def ctx(tmp_path):
    gateways = {}

    def login(base_url, username=None, password=None, access_token=None):
        if access_token:  # fake tokens look like "token-<username>"
            if not access_token.startswith("token-"):
                raise TapisError("bad token")
            username = access_token[len("token-"):]
        elif password != "secret":
            raise TapisError("bad credentials")
        return gateways.setdefault(username, FakeGateway(username))

    app = create_app(data_dir=str(tmp_path), login=login, start_poller=False)
    client = TestClient(app, base_url="https://testserver")
    c = Ctx((client, app.state.manager, gateways))
    c.app = app
    return c


def _login(client, user="alice"):
    r = client.post("/api/login", json={"base_url": "https://fake.tapis.io", "username": user, "password": "secret"})
    assert r.status_code == 200, r.text


def test_requires_login(ctx):
    client, _, _ = ctx
    assert client.get("/api/campaigns").status_code == 401
    r = client.post("/api/login", json={"base_url": "https://fake.tapis.io", "access_token": "forged"})
    assert r.status_code == 401
    assert client.post("/api/login", json={"base_url": "https://fake.tapis.io", "access_token": "token-a"}).status_code == 200
    assert client.post("/api/login", json={"base_url": "http://insecure", "access_token": "token-a"}).status_code == 400


def test_login_with_credentials_sets_a_secure_session_cookie_only(ctx):
    client, _, _ = ctx
    r = client.post("/api/login", json={"base_url": "https://fake.tapis.io", "username": "alice", "password": "secret"})
    assert r.status_code == 200
    assert set(r.json()) == {"username", "base_url", "expires_at"}  # no token, no password
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    bad = client.post("/api/login", json={"base_url": "https://fake.tapis.io", "username": "alice", "password": "nope"})
    assert bad.status_code == 401
    assert client.post("/api/login", json={"base_url": "https://fake.tapis.io"}).status_code == 400


def test_credentials_are_refused_over_plain_http_from_other_hosts(ctx):
    creds = {"base_url": "https://fake.tapis.io", "username": "alice", "password": "secret"}
    remote = TestClient(ctx.app, base_url="http://harp.example.org", client=("203.0.113.7", 50000))
    r = remote.post("/api/login", json=creds)
    assert r.status_code == 403 and "https" in r.json()["detail"]
    local = TestClient(ctx.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    r = local.post("/api/login", json=creds)
    assert r.status_code == 200
    assert "secure" not in r.headers["set-cookie"].lower()  # dev on localhost


def test_security_headers(ctx):
    client, _, _ = ctx
    r = client.get("/")
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/api/campaigns").headers["cache-control"] == "no-store"


def test_expired_token_logs_out_and_pauses_campaigns(ctx):
    client, manager, gateways = ctx
    _login(client)
    c = client.post("/api/campaigns", json=euler_spec()).json()
    gateways["alice"].expires_at = 1  # long ago
    r = client.get("/api/campaigns")
    assert r.status_code == 401 and "expired" in r.json()["detail"]
    manager.tick()
    assert manager.campaigns[c["id"]]["status"] == "WAITING_FOR_LOGIN"
    assert gateways["alice"].submitted == []

    gateways["alice"].expires_at = None  # a fresh token
    _login(client)
    manager.tick()
    assert manager.campaigns[c["id"]]["status"] == "RUNNING"
    assert gateways["alice"].submitted


def test_ui_is_served(ctx):
    client, _, _ = ctx
    assert "HARP" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_browse_and_check_access(ctx):
    client, _, _ = ctx
    _login(client)
    assert [s["id"] for s in client.get("/api/tapis/systems").json()] == ["pitzer", "stampede", "storage"]
    r = client.post("/api/tapis/check", json={"targets": euler_spec()["targets"],
                                              "storage": {"system_id": "storage", "path": "/scratch"}}).json()
    assert r["ok"] is True
    r = client.post("/api/tapis/check", json={"targets": [{"system_id": "storage", "app_id": "x"}]}).json()
    assert r["ok"] is False


def test_preview_and_launch_and_download(ctx):
    client, manager, gateways = ctx
    _login(client)
    preview = client.post("/api/campaigns/preview", json=euler_spec()).json()
    assert preview["summary"]["jobs"] == 4 and preview["summary"]["total_runs"] == 14

    bad = client.post("/api/campaigns/preview", json=euler_spec(command=""))
    assert bad.status_code == 400

    c = client.post("/api/campaigns", json=euler_spec()).json()
    assert c["jobs_total"] == 4 and c["status"] == "RUNNING"

    gw = gateways["alice"]
    manager.tick()
    while any(j["uuid"] is None for j in manager.campaigns[c["id"]]["jobs"]):
        for u in gw.submitted:
            gw.finish(u, b"run_type,walltime\nSD,1.0\n")
        manager.tick()
    for u in gw.submitted:
        gw.finish(u, b"run_type,walltime\nSD,1.0\n")
    manager.tick()

    view = client.get(f"/api/campaigns/{c['id']}").json()
    assert view["status"] == "DONE" and view["jobs_done"] == 4
    csv_resp = client.get(f"/api/campaigns/{c['id']}/csv")
    assert csv_resp.status_code == 200
    assert csv_resp.text.splitlines()[0] == "run_type,walltime"
    assert len(csv_resp.text.splitlines()) == 5


def test_users_only_see_their_own_campaigns(ctx):
    client, _, _ = ctx
    _login(client, "alice")
    c = client.post("/api/campaigns", json=euler_spec()).json()
    _login(client, "bob")
    assert client.get("/api/campaigns").json() == []
    assert client.get(f"/api/campaigns/{c['id']}").status_code == 404


def test_profiling_page_is_served_with_its_own_csp(ctx):
    client, _, _ = ctx
    r = client.get("/profiling.html")
    assert r.status_code == 200 and "Profiling" in r.text
    csp = r.headers["content-security-policy"]
    assert "fonts.googleapis.com" in csp and "'unsafe-inline'" in csp
    assert "unsafe-inline" not in client.get("/").headers["content-security-policy"].split("script-src")[1].split(";")[0]


def _finish_all(manager, gw, csv_for):
    manager.tick()
    for _ in range(20):
        for u in gw.submitted:
            if gw.jobs[u]["status"] != "FINISHED":
                gw.finish(u, csv_for(u))
        manager.tick()
        if all(j["uuid"] for c in manager.campaigns.values() for j in c["jobs"]) and \
           all(c["status"] not in ("RUNNING", "FINALIZING") for c in manager.campaigns.values()):
            break


def test_app_profile_merges_all_campaigns_of_an_app(ctx):
    client, manager, gateways = ctx
    _login(client)
    client.post("/api/campaigns", json=euler_spec(name="sweep-a"))
    client.post("/api/campaigns", json=euler_spec(name="sweep-b"))
    gw = gateways["alice"]
    _finish_all(manager, gw, lambda u: b"run_type,run_n,walltime\nSD,10,0.5\n")

    r = client.get("/api/apps/harp-sweep-euler/profile").json()
    assert len(r["campaigns"]) == 2 and all(c["status"] == "DONE" for c in r["campaigns"])
    assert r["columns"] == ["campaign", "system", "run_type", "run_n", "walltime"]
    assert r["total_rows"] == 8 and {row["campaign"] for row in r["rows"]} == {"sweep-a", "sweep-b"}

    csv_text = client.get("/api/apps/harp-sweep-euler/profile.csv").text.splitlines()
    assert csv_text[0] == "campaign,system,run_type,run_n,walltime" and len(csv_text) == 9

    assert client.get("/api/apps/other-app/profile").json()["total_rows"] == 0
    assert client.get("/api/apps/other-app/profile.csv").status_code == 404


def test_runs_and_profile_data_pages_are_served(ctx):
    client, _, _ = ctx
    for path in ("/runs", "/profile-data.html"):
        r = client.get(path)
        assert r.status_code == 200 and "iScheduler" in r.text, path


def test_rows_are_tagged_with_the_system_that_ran_them(ctx):
    client, manager, gateways = ctx
    _login(client)
    client.post("/api/campaigns", json=euler_spec(name="hw"))
    gw = gateways["alice"]
    def csv_for(u):
        job = gw.jobs[u]["request"]["name"]
        return f"run_config,run_type,walltime\n{job}.run-0.iteration-0,SD,1.0\n".encode()
    _finish_all(manager, gw, csv_for)
    rows = client.get("/api/apps/harp-sweep-euler/profile").json()["rows"]
    assert {r["system"] for r in rows} == {"pitzer", "stampede"}
    view = client.get(f"/api/campaigns/{list(manager.campaigns)[0]}").json()
    assert [h["system_id"] for h in view["hardware"]] == ["pitzer", "stampede"]
    assert {j["target"] for j in view["jobs"]} == {"t0", "t1"}


def test_app_profile_shows_finished_jobs_while_sweep_runs(ctx):
    client, manager, gateways = ctx
    _login(client)
    spec = euler_spec(name="live")
    for t in spec["targets"]:
        t["max_concurrent_jobs"] = 10
    c = client.post("/api/campaigns", json=spec).json()
    manager.tick()
    gw = gateways["alice"]
    gw.finish(gw.submitted[0], b"run_type,walltime\nSD,0.5\nSD,0.6\n")
    manager.tick()
    assert manager.campaigns[c["id"]]["status"] == "RUNNING"
    r = client.get("/api/apps/harp-sweep-euler/profile").json()
    assert r["total_rows"] == 2 and r["campaigns"][0]["status"] == "RUNNING"
