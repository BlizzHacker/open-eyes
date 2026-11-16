"""Proxmox VE: cluster overview, guest power, snapshots, tasks and in-guest exec.

Two transports, picked from ``[proxmox]``:

* ``url`` + ``token_id`` + ``token_secret`` -> the REST API with an API token.
* ``ssh_host`` -> ``pvesh`` over SSH on a cluster node. No token needed;
  uses the same SSH keys as ``ssh_run``.

``guest_exec`` always goes over SSH to the node that hosts the guest
(``pct exec`` for containers, ``qm guest exec`` for VMs with the agent).
"""

from __future__ import annotations

import json
import shlex
import threading
import time
from typing import Any, Protocol

import httpx

from openowl.ops import policy
from openowl.ops.config import ConfigError, SshHost, get_config
from openowl.ops.remote import resolve_host, ssh_argv
from openowl.ops.system import default_timeout, exec_process, format_process

POWER_ACTIONS = ("start", "stop", "shutdown", "reboot", "suspend", "resume")
GUEST_READS = {"status": "/status/current", "config": "/config", "snapshots": "/snapshot"}
GUEST_CACHE_SECONDS = 15.0
_PVESH_VERB = {"GET": "get", "POST": "create", "PUT": "set", "DELETE": "delete"}


class Transport(Protocol):
    def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any: ...


class ApiTransport:
    def __init__(self, url: str, token_id: str, secret: str, verify_tls: bool) -> None:
        self._client = httpx.Client(
            base_url=f"{url}/api2/json",
            headers={"Authorization": f"PVEAPIToken={token_id}={secret}"},
            verify=verify_tls, timeout=30,
        )

    def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        if method == "GET":
            resp = self._client.get(path, params=params)
        else:
            resp = self._client.request(method, path, data=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"Proxmox API {method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json().get("data")


class PveshTransport:
    def __init__(self, host: SshHost) -> None:
        self.host = host

    def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        verb = _PVESH_VERB[method.upper()]
        parts = ["pvesh", verb, path, "--output-format", "json"]
        for key, value in (params or {}).items():
            if isinstance(value, bool):
                value = int(value)
            parts += [f"--{key}", str(value)]
        cmd = " ".join(shlex.quote(p) for p in parts)
        out, err, code = exec_process(ssh_argv(self.host, cmd), timeout=90)
        if code != 0:
            raise RuntimeError(f"pvesh {verb} {path} failed ({policy.exit_reason(code)}): {err.strip()[:500]}")
        body = out.strip()
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body


# Every pvesh call is an SSH round trip, so the transport, the guest list and
# node addresses are shared and cached. reset() drops them all (config reloads).
_lock = threading.RLock()  # re-entrant: a guest-sourced token secret looks up guests
_transport: Transport | None = None
_guest_cache: tuple[float, list[dict[str, Any]]] | None = None
_node_ips: dict[str, str] = {}


def get_transport() -> Transport:
    global _transport
    with _lock:
        if _transport is None:
            _transport = _build_transport()
        return _transport


def _build_transport() -> Transport:
    pve = get_config().proxmox
    if pve.url and pve.raw.get("token_id"):
        return ApiTransport(pve.url, str(pve.raw["token_id"]), pve.secret("token_secret"),
                            pve.verify_tls)
    if pve.ssh_host:
        return PveshTransport(resolve_host(pve.ssh_host))
    raise ConfigError(
        "Proxmox is not configured. Add [proxmox] with ssh_host = \"<ssh alias>\" "
        "(uses pvesh over SSH) or url + token_id + token_secret_env."
    )


def set_transport(transport: Transport | None) -> None:
    reset()
    global _transport
    _transport = transport


def reset() -> None:
    global _transport, _guest_cache
    with _lock:
        _transport = None
        _guest_cache = None
        _node_ips.clear()


def _invalidate_guests() -> None:
    global _guest_cache
    _guest_cache = None


# ------------------------------------------------------------------
# Lookups and summaries
# ------------------------------------------------------------------

def guests() -> list[dict[str, Any]]:
    """All VMs and containers, cached briefly; concurrent callers share one fetch."""
    global _guest_cache
    transport = get_transport()
    with _lock:
        if _guest_cache and time.monotonic() - _guest_cache[0] < GUEST_CACHE_SECONDS:
            return _guest_cache[1]
        items = transport.call("GET", "/cluster/resources", {"type": "vm"}) or []
        _guest_cache = (time.monotonic(), items)
        return items


def find_guest(vmid: int | str) -> dict[str, Any]:
    """Return the cluster resource for *vmid*, or for a guest whose name matches."""
    items = guests()
    key = str(vmid).strip()
    for g in items:
        if str(g.get("vmid")) == key:
            return g
    matches = [g for g in items if str(g.get("name", "")).lower() == key.lower()]
    if len(matches) == 1:
        return matches[0]
    raise LookupError(f"No guest with vmid or name '{vmid}'.")


def _gib(value: Any) -> str:
    try:
        return f"{float(value) / 1024 ** 3:.1f}G"
    except (TypeError, ValueError):
        return "?"


def _pct(used: Any, total: Any) -> str:
    try:
        return f"{100 * float(used) / float(total):.0f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "?"


def summarize_resources(resources: list[dict[str, Any]], status: str = "", search: str = "") -> str:
    nodes = [r for r in resources if r.get("type") == "node"]
    vms = [r for r in resources if r.get("type") in ("qemu", "lxc")]
    storage = [r for r in resources if r.get("type") == "storage"]
    lines = ["NODES"]
    for n in sorted(nodes, key=lambda r: r.get("node", "")):
        lines.append(
            f"  {n.get('node')}: {n.get('status')}  cpu {float(n.get('cpu') or 0) * 100:.0f}% "
            f"of {n.get('maxcpu', '?')}  mem {_gib(n.get('mem'))}/{_gib(n.get('maxmem'))}  "
            f"uptime {int(n.get('uptime') or 0) // 3600}h"
        )
    if status:
        vms = [v for v in vms if v.get("status") == status]
    if search:
        s = search.lower()
        vms = [v for v in vms if s in str(v.get("name", "")).lower() or s in str(v.get("vmid"))
               or s in str(v.get("tags", "")).lower()]
    running = sum(1 for v in vms if v.get("status") == "running")
    lines.append(f"GUESTS ({len(vms)} shown, {running} running)")
    for v in sorted(vms, key=lambda r: int(r.get("vmid") or 0)):
        lines.append(
            f"  {v.get('vmid'):>5} {v.get('type'):<4} {str(v.get('name', '')):<24} "
            f"{v.get('status'):<8} on {v.get('node'):<10} cpu {float(v.get('cpu') or 0) * 100:.0f}% "
            f"mem {_gib(v.get('mem'))}/{_gib(v.get('maxmem'))} disk {_gib(v.get('disk'))}/{_gib(v.get('maxdisk'))}"
        )
    if storage and not (status or search):
        lines.append("STORAGE")
        seen = set()
        for s in sorted(storage, key=lambda r: (r.get("storage", ""), r.get("node", ""))):
            key = (s.get("storage"), s.get("shared") and "shared" or s.get("node"))
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"  {s.get('storage'):<16} {s.get('node'):<10} {s.get('status', ''):<10} "
                f"{_gib(s.get('disk'))}/{_gib(s.get('maxdisk'))} ({_pct(s.get('disk'), s.get('maxdisk'))})"
            )
    return "\n".join(lines)


# ------------------------------------------------------------------
# Actions
# ------------------------------------------------------------------

def _guest_path(g: dict[str, Any]) -> str:
    return f"/nodes/{g['node']}/{g['type']}/{g['vmid']}"


def do_proxmox(action: str, target: str = "", name: str = "", status: str = "",
               search: str = "", limit: int = 30, params: dict | None = None) -> str:
    action = action.lower().strip()
    t = get_transport()

    if action in ("overview", "resources"):
        return summarize_resources(t.call("GET", "/cluster/resources") or [], status, search)
    if action == "nodes":
        return policy.to_text(t.call("GET", "/nodes"))
    if action == "cluster":
        return policy.to_text(t.call("GET", "/cluster/status"))
    if action == "tasks":
        tasks = t.call("GET", "/cluster/tasks") or []
        if target:
            tasks = [x for x in tasks if str(x.get("id", "")) == str(target)
                     or str(target) in str(x.get("upid", ""))]
        rows = [
            f"{time.strftime('%m-%d %H:%M', time.localtime(int(x.get('starttime') or 0)))} "
            f"{x.get('node')} {x.get('type')} {x.get('id', '')} {x.get('user')} "
            f"-> {x.get('status', 'running')}  {x.get('upid')}"
            for x in tasks[:limit]
        ]
        return "\n".join(rows) or "No tasks."
    if action == "task_log":
        if not target.startswith("UPID:"):
            raise ValueError("task_log needs target = the task UPID (from action 'tasks').")
        node = target.split(":")[1]
        log = t.call("GET", f"/nodes/{node}/tasks/{target}/log", {"limit": 500}) or []
        status_info = t.call("GET", f"/nodes/{node}/tasks/{target}/status")
        lines = [str(e.get("t", "")) for e in log] if isinstance(log, list) else [str(log)]
        return policy.clip(policy.to_text(status_info) + "\n" + "\n".join(lines))
    if action == "storage":
        node = target or _first_node(t)
        return policy.to_text(t.call("GET", f"/nodes/{node}/storage"))
    if action == "api":
        return _raw_api(t, name or "GET", target, params)

    if not target:
        raise ValueError(f"Action '{action}' needs target = a guest vmid or name.")
    g = find_guest(target)
    base = _guest_path(g)
    label = f"{g['type']} {g['vmid']} ({g.get('name', '')}) on {g['node']}"

    if action in GUEST_READS:
        return f"{label}\n" + policy.to_text(t.call("GET", base + GUEST_READS[action]))
    who = {"vmid": g["vmid"], "node": g["node"]}
    if action in POWER_ACTIONS:
        policy.authorize(policy.OPERATE, f"proxmox {action}", "proxmox", **who)
        upid = t.call("POST", f"{base}/status/{action}")
        _invalidate_guests()
        return f"{action} requested for {label}. Task: {upid}"
    if action == "snapshot":
        snap = name or time.strftime("oe-%Y%m%d-%H%M%S")
        policy.authorize(policy.OPERATE, "proxmox snapshot", "proxmox", snapname=snap, **who)
        upid = t.call("POST", f"{base}/snapshot", {"snapname": snap, "description": "Open Eyes"})
        return f"Snapshot '{snap}' requested for {label}. Task: {upid}"
    if action in ("rollback", "delete_snapshot"):
        if not name:
            raise ValueError(f"{action} needs name = the snapshot name.")
        policy.authorize(policy.ADMIN, f"proxmox {action}", "proxmox", snapname=name, **who)
        if action == "rollback":
            upid = t.call("POST", f"{base}/snapshot/{name}/rollback")
        else:
            upid = t.call("DELETE", f"{base}/snapshot/{name}")
        _invalidate_guests()
        return f"{action} '{name}' requested for {label}. Task: {upid}"
    if action == "set_config":
        if not params:
            raise ValueError("set_config needs params, e.g. {\"memory\": 4096, \"cores\": 4}.")
        policy.authorize(policy.OPERATE, "proxmox set_config", "proxmox", params=params, **who)
        t.call("PUT", f"{base}/config", params)
        return f"Updated config of {label}: {params}"
    raise ValueError(
        f"Unknown action '{action}'. Use overview, nodes, cluster, tasks, task_log, storage, "
        f"status, config, snapshots, {', '.join(POWER_ACTIONS)}, snapshot, rollback, "
        f"delete_snapshot, set_config or api."
    )


def _first_node(t: Transport) -> str:
    nodes = t.call("GET", "/nodes") or []
    if not nodes:
        raise RuntimeError("Proxmox returned no nodes.")
    return nodes[0]["node"]


def _raw_api(t: Transport, method: str, path: str, params: dict | None) -> str:
    if not path.startswith("/"):
        raise ValueError("api needs target = an API path such as /nodes/Slimmm/disks/list.")
    level = policy.method_level(method)
    if path.rstrip("/").endswith("/rollback"):
        level = policy.ADMIN  # same level as the dedicated rollback action
    policy.authorize(level, f"proxmox api {method.upper()}", "proxmox", path=path, params=params)
    result = t.call(method, path, params)
    if level:
        _invalidate_guests()
    return policy.to_text(result)


# ------------------------------------------------------------------
# Guest exec
# ------------------------------------------------------------------

def node_host(node: str) -> SshHost:
    """SSH host for a cluster node: explicit mapping, else its cluster IP via the entry node."""
    pve = get_config().proxmox
    if node in pve.nodes:
        return resolve_host(pve.nodes[node])
    if not _node_ips:
        entries = get_transport().call("GET", "/cluster/status") or []
        _node_ips.update({e["name"]: e["ip"] for e in entries
                          if e.get("type") == "node" and e.get("ip")})
    ip = _node_ips.get(node)
    if not ip:
        raise ConfigError(f"Cannot find an address for node '{node}'. Map it in [proxmox.nodes].")
    jump = ""
    if pve.ssh_host:
        entry = resolve_host(pve.ssh_host)
        if entry.target.rsplit("@", 1)[-1] == ip:
            return entry
        jump = entry.target
    return SshHost(name=node, target=f"root@{ip}", jump=jump)


def guest_exec_command(guest_type: str, vmid: int | str, command: str) -> str:
    # Plain `sh -c`, not a login shell: guest profiles print banners into stdout.
    inner = shlex.quote(command)
    if guest_type == "lxc":
        return f"pct exec {int(vmid)} -- sh -c {inner}"
    return f"qm guest exec {int(vmid)} --timeout 110 -- sh -c {inner}"


def do_guest_exec(target: str, command: str, timeout: float = 0) -> str:
    policy.screen_command(command, "guest_exec")
    g = find_guest(target)
    if g.get("status") != "running":
        raise RuntimeError(f"Guest {g['vmid']} ({g.get('name')}) is {g.get('status')}, not running.")
    host = node_host(g["node"])
    remote = guest_exec_command(g["type"], g["vmid"], command)
    policy.audit("guest_exec", vmid=g["vmid"], node=g["node"], command=command)
    limit = default_timeout(timeout)
    out, err, code = exec_process(ssh_argv(host, remote), timeout=limit)
    if g["type"] == "qemu" and code == 0:
        out, err, code = _decode_qm_exec(out, err, code)
    text = format_process(out, err, code, limit)
    return f"[{g['type']} {g['vmid']} {g.get('name', '')} on {g['node']}]\n{text}"


def _decode_qm_exec(out: str, err: str, code: int | None) -> tuple[str, str, int | None]:
    """Unwrap the JSON that `qm guest exec` prints into plain stdout/stderr/exit."""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out, err, code
    return data.get("out-data", ""), data.get("err-data", ""), data.get("exitcode", code)


def register(server) -> None:
    @server.tool()
    def proxmox(action: str, target: str = "", name: str = "", status: str = "",
                search: str = "", params: dict | None = None, limit: int = 30) -> str:
        """Inspect and manage the Proxmox VE cluster (nodes, VMs, LXC containers).

        Read actions: overview (nodes, every guest, storage), nodes, cluster, tasks,
        task_log (target=UPID), storage (target=node), status/config/snapshots (target=guest).
        Operate actions: start, stop, shutdown, reboot, suspend, resume, snapshot
        (name optional), set_config (params). Admin: rollback, delete_snapshot (name).
        action="api" calls any path: name=HTTP method, target=path, params=arguments.

        Parameters:
            action: What to do (see above).
            target: Guest vmid or exact name; a node; a UPID; or an API path.
            name: Snapshot name, or the HTTP method for action="api".
            status: Filter overview by status, e.g. "running" or "stopped".
            search: Filter overview by name, vmid or tag substring.
            params: Extra arguments for set_config / api.
            limit: Maximum rows for tasks.
        """
        return do_proxmox(action, target, name, status, search, limit, params)

    @server.tool()
    def guest_exec(target: str, command: str, timeout: float = 0) -> str:
        """Run a shell command inside a Proxmox guest: pct exec for LXC, qm guest exec for VMs.

        Finds the guest's node automatically. Same policy as ssh_run (read-only
        commands in read mode).

        Parameters:
            target: Guest vmid or exact name (e.g. 105 or "radarr").
            command: Command to run inside the guest (POSIX shell).
            timeout: Seconds before giving up.
        """
        return do_guest_exec(target, command, timeout)
