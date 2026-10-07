import sys
import time
import types

import pytest

from harp_server.tapis_gateway import TapisError, TapisGateway


class FakeTapis:
    """Mimics tapipy.tapis.Tapis(base_url, access_token=...)."""
    valid = {"good-token": "alice"}
    passwords = {"alice": "pw"}
    last = None

    def __init__(self, base_url, access_token=None, username=None, password=None):
        FakeTapis.last = self
        self.base_url = base_url
        self.username, self.password = username, password
        self.refresh_token = None
        self.access_token = types.SimpleNamespace(claims={"exp": time.time() + 3600}) if access_token else None
        user = self.valid.get(access_token)

        def get_userinfo():
            if user is None:
                raise RuntimeError("401 invalid JWT")
            return types.SimpleNamespace(username=user)
        self.authenticator = types.SimpleNamespace(get_userinfo=get_userinfo)

    def get_tokens(self):
        if self.passwords.get(self.username) != self.password:
            raise RuntimeError(f"Invalid username/password combination for {self.username}:{self.password}")
        self.access_token = types.SimpleNamespace(claims={"exp": time.time() + 4 * 3600})


@pytest.fixture(autouse=True)
def fake_tapipy(monkeypatch):
    mod = types.ModuleType("tapipy.tapis")
    mod.Tapis = FakeTapis
    monkeypatch.setitem(sys.modules, "tapipy", types.ModuleType("tapipy"))
    monkeypatch.setitem(sys.modules, "tapipy.tapis", mod)


def test_login_takes_username_from_tapis_not_from_the_client():
    gw = TapisGateway.login("https://icicle.tapis.io", access_token="good-token")
    assert gw.username == "alice"
    assert not gw.expired()
    assert gw.expires_at > time.time()


def test_forged_token_is_rejected():
    with pytest.raises(TapisError, match="login failed"):
        TapisGateway.login("https://icicle.tapis.io", access_token="forged")


def test_password_login_gets_token_and_forgets_password():
    gw = TapisGateway.login("https://icicle.tapis.io", username="alice", password="pw")
    assert gw.username == "alice"
    assert FakeTapis.last.password is None
    assert gw.expires_at > time.time() + 3 * 3600


def test_wrong_password_error_does_not_echo_the_password():
    with pytest.raises(TapisError) as e:
        TapisGateway.login("https://icicle.tapis.io", username="alice", password="hunter2")
    assert "hunter2" not in str(e.value) and "***" in str(e.value)


def test_refresh_token_extends_the_session():
    gw = TapisGateway.login("https://icicle.tapis.io", username="alice", password="pw")
    FakeTapis.last.refresh_token = types.SimpleNamespace(claims={"exp": time.time() + 24 * 3600})
    assert gw.expires_at > time.time() + 23 * 3600


def test_expired():
    gw = TapisGateway(None, "a", "u", expires_at=time.time() + 30)
    assert gw.expired()  # inside the 60s safety margin
    assert not TapisGateway(None, "a", "u").expired()


def test_list_apps_flattens_tapis_results():
    gw = TapisGateway.login("https://icicle.tapis.io", username="alice", password="pw")
    app = types.SimpleNamespace(
        id="harp-sweep-yolo", version="1.0.0", containerImage="docker://x", runtime="SINGULARITY",
        description="YOLO", notes=types.SimpleNamespace(harp=types.SimpleNamespace(command="python3 t.py {m}")),
        jobAttributes=types.SimpleNamespace(parameterSet=types.SimpleNamespace(
            appArgs=[types.SimpleNamespace(name="spec", arg="x")])))
    gw.client.apps = types.SimpleNamespace(getApps=lambda **kw: [app])
    [out] = gw.list_apps()
    assert out["notes"] == {"harp": {"command": "python3 t.py {m}"}}
    assert out["app_args"] == [{"name": "spec", "arg": "x"}]
