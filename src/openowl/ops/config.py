"""Configuration for the Open Eyes ops toolkit.

The toolkit is driven by one TOML file. Its location is, in order:

1. ``$OPEN_EYES_CONFIG``
2. ``%LOCALAPPDATA%\\MoveWeight\\OpenEyes\\config.toml`` on Windows
3. ``~/.config/open-eyes/config.toml`` elsewhere

A missing file is not an error: every tool then runs with defaults and
reports which section it needs.

Secrets are never required to live in the file. Any secret field ``x`` may be
given as ``x`` (literal), ``x_env`` (environment variable), ``x_file`` (file
whose first line is the value), ``x_cmd`` (local command), ``x_ssh``
(``{host, command}`` run over SSH) or ``x_guest`` (``{guest, command}`` run
inside a Proxmox guest). Command forms run once and are cached in memory, so
a key can stay on the server that owns it and never be written to disk here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openowl import __version__

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

LEVELS = ("read", "operate", "admin")
USER_AGENT = f"open-eyes/{__version__}"

_ARR_DEFAULT_VERSION = {
    "sonarr": "v3",
    "radarr": "v3",
    "whisparr": "v3",
    "lidarr": "v1",
    "readarr": "v1",
    "prowlarr": "v1",
    "romarr": "v1",
}


class ConfigError(ValueError):
    """Raised for an invalid or incomplete configuration."""


def default_config_path() -> Path:
    env = os.environ.get("OPEN_EYES_CONFIG")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "MoveWeight" / "OpenEyes" / "config.toml"
    return Path.home() / ".config" / "open-eyes" / "config.toml"


@dataclass(frozen=True)
class Policy:
    mode: str = "read"
    remote_mode: str = "read"
    allow_paths: tuple[str, ...] = ()
    audit_log: str = ""
    max_output_chars: int = 20000
    command_timeout: float = 120.0

    @property
    def level(self) -> int:
        return LEVELS.index(self.mode)


@dataclass(frozen=True)
class SshHost:
    name: str
    target: str
    jump: str = ""
    port: int = 0
    identity_file: str = ""


@dataclass(frozen=True)
class Service:
    name: str
    kind: str = "http"
    url: str = ""
    app: str = ""
    api_version: str = ""
    auth: str = "none"
    health: str = ""
    verify_tls: bool = True
    timeout: float = 20.0
    description: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def secret(self, name: str) -> str:
        return resolve_secret(self.raw, name, f"services.{self.name}")


@dataclass(frozen=True)
class ProxmoxConfig:
    url: str = ""
    ssh_host: str = ""
    verify_tls: bool = False
    nodes: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def configured(self) -> bool:
        return bool(self.url or self.ssh_host)

    def secret(self, name: str) -> str:
        return resolve_secret(self.raw, name, "proxmox")


@dataclass(frozen=True)
class VisionConfig:
    api: str = "openai"
    base_url: str = ""
    model: str = ""
    max_tokens: int = 1024
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def secret(self, name: str) -> str:
        return resolve_secret(self.raw, name, "vision")


@dataclass(frozen=True)
class OpsConfig:
    path: str = ""
    loaded: bool = False
    policy: Policy = Policy()
    ssh: dict[str, SshHost] = field(default_factory=dict)
    services: dict[str, Service] = field(default_factory=dict)
    proxmox: ProxmoxConfig = ProxmoxConfig()
    vision: VisionConfig = VisionConfig()

    def service(self, name: str, kind: str | None = None) -> Service:
        svc = self.services.get(name)
        if svc is None:
            known = ", ".join(sorted(self.services)) or "none configured"
            raise ConfigError(f"Unknown service '{name}'. Configured services: {known}.")
        if kind and svc.kind != kind:
            raise ConfigError(f"Service '{name}' is kind '{svc.kind}', not '{kind}'.")
        return svc

    def services_of(self, kind: str) -> list[Service]:
        return [s for s in self.services.values() if s.kind == kind]


# ------------------------------------------------------------------
# Secrets
# ------------------------------------------------------------------

_secret_cache: dict[str, str] = {}
_secret_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(label: str) -> threading.Lock:
    """One lock per secret, so different keys are fetched in parallel."""
    with _locks_guard:
        return _secret_locks.setdefault(label, threading.Lock())


def resolve_secret(section: dict[str, Any], name: str, where: str) -> str:
    """Resolve secret *name* from *section* using the literal/_env/_file/_cmd forms."""
    if section.get(name):
        return str(section[name])
    env_name = section.get(f"{name}_env")
    if env_name:
        value = os.environ.get(str(env_name), "")
        if not value:
            raise ConfigError(f"{where}.{name}_env names ${env_name}, which is not set.")
        return value
    file_name = section.get(f"{name}_file")
    if file_name:
        try:
            text = Path(str(file_name)).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"{where}.{name}_file could not be read: {exc}") from exc
        return text.strip().splitlines()[0].strip() if text.strip() else ""
    for form in ("cmd", "ssh", "guest"):
        spec = section.get(f"{name}_{form}")
        if spec:
            return _cached_fetch(f"{where}.{name}_{form}", form, spec)
    return ""


def _cached_fetch(label: str, form: str, spec: Any) -> str:
    """Run a command-based secret source once and cache the value in memory."""
    with _lock_for(label):
        if label in _secret_cache:
            return _secret_cache[label]
        out, err, code = _run_secret_source(label, form, spec)
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if code != 0 or not lines:
            reason = "timed out" if code is None else f"exit {code}"
            raise ConfigError(f"{label} failed ({reason}): {err.strip()[:200]}")
        if len(lines) > 1:
            # Never echo the output: it may contain the secret.
            raise ConfigError(f"{label} printed {len(lines)} lines; it must print only the value.")
        value = lines[0]
        _secret_cache[label] = value
        return value


def _run_secret_source(label: str, form: str, spec: Any) -> tuple[str, str, int | None]:
    if form == "cmd":
        try:
            proc = subprocess.run(str(spec), shell=True, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "", "", None
        return proc.stdout, proc.stderr, proc.returncode
    if not isinstance(spec, dict) or not spec.get("command"):
        raise ConfigError(f"{label} must be a table with 'command' and '{'host' if form == 'ssh' else 'guest'}'.")
    # Imported here: these modules import this one.
    from openowl.ops.remote import resolve_host, ssh_argv
    from openowl.ops.system import exec_process

    if form == "ssh":
        if not spec.get("host"):
            raise ConfigError(f"{label}.host is required.")
        return exec_process(ssh_argv(resolve_host(str(spec["host"])), str(spec["command"])), timeout=60)
    from openowl.ops.proxmox import find_guest, guest_exec_command, node_host

    if not spec.get("guest"):
        raise ConfigError(f"{label}.guest is required (vmid or name).")
    guest = find_guest(spec["guest"])
    remote = guest_exec_command(guest["type"], guest["vmid"], str(spec["command"]))
    return exec_process(ssh_argv(node_host(guest["node"]), remote), timeout=60)


_SECRET_FORMS = ("", "_env", "_file", "_cmd", "_ssh", "_guest")


def has_secret(section: dict[str, Any], name: str) -> bool:
    return any(section.get(f"{name}{suffix}") for suffix in _SECRET_FORMS)


# ------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------

def _parse_policy(data: dict[str, Any]) -> Policy:
    env_mode = os.environ.get("OPEN_EYES_MODE", "").strip().lower()
    mode = (env_mode or str(data.get("mode", "read"))).lower()
    remote_mode = str(data.get("remote_mode", "read")).lower()
    for key, value in (("mode", mode), ("remote_mode", remote_mode)):
        if value not in LEVELS:
            raise ConfigError(f"policy.{key} must be one of {LEVELS}, got '{value}'.")
    paths = data.get("allow_paths", [])
    if isinstance(paths, str):
        paths = [paths]
    return Policy(
        mode=mode,
        remote_mode=remote_mode,
        allow_paths=tuple(str(Path(p).expanduser()) for p in paths),
        audit_log=str(Path(data["audit_log"]).expanduser()) if data.get("audit_log") else "",
        max_output_chars=int(data.get("max_output_chars", 20000)),
        command_timeout=float(data.get("command_timeout", 120)),
    )


def _parse_ssh(data: dict[str, Any]) -> dict[str, SshHost]:
    hosts = {}
    for name, entry in data.items():
        if isinstance(entry, str):
            entry = {"target": entry}
        if not entry.get("target"):
            raise ConfigError(f"ssh.{name}.target is required (e.g. 'root@192.168.0.6').")
        hosts[name] = SshHost(
            name=name,
            target=str(entry["target"]),
            jump=str(entry.get("jump", "")),
            port=int(entry.get("port", 0)),
            identity_file=str(entry.get("identity_file", "")),
        )
    return hosts


def _parse_service(name: str, entry: dict[str, Any]) -> Service:
    kind = str(entry.get("kind", "http")).lower()
    app = str(entry.get("app", "")).lower()
    if kind == "arr" and not app:
        app = name.lower().split("-")[0]
    api_version = str(entry.get("api_version", ""))
    if kind == "arr" and not api_version:
        api_version = _ARR_DEFAULT_VERSION.get(app, "v3")
    auth = str(entry.get("auth", "")).lower()
    if not auth:
        auth = {"arr": "x-api-key", "plex": "header:X-Plex-Token"}.get(kind, "none")
        if kind == "http" and has_secret(entry, "token"):
            auth = "bearer"
    health = str(entry.get("health", ""))
    if not health:
        health = {
            "arr": f"/api/{api_version}/system/status",
            "plex": "/identity",
            "qbittorrent": "/api/v2/app/version",
        }.get(kind, "/")
    return Service(
        name=name,
        kind=kind,
        url=str(entry.get("url", "")).rstrip("/"),
        app=app,
        api_version=api_version,
        auth=auth,
        health=health,
        verify_tls=bool(entry.get("verify_tls", True)),
        timeout=float(entry.get("timeout", 20)),
        description=str(entry.get("description", "")),
        raw=dict(entry),
    )


def parse_config(data: dict[str, Any], path: str = "", loaded: bool = True) -> OpsConfig:
    services = {}
    for name, entry in (data.get("services") or {}).items():
        svc = _parse_service(name, entry)
        if not svc.url:
            raise ConfigError(f"services.{name}.url is required.")
        services[name] = svc
    pve = data.get("proxmox") or {}
    vision = data.get("vision") or {}
    return OpsConfig(
        path=path,
        loaded=loaded,
        policy=_parse_policy(data.get("policy") or {}),
        ssh=_parse_ssh(data.get("ssh") or {}),
        services=services,
        proxmox=ProxmoxConfig(
            url=str(pve.get("url", "")).rstrip("/"),
            ssh_host=str(pve.get("ssh_host", "")),
            verify_tls=bool(pve.get("verify_tls", False)),
            nodes={str(k): str(v) for k, v in (pve.get("nodes") or {}).items()},
            raw=dict(pve),
        ),
        vision=VisionConfig(
            api=str(vision.get("api", "openai")).lower(),
            base_url=str(vision.get("base_url", "")).rstrip("/"),
            model=str(vision.get("model", "")),
            max_tokens=int(vision.get("max_tokens", 1024)),
            raw=dict(vision),
        ),
    )


def load_config(path: str | os.PathLike[str] | None = None) -> OpsConfig:
    cfg_path = Path(path).expanduser() if path else default_config_path()
    if not cfg_path.is_file():
        return parse_config({}, path=str(cfg_path), loaded=False)
    try:
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{cfg_path}: invalid TOML: {exc}") from exc
    return parse_config(data, path=str(cfg_path), loaded=True)


_current: OpsConfig | None = None


def get_config() -> OpsConfig:
    """Return the process-wide config, loading it on first use."""
    global _current
    if _current is None:
        _current = load_config()
    return _current


def set_config(cfg: OpsConfig | None) -> None:
    """Replace the process-wide config (tests and reloads) and drop everything derived from it."""
    global _current
    _current = cfg
    _secret_cache.clear()
    from openowl.ops import proxmox  # imported here: proxmox imports this module

    proxmox.reset()
