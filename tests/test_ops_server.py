"""Profile selection and server assembly without a desktop session."""

import asyncio
import sys

import pytest

from openowl.ops import ALL_PROFILES, OPS_PROFILES, parse_profiles


def test_parse_profiles():
    assert parse_profiles("") == set(ALL_PROFILES)
    assert parse_profiles("ops") == set(OPS_PROFILES)
    assert parse_profiles("desktop, proxmox") == {"desktop", "proxmox"}
    with pytest.raises(ValueError):
        parse_profiles("telepathy")


def test_ops_only_server_registers_tools_without_desktop_imports(ops_config):
    ops_config()
    from openowl.server import _build_server

    sys.modules.pop("pyautogui", None)
    mcp = _build_server(set(OPS_PROFILES))
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    expected = {
        "run_command", "read_file", "write_file", "edit_file", "find_files", "http_request",
        "ssh_run", "ssh_transfer", "proxmox", "guest_exec", "list_services", "services_status",
        "homelab_overview", "service_api", "arr", "qbittorrent", "plex", "describe_image",
    }
    assert expected <= names
    assert "look" not in names  # needs the desktop profile
    assert "screenshot" not in names
    assert "pyautogui" not in sys.modules
    assert "list_services" in (mcp.instructions or "")


def test_list_services_tool_runs(ops_config):
    ops_config({"ssh": {"slim": "root@10.0.0.6"},
                "services": {"romarr": {"kind": "arr", "url": "http://g"}}})
    from openowl.ops.services import do_list_services

    out = do_list_services()
    assert "mode: read" in out and "slim" in out and "romarr" in out and "arr/romarr" in out


def test_example_config_parses():
    from importlib import resources

    from openowl.ops.config import parse_config

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    text = resources.files("openowl.ops").joinpath("config.example.toml").read_text(encoding="utf-8")
    cfg = parse_config(tomllib.loads(text))
    assert cfg.proxmox.ssh_host == "pve1"
    assert cfg.services["romarr"].api_version == "v1"
    assert cfg.vision.configured


def test_remote_mode_ignores_local_mode_env(tmp_path, monkeypatch):
    from openowl.ops.config import get_config, set_config
    from openowl.server import _apply_remote_mode

    cfg = tmp_path / "c.toml"
    cfg.write_text('[policy]\nmode = "operate"\nremote_mode = "read"\n', encoding="utf-8")
    monkeypatch.setenv("OPEN_EYES_CONFIG", str(cfg))
    monkeypatch.setenv("OPEN_EYES_MODE", "admin")
    try:
        _apply_remote_mode()
        assert get_config().policy.mode == "read"
        monkeypatch.setenv("OPEN_EYES_REMOTE_MODE", "operate")
        _apply_remote_mode()
        assert get_config().policy.mode == "operate"
    finally:
        set_config(None)
