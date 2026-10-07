import sys
import time
import types

import pytest

from harp_server.tapis_gateway import TapisError, TapisGateway


class FakeTapis:
    """Mimics tapipy.tapis.Tapis(base_url, access_token=...)."""
    valid = {"good-token": "alice"}

    def __init__(self, base_url, access_token):
        self.base_url = base_url
        self.access_token = types.SimpleNamespace(claims={"exp": time.time() + 3600})
        user = self.valid.get(access_token)

        def get_userinfo():
            if user is None:
                raise RuntimeError("401 invalid JWT")
            return types.SimpleNamespace(username=user)
        self.authenticator = types.SimpleNamespace(get_userinfo=get_userinfo)


@pytest.fixture(autouse=True)
def fake_tapipy(monkeypatch):
    mod = types.ModuleType("tapipy.tapis")
    mod.Tapis = FakeTapis
    monkeypatch.setitem(sys.modules, "tapipy", types.ModuleType("tapipy"))
    monkeypatch.setitem(sys.modules, "tapipy.tapis", mod)


def test_login_takes_username_from_tapis_not_from_the_client():
    gw = TapisGateway.login("https://icicle.tapis.io", "good-token")
    assert gw.username == "alice"
    assert not gw.expired()
    assert gw.expires_at > time.time()


def test_forged_token_is_rejected():
    with pytest.raises(TapisError, match="rejected"):
        TapisGateway.login("https://icicle.tapis.io", "forged")


def test_expired():
    gw = TapisGateway(None, "a", "u", expires_at=time.time() + 30)
    assert gw.expired()  # inside the 60s safety margin
    assert not TapisGateway(None, "a", "u").expired()
