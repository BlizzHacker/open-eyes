"""Configured homelab services: authenticated API calls and health checks.

Every entry in ``[services.<name>]`` gets the right auth attached
automatically, so a model can call ``service_api("radarr", "GET",
"/api/v3/queue")`` or ``service_api("yarrit", "GET", "/api/health")``
without ever seeing a key.

``auth`` values: ``none``, ``x-api-key`` (secret ``api_key``), ``bearer``
(secret ``token``), ``basic`` (``username`` + secret ``password``),
``header:<Name>`` (secret ``token``), ``query:<param>`` (secret ``api_key``).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from openowl.ops import policy
from openowl.ops.config import USER_AGENT, ConfigError, Service, get_config
from openowl.ops.system import format_response

# Tests replace this to route requests through httpx.MockTransport.
transport_factory: Any = None


def auth_parts(svc: Service) -> tuple[dict[str, str], dict[str, str], tuple[str, str] | None]:
    """Return (headers, query params, basic auth) for *svc*."""
    auth = svc.auth
    if auth in ("", "none"):
        return {}, {}, None
    if auth == "x-api-key":
        return {"X-Api-Key": svc.secret("api_key")}, {}, None
    if auth == "bearer":
        return {"Authorization": f"Bearer {svc.secret('token')}"}, {}, None
    if auth == "basic":
        return {}, {}, (str(svc.raw.get("username", "")), svc.secret("password"))
    if auth.startswith("header:"):
        return {auth.split(":", 1)[1]: svc.secret("token")}, {}, None
    if auth.startswith("query:"):
        return {}, {auth.split(":", 1)[1]: svc.secret("api_key")}, None
    raise ConfigError(f"services.{svc.name}.auth '{auth}' is not recognised.")


def client_for(svc: Service) -> httpx.Client:
    headers, params, basic = auth_parts(svc)
    extra = {"transport": transport_factory(svc)} if transport_factory else {}
    return httpx.Client(
        base_url=svc.url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                   **headers},
        params=params, auth=basic, verify=svc.verify_tls, timeout=svc.timeout,
        follow_redirects=True, **extra,
    )


def request(svc: Service, method: str, path: str, query: dict | None = None,
            json_body: Any = None, body: str = "") -> httpx.Response:
    if not path.startswith("/"):
        path = "/" + path
    with client_for(svc) as client:
        return client.request(method.upper(), path, params=query or None, json=json_body,
                              content=body or None)


def call_json(svc: Service, method: str, path: str, query: dict | None = None,
              json_body: Any = None) -> Any:
    resp = request(svc, method, path, query, json_body)
    if resp.status_code >= 400:
        raise RuntimeError(f"{svc.name} {method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def service_level(svc: Service, method: str, path: str) -> int:
    """Permission level for a raw call; some APIs change state on GET."""
    level = policy.method_level(method)
    if svc.kind == "plex" and "/refresh" in path:
        level = max(level, policy.OPERATE)  # same level as plex(action="refresh")
    return level


def do_service_api(service: str, method: str = "GET", path: str = "/", query: dict | None = None,
                   json_body: Any = None) -> str:
    svc = get_config().service(service)
    policy.authorize(service_level(svc, method, path), f"service_api {method.upper()} {service}",
                     "service_api", service=service, path=path)
    return format_response(request(svc, method, path, query, json_body))


def check_service(svc: Service) -> dict[str, Any]:
    start = time.monotonic()
    ok, code, detail = False, None, ""
    try:
        resp = request(svc, "GET", svc.health)
        ok, code = resp.status_code < 400, resp.status_code
        if ok and "json" in resp.headers.get("content-type", ""):
            data = resp.json()
            if isinstance(data, dict):
                detail = str(data.get("version") or data.get("appVersion") or "")
        elif not ok:
            detail = resp.text[:120].strip()
    except (httpx.HTTPError, ConfigError, ValueError) as exc:
        detail = str(exc)[:160]
    return {"service": svc.name, "ok": ok, "code": code,
            "ms": int((time.monotonic() - start) * 1000), "detail": detail}


def check_services(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Health-check services in parallel; returns one result dict per service."""
    cfg = get_config()
    services = [cfg.service(n) for n in names] if names else list(cfg.services.values())
    if not services:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(services))) as pool:
        return list(pool.map(check_service, services))


def format_status(results: list[dict[str, Any]]) -> str:
    cfg = get_config()
    if not results:
        return (f"No services configured. Add [services.<name>] entries to "
                f"{cfg.path or 'the Open Eyes config'}.")
    down = [r for r in results if not r["ok"]]
    lines = [f"{len(results) - len(down)}/{len(results)} services healthy"]
    for r in sorted(results, key=lambda r: (r["ok"], r["service"])):
        mark = "OK  " if r["ok"] else "DOWN"
        svc = cfg.services[r["service"]]
        lines.append(f"  {mark} {r['service']:<18} {svc.kind:<11} {r['code'] or '-':<4} "
                     f"{r['ms']:>5}ms  {svc.url}  {r['detail']}")
    return "\n".join(lines)


def do_services_status(names: list[str] | None = None) -> str:
    return format_status(check_services(names))


def do_list_services() -> str:
    cfg = get_config()
    lines = [f"Config: {cfg.path} ({'loaded' if cfg.loaded else 'not found'}), "
             f"mode: {cfg.policy.mode}"]
    if cfg.ssh:
        lines.append("SSH hosts:")
        lines += [f"  {h.name:<14} {h.target}" + (f"  via {h.jump}" if h.jump else "")
                  for h in cfg.ssh.values()]
    if cfg.proxmox.configured:
        how = "API" if cfg.proxmox.raw.get("token_id") else f"pvesh via ssh '{cfg.proxmox.ssh_host}'"
        lines.append(f"Proxmox: {how}")
    if cfg.services:
        lines.append("Services:")
        for s in sorted(cfg.services.values(), key=lambda s: (s.kind, s.name)):
            app = f"/{s.app}" if s.app else ""
            desc = f"  - {s.description}" if s.description else ""
            lines.append(f"  {s.name:<18} {s.kind}{app:<12} {s.url}{desc}")
    if cfg.vision.configured:
        lines.append(f"Vision model: {cfg.vision.model} ({cfg.vision.api} at {cfg.vision.base_url})")
    return "\n".join(lines)


def _proxmox_section() -> str:
    from openowl.ops.proxmox import do_proxmox
    try:
        return do_proxmox("overview")
    except Exception as exc:  # report, keep going with the rest
        return f"PROXMOX: unavailable ({exc})"


def do_homelab_overview() -> str:
    cfg = get_config()
    jobs = []
    if cfg.proxmox.configured:
        jobs.append(_proxmox_section)
    if cfg.services:
        jobs.append(lambda: "SERVICES\n" + do_services_status())
    if not jobs:
        return do_list_services()
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:  # independent; run together
        sections = list(pool.map(lambda job: job(), jobs))
    return policy.clip("\n\n".join(sections))


def register(server) -> None:
    @server.tool()
    def list_services() -> str:
        """Show what Open Eyes can reach: SSH hosts, Proxmox, configured services, vision model and the current permission mode."""
        return do_list_services()

    @server.tool()
    def services_status(names: list[str] | None = None) -> str:
        """Health-check configured services in parallel (status code, latency, version).

        Parameters:
            names: Optional list of service names; default checks all.
        """
        return do_services_status(names)

    @server.tool()
    def homelab_overview() -> str:
        """One-call picture of the homelab: Proxmox nodes, every VM/LXC with status, storage use, and service health."""
        return do_homelab_overview()

    @server.tool()
    def service_api(service: str, method: str = "GET", path: str = "/",
                    query: dict | None = None, json_body: dict | list | None = None) -> str:
        """Call any configured service's HTTP API with its credentials attached.

        Works for every [services.*] entry: Sonarr/Radarr/Prowlarr/ROMarr, RomM,
        Yarrit, Forgeverse, Plex, Authentik, Traefik, Gitea and anything else listed
        by list_services. GET is read mode; POST/PUT/PATCH need operate; DELETE needs admin.

        Parameters:
            service: Service name from list_services.
            method: HTTP method.
            path: API path, e.g. "/api/v3/system/status".
            query: Optional query parameters.
            json_body: Optional JSON body.
        """
        return do_service_api(service, method, path, query, json_body)
