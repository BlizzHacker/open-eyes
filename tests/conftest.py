"""Shared fixtures for the ops toolkit tests."""

import pytest

from openowl.ops import proxmox, services, vision
from openowl.ops.config import parse_config, set_config


@pytest.fixture
def ops_config():
    """Install a config built from a dict; reset everything afterwards."""

    def _install(data: dict | None = None, mode: str = "read"):
        data = dict(data or {})
        data.setdefault("policy", {}).setdefault("mode", mode)
        cfg = parse_config(data, path="test-config.toml")
        set_config(cfg)
        return cfg

    yield _install
    set_config(None)
    proxmox.set_transport(None)
    services.transport_factory = None
    vision.transport_factory = None


@pytest.fixture(autouse=True)
def _no_mode_env(monkeypatch):
    monkeypatch.delenv("OPEN_EYES_MODE", raising=False)
    monkeypatch.delenv("OPEN_EYES_PROFILES", raising=False)
