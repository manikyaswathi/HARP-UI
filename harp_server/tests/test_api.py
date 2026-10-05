import pytest
from fastapi.testclient import TestClient

from harp_server.app import create_app
from harp_server.tapis_gateway import TapisError
from fake_tapis import FakeGateway
from specs import euler_spec


@pytest.fixture
def ctx(tmp_path):
    gateways = {}

    def login(base_url, username=None, password=None, access_token=None):
        if password != "secret":
            raise TapisError("bad credentials")
        return gateways.setdefault(username, FakeGateway(username))

    app = create_app(data_dir=str(tmp_path), login=login, start_poller=False)
    client = TestClient(app)
    return client, app.state.manager, gateways


def _login(client, user="alice"):
    r = client.post("/api/login", json={"base_url": "https://fake.tapis.io", "username": user, "password": "secret"})
    assert r.status_code == 200, r.text


def test_requires_login(ctx):
    client, _, _ = ctx
    assert client.get("/api/campaigns").status_code == 401
    r = client.post("/api/login", json={"base_url": "https://fake.tapis.io", "username": "a", "password": "x"})
    assert r.status_code == 401
    assert client.post("/api/login", json={"base_url": "http://insecure", "username": "a", "password": "secret"}).status_code == 400


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
