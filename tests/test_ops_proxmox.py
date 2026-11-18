"""Proxmox tool routing, summaries, policy and guest exec."""

import json

import pytest

from openowl.ops import policy, proxmox

RESOURCES = [
    {"type": "node", "node": "Slimmm", "status": "online", "cpu": 0.25, "maxcpu": 16,
     "mem": 32 * 1024 ** 3, "maxmem": 64 * 1024 ** 3, "uptime": 7200},
    {"type": "lxc", "vmid": 105, "name": "radarr", "node": "Slimmm", "status": "running",
     "cpu": 0.01, "mem": 1024 ** 3, "maxmem": 2 * 1024 ** 3, "disk": 1, "maxdisk": 2},
    {"type": "qemu", "vmid": 9000, "name": "gpu", "node": "Thiccc", "status": "stopped"},
    {"type": "storage", "storage": "local", "node": "Slimmm", "status": "available",
     "disk": 50 * 1024 ** 3, "maxdisk": 100 * 1024 ** 3},
]


class FakeTransport:
    def __init__(self):
        self.calls = []

    def call(self, method, path, params=None):
        self.calls.append((method, path, params))
        if path == "/cluster/resources":
            if params and params.get("type") == "vm":
                return [r for r in RESOURCES if r["type"] in ("lxc", "qemu")]
            return RESOURCES
        if path == "/cluster/status":
            return [{"type": "node", "name": "Thiccc", "ip": "10.0.0.5"},
                    {"type": "node", "name": "Slimmm", "ip": "10.0.0.6"}]
        if path.endswith("/status/current"):
            return {"status": "running", "uptime": 5}
        if method == "POST":
            return "UPID:Slimmm:0001:task"
        return {}


@pytest.fixture
def pve(ops_config):
    ops_config({"ssh": {"slim": "root@10.0.0.6"}, "proxmox": {"ssh_host": "slim"}})
    fake = FakeTransport()
    proxmox.set_transport(fake)
    return fake


def test_overview_summarises_nodes_guests_storage(pve):
    out = proxmox.do_proxmox("overview")
    assert "Slimmm: online" in out
    assert "105 lxc  radarr" in out
    assert "9000 qemu" in out
    assert "local" in out and "50%" in out


def test_overview_filters(pve):
    out = proxmox.do_proxmox("overview", status="running")
    assert "radarr" in out and "gpu" not in out
    assert "gpu" in proxmox.do_proxmox("overview", search="gpu")


def test_find_guest_by_vmid_or_name(pve):
    assert proxmox.find_guest(105)["name"] == "radarr"
    assert proxmox.find_guest("GPU")["vmid"] == 9000
    with pytest.raises(LookupError):
        proxmox.find_guest("nope")


def test_status_uses_guest_path(pve):
    proxmox.do_proxmox("status", target="radarr")
    assert ("GET", "/nodes/Slimmm/lxc/105/status/current", None) in pve.calls


def test_power_actions_need_operate(pve, ops_config):
    with pytest.raises(policy.PolicyError):
        proxmox.do_proxmox("start", target="9000")
    ops_config({"ssh": {"slim": "root@10.0.0.6"}, "proxmox": {"ssh_host": "slim"}}, mode="operate")
    proxmox.set_transport(pve)  # a new config drops the cached transport
    out = proxmox.do_proxmox("start", target="9000")
    assert "UPID" in out
    assert ("POST", "/nodes/Thiccc/qemu/9000/status/start", None) in pve.calls


def test_rollback_needs_admin(pve, ops_config):
    ops_config({"ssh": {"slim": "root@10.0.0.6"}, "proxmox": {"ssh_host": "slim"}}, mode="operate")
    proxmox.set_transport(pve)  # a new config drops the cached transport
    with pytest.raises(policy.PolicyError, match="admin"):
        proxmox.do_proxmox("rollback", target="105", name="snap1")


def test_raw_api_levels(pve):
    proxmox.do_proxmox("api", target="/nodes/Slimmm/disks/list", name="GET")
    with pytest.raises(policy.PolicyError):
        proxmox.do_proxmox("api", target="/nodes/Slimmm/lxc/105/status/start", name="POST")


def test_unknown_action_lists_choices(pve):
    with pytest.raises(ValueError, match="overview"):
        proxmox.do_proxmox("explode", target="105")


def test_pvesh_transport_parses_json(monkeypatch, ops_config):
    ops_config({"ssh": {"slim": "root@10.0.0.6"}})
    seen = {}

    def fake_exec(argv, timeout):
        seen["argv"] = argv
        return json.dumps([{"vmid": 1}]), "", 0

    monkeypatch.setattr(proxmox, "exec_process", fake_exec)
    t = proxmox.PveshTransport(proxmox.resolve_host("slim"))
    assert t.call("GET", "/cluster/resources", {"type": "vm"}) == [{"vmid": 1}]
    assert seen["argv"][-1] == "pvesh get /cluster/resources --output-format json --type vm"

    monkeypatch.setattr(proxmox, "exec_process", lambda argv, timeout: ("", "denied", 255))
    with pytest.raises(RuntimeError, match="denied"):
        t.call("POST", "/x")


def test_guest_exec_routes_to_node(pve, monkeypatch):
    seen = {}

    def fake_exec(argv, timeout):
        seen["argv"] = argv
        return "radarr.db\n", "", 0

    monkeypatch.setattr(proxmox, "exec_process", fake_exec)
    out = proxmox.do_guest_exec("radarr", "ls /var/lib/radarr")
    assert "radarr.db" in out and "[exit 0]" in out
    assert "root@10.0.0.6" in seen["argv"]
    assert seen["argv"][-1] == "pct exec 105 -- sh -c 'ls /var/lib/radarr'"


def test_guest_exec_unmapped_node_hops_via_entry(pve, monkeypatch):
    RESOURCES[2]["status"] = "running"
    try:
        seen = {}
        payload = json.dumps({"exitcode": 0, "out-data": "hi\n"})
        monkeypatch.setattr(proxmox, "exec_process",
                            lambda argv, timeout: seen.setdefault("argv", argv) and (payload, "", 0))
        out = proxmox.do_guest_exec("9000", "echo hi")
        assert "hi" in out and "[exit 0]" in out
        argv = seen["argv"]
        assert argv[argv.index("-J") + 1] == "root@10.0.0.6"
        assert "root@10.0.0.5" in argv
        assert argv[-1].startswith("qm guest exec 9000")
    finally:
        RESOURCES[2]["status"] = "stopped"


def test_guest_exec_screens_command(pve):
    with pytest.raises(policy.PolicyError):
        proxmox.do_guest_exec("radarr", "systemctl restart radarr")


def test_guest_exec_refuses_stopped_guest(pve):
    with pytest.raises(RuntimeError, match="stopped"):
        proxmox.do_guest_exec("9000", "ls")


def test_secret_from_guest_and_ssh(pve, monkeypatch):
    from openowl.ops import config as config_mod
    from openowl.ops import system

    calls = []

    def fake_exec(argv, timeout):
        calls.append(argv)
        return "SECRET123\n", "", 0

    monkeypatch.setattr(system, "exec_process", fake_exec)
    section = {"api_key_guest": {"guest": "radarr", "command": "cat /k"}}
    assert config_mod.resolve_secret(section, "api_key", "services.radarr") == "SECRET123"
    assert calls[-1][-1] == "pct exec 105 -- sh -c 'cat /k'"
    # cached: a second lookup does not reconnect
    assert config_mod.resolve_secret(section, "api_key", "services.radarr") == "SECRET123"
    assert len(calls) == 1

    ssh_section = {"token_ssh": {"host": "slim", "command": "cat /root/t"}}
    assert config_mod.resolve_secret(ssh_section, "token", "services.x") == "SECRET123"
    assert calls[-1][-3:] == ["root@10.0.0.6", "--", "cat /root/t"]
    assert config_mod.has_secret(ssh_section, "token")


def test_secret_source_failure_is_explained(pve, monkeypatch):
    from openowl.ops import config as config_mod
    from openowl.ops import system

    monkeypatch.setattr(system, "exec_process", lambda argv, timeout: ("", "Permission denied", 255))
    with pytest.raises(config_mod.ConfigError, match="Permission denied"):
        config_mod.resolve_secret({"k_ssh": {"host": "slim", "command": "x"}}, "k", "s")
    with pytest.raises(config_mod.ConfigError, match="command"):
        config_mod.resolve_secret({"k_guest": "105"}, "k", "s2")


def test_secret_source_must_print_one_line(pve, monkeypatch):
    from openowl.ops import config as config_mod
    from openowl.ops import system

    monkeypatch.setattr(system, "exec_process", lambda argv, timeout: ("banner\nKEY\n", "", 0))
    with pytest.raises(config_mod.ConfigError, match="2 lines") as err:
        config_mod.resolve_secret({"k_ssh": {"host": "slim", "command": "x"}}, "k", "s3")
    assert "KEY" not in str(err.value)


def test_raw_api_rollback_needs_admin_like_dedicated_action(pve, ops_config):
    ops_config({"ssh": {"slim": "root@10.0.0.6"}, "proxmox": {"ssh_host": "slim"}}, mode="operate")
    proxmox.set_transport(pve)
    with pytest.raises(policy.PolicyError, match="admin"):
        proxmox.do_proxmox("api", target="/nodes/Slimmm/lxc/105/snapshot/s1/rollback", name="POST")


def test_guest_list_is_cached_and_invalidated(pve, ops_config):
    proxmox.find_guest(105)
    proxmox.find_guest("radarr")
    assert sum(1 for c in pve.calls if c[1] == "/cluster/resources") == 1
    ops_config({"ssh": {"slim": "root@10.0.0.6"}, "proxmox": {"ssh_host": "slim"}}, mode="operate")
    proxmox.set_transport(pve)
    proxmox.do_proxmox("start", target="105")   # lookup (fetch 2), then invalidates
    proxmox.find_guest(105)                     # fetch 3
    assert sum(1 for c in pve.calls if c[1] == "/cluster/resources") == 3
