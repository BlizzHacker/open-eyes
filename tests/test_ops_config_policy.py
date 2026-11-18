"""Config parsing, secret resolution and the permission policy."""

import json
import sys

import pytest

from openowl.ops import policy
from openowl.ops.config import ConfigError, load_config, parse_config, resolve_secret


def test_missing_config_file_gives_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert not cfg.loaded
    assert cfg.policy.mode == "read"
    assert cfg.services == {}


def test_parses_services_with_kind_defaults(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[services.radarr]\nkind = "arr"\nurl = "http://r:7878/"\napi_key = "k"\n'
        '[services.romarr]\nkind = "arr"\nurl = "http://g:7878"\n'
        '[services.plex]\nkind = "plex"\nurl = "http://p:32400"\n'
        '[services.site]\nurl = "https://x.test"\ntoken_env = "T"\n'
        '[ssh.slim]\ntarget = "root@10.0.0.6"\n'
        '[ssh.thic]\ntarget = "root@10.0.0.5"\njump = "slim"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    radarr = cfg.services["radarr"]
    assert (radarr.app, radarr.api_version, radarr.auth) == ("radarr", "v3", "x-api-key")
    assert radarr.url == "http://r:7878"
    assert radarr.health == "/api/v3/system/status"
    assert cfg.services["romarr"].api_version == "v1"
    assert cfg.services["plex"].auth == "header:X-Plex-Token"
    assert cfg.services["site"].auth == "bearer"
    assert cfg.ssh["thic"].jump == "slim"


def test_invalid_mode_rejected():
    with pytest.raises(ConfigError):
        parse_config({"policy": {"mode": "god"}})


def test_env_overrides_mode(monkeypatch):
    monkeypatch.setenv("OPEN_EYES_MODE", "admin")
    assert parse_config({"policy": {"mode": "read"}}).policy.mode == "admin"


def test_service_requires_url():
    with pytest.raises(ConfigError, match="url"):
        parse_config({"services": {"x": {"kind": "http"}}})


def test_secret_forms(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "from-env")
    key_file = tmp_path / "key.txt"
    key_file.write_text("from-file\nignored\n", encoding="utf-8")
    assert resolve_secret({"k": "lit"}, "k", "s") == "lit"
    assert resolve_secret({"k_env": "MY_KEY"}, "k", "s") == "from-env"
    assert resolve_secret({"k_file": str(key_file)}, "k", "s") == "from-file"
    cmd = f'"{sys.executable}" -c "print(123)"'
    assert resolve_secret({"k_cmd": cmd}, "k", "cmd-test") == "123"
    assert resolve_secret({}, "k", "s") == ""


def test_missing_env_secret_is_explained(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(ConfigError, match="NOT_SET_ANYWHERE"):
        resolve_secret({"k_env": "NOT_SET_ANYWHERE"}, "k", "services.x")


@pytest.mark.parametrize("command", [
    "ls -la /etc",
    "df -h | grep sd",
    "pct list",
    "pct config 105",
    "qm status 9000",
    "systemctl status radarr --no-pager",
    "journalctl -u romarr -n 50 2>&1",
    "git log --oneline -5",
    "docker ps",
    "docker compose ps",
    "curl -s http://localhost:7878/ping",
    "pvesh get /cluster/resources --output-format json",
    "Get-ChildItem C:\\MoveWeight",
    "zpool status",
])
def test_readonly_commands_allowed(command):
    assert policy.is_readonly_command(command)


@pytest.mark.parametrize("command", [
    "rm file",
    "ls; rm -rf /",
    "cat a && reboot",
    "echo hi > /etc/passwd",
    "ls $(whoami)",
    "pct start 105",
    "pct exec 105 -- bash",
    "systemctl restart radarr",
    "git push",
    "git branch -D main",
    "docker rm x",
    "curl -X POST http://x",
    "curl -o /tmp/x http://x",
    "sed -i s/a/b/ f",
    "find / -delete",
    "find . -exec rm {} ;",
    "awk 'BEGIN{system(\"id\")}'",
    "sleep 10 &",
    "python3 -c 'print(1)'",
])
def test_non_readonly_commands_refused(command):
    assert not policy.is_readonly_command(command)


@pytest.mark.parametrize("command,reason", [
    ("rm -rf /var/lib/radarr", "rm"),
    ("pct destroy 105", "destroy"),
    ("zfs destroy tank/x", "ZFS"),
    ("dd if=/dev/zero of=/dev/sda", "dd"),
    ("git push --force origin main", "force"),
    ("git reset --hard origin/main", "git"),
    ("sudo reboot", "power"),
    ("mkfs.ext4 /dev/sdb1", "mkfs"),
    ("pvesh delete /nodes/x/lxc/105", "pvesh"),
    ("pvesh create /nodes/x/lxc/105/snapshot/s/rollback", "pvesh"),
    ("Remove-Item C:\\x -Recurse -Force", "Remove-Item"),
])
def test_destructive_commands_detected(command, reason):
    found = policy.destructive_reason(command)
    assert found and reason.lower() in found.lower()


def test_screen_command_levels(ops_config):
    ops_config(mode="read")
    policy.screen_command("df -h", "t")
    with pytest.raises(policy.PolicyError, match="operate"):
        policy.screen_command("systemctl restart radarr", "t")

    ops_config(mode="operate")
    policy.screen_command("systemctl restart radarr", "t")
    with pytest.raises(policy.PolicyError, match="admin"):
        policy.screen_command("pct destroy 105", "t")

    ops_config(mode="admin")
    policy.screen_command("pct destroy 105", "t")


def test_method_levels():
    assert policy.method_level("get") == policy.READ
    assert policy.method_level("POST") == policy.OPERATE
    assert policy.method_level("delete") == policy.ADMIN


def test_check_path_enforces_roots(ops_config, tmp_path):
    allowed = tmp_path / "ok"
    allowed.mkdir()
    ops_config({"policy": {"allow_paths": [str(allowed)]}})
    assert policy.check_path(allowed / "f.txt") == (allowed / "f.txt").resolve()
    with pytest.raises(policy.PolicyError):
        policy.check_path(tmp_path / "other.txt")
    with pytest.raises(policy.PolicyError):
        policy.check_path(allowed / ".." / "escape.txt")


def test_audit_log_written(ops_config, tmp_path):
    log = tmp_path / "audit.jsonl"
    ops_config({"policy": {"audit_log": str(log)}})
    policy.audit("unit", detail="x")
    record = json.loads(log.read_text(encoding="utf-8").strip())
    assert record["tool"] == "unit" and record["detail"] == "x"


def test_clip_keeps_head_and_tail(ops_config):
    ops_config({"policy": {"max_output_chars": 100}})
    text = "A" * 200 + "Z" * 200
    out = policy.clip(text)
    assert out.startswith("A") and out.endswith("Z") and "omitted" in out


def test_remote_mode_parsed_and_validated():
    assert parse_config({"policy": {"mode": "operate"}}).policy.remote_mode == "read"
    assert parse_config({"policy": {"remote_mode": "operate"}}).policy.remote_mode == "operate"
    with pytest.raises(ConfigError, match="remote_mode"):
        parse_config({"policy": {"remote_mode": "root"}})
