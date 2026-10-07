"""Every user sees only their own sweeps, jobs, profiled data and builds - per TAPIS tenant too."""
import pytest
from fastapi.testclient import TestClient

from harp_server.app import create_app
from fake_tapis import FakeGateway
from specs import euler_spec


@pytest.fixture
def server(tmp_path):
    gateways = {}

    def login(base_url, username=None, password=None, access_token=None):
        key = (base_url, username)
        if key not in gateways:
            gateways[key] = FakeGateway(username)
            gateways[key].base_url = base_url
        return gateways[key]

    app = create_app(data_dir=str(tmp_path), login=login, start_poller=False)
    return app, gateways


def client_for(app, user, tenant="https://icicle.tapis.io"):
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/api/login", json={"base_url": tenant, "username": user, "password": "x"}).status_code == 200
    return c


def test_users_and_tenants_only_see_their_own_work(server):
    app, gateways = server
    alice = client_for(app, "alice")
    manager = app.state.manager
    c = manager.create(euler_spec(), gateways[("https://icicle.tapis.io", "alice")])
    manager.tick()
    assert alice.get("/api/campaigns").json() and alice.get("/api/jobs").json()

    for other in (client_for(app, "bob"), client_for(app, "alice", tenant="https://tacc.tapis.io")):
        assert other.get("/api/campaigns").json() == []
        assert other.get("/api/jobs").json() == []
        assert other.get("/api/builds").json() == []
        assert other.get("/api/apps/harp-sweep-euler/profile").json()["total_rows"] == 0
        assert other.post(f"/api/campaigns/{c['id']}/cancel").status_code == 404
        r = other.post("/api/apps/harp-sweep-euler/training-data", json={"sweeps": [c["id"]]})
        assert r.status_code == 404

    assert alice.get("/api/campaigns").json()[0]["id"] == c["id"]   # still alice's


def test_sweeps_saved_before_tenant_owners_go_to_their_user(server):
    app, gateways = server
    manager = app.state.manager
    gw = FakeGateway("carol")
    gw.base_url = "https://icicle.tapis.io"
    c = manager.create(euler_spec(), gw)
    c["owner"] = "carol"          # how older versions stored it
    carol = client_for(app, "carol")
    assert [x["id"] for x in carol.get("/api/campaigns").json()] == [c["id"]]
    assert manager.campaigns[c["id"]]["owner"] == "carol@icicle.tapis.io"
