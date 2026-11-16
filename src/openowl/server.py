"""Open Eyes MCP server: desktop vision and control plus homelab operations."""

from __future__ import annotations

import hmac
import os
import subprocess
import sys
import tempfile

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from openowl import __version__
from openowl.ops import INSTRUCTIONS, parse_profiles, register_ops

_VERSION = __version__


class _BearerTokenMiddleware(BaseHTTPMiddleware):
    """Protect remote HTTP MCP calls with a shared bearer token."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request, call_next):
        if request.url.path == "/mcp" and not hmac.compare_digest(
            request.headers.get("authorization", ""), self._expected
        ):
            return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)


def _macos_preflight_permissions() -> None:
    """Check and request macOS Accessibility + Screen Recording permissions."""
    missing: list[str] = []

    try:
        from ApplicationServices import (  # type: ignore[import-untyped]
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
        if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
            missing.append("Accessibility")
    except Exception:
        try:
            from ApplicationServices import AXIsProcessTrusted  # type: ignore[import-untyped]
            if not AXIsProcessTrusted():
                missing.append("Accessibility")
                subprocess.Popen(
                    ["open", "x-apple.systempreferences:"
                     "com.apple.preference.security?Privacy_Accessibility"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    try:
        from Quartz import (  # type: ignore[import-untyped]
            CGPreflightScreenCaptureAccess,
            CGRequestScreenCaptureAccess,
        )
        if not CGPreflightScreenCaptureAccess():
            CGRequestScreenCaptureAccess()
            missing.append("Screen Recording")
    except ImportError:
        pass

    if missing:
        perms = " and ".join(missing)
        sys.stderr.write(
            f"[open-eyes] macOS permissions needed: {perms}\n"
            f"[open-eyes] Grant access in System Settings > Privacy & Security,\n"
            f"[open-eyes] then restart the server for changes to take effect.\n"
        )
    else:
        sys.stderr.write("[open-eyes] macOS permissions OK.\n")


def _register_desktop(mcp: FastMCP) -> None:
    """Register the screen/input tools. Imported lazily: they need a desktop session."""
    from openowl.screenshot_manager import ScreenshotManager
    from openowl.tools import (
        batch,
        desktop,
        framework_detect,
        input_tools,
        manage,
        ocr,
        screenshot,
        target_window,
        uac,
        ui_automation,
        visual_diff,
        watcher,
        windows,
        workflow,
    )

    screenshot_dir = os.path.join(tempfile.gettempdir(), "openowl_screenshots")
    screenshot.screenshot_manager = ScreenshotManager(screenshot_dir)

    for module in (
        screenshot, input_tools, windows, manage, uac, desktop, ui_automation,
        ocr, batch, framework_detect, target_window, visual_diff, watcher, workflow,
    ):
        module.register(mcp)


def _build_server(profiles: set[str] | None = None) -> FastMCP:
    if profiles is None:
        profiles = parse_profiles(os.environ.get("OPEN_EYES_PROFILES"))
    mcp = FastMCP("open-eyes", instructions=INSTRUCTIONS)
    if "desktop" in profiles:
        _register_desktop(mcp)
    register_ops(mcp, profiles)
    return mcp


def _apply_remote_mode() -> None:
    """Make remote clients run under policy.remote_mode, not the local mode.

    OPEN_EYES_MODE is ignored here on purpose: a leftover value in the shell must
    not silently lift the remote limit. Use OPEN_EYES_REMOTE_MODE to override.
    """
    from dataclasses import replace

    from openowl.ops.config import LEVELS, load_config, set_config

    cfg = load_config()
    mode = os.environ.get("OPEN_EYES_REMOTE_MODE", "").strip().lower() or cfg.policy.remote_mode
    if mode not in LEVELS:
        raise SystemExit(f"OPEN_EYES_REMOTE_MODE must be one of {LEVELS}, got '{mode}'.")
    set_config(replace(cfg, policy=replace(cfg.policy, mode=mode)))
    sys.stderr.write(f"[open-eyes] serve-http permission mode: {mode}\n")


_USAGE = """\
usage: open-eyes [command]

  (no command)      run the MCP server over stdio
  serve-http        run the MCP server over token-protected Streamable HTTP
  doctor            check the ops config, SSH hosts, Proxmox and services
  init-config       write an example ops config to the default location
  version           print the version

Environment: OPEN_EYES_PROFILES (all, ops, desktop, system, remote, proxmox,
services, media, vision), OPEN_EYES_MODE (read, operate, admin),
OPEN_EYES_REMOTE_MODE (serve-http mode; else [policy] remote_mode),
OPEN_EYES_CONFIG (config path), OPEN_EYES_TOKEN (serve-http).
"""


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command in ("--version", "-v", "version"):
        print(f"Open Eyes by Move Weight v{_VERSION}")
        return
    if command in ("--help", "-h", "help"):
        print(_USAGE)
        return
    if command == "doctor":
        from openowl.ops.doctor import run_doctor
        sys.exit(run_doctor())
    if command == "init-config":
        from openowl.ops.doctor import run_init_config
        sys.exit(run_init_config(force="--force" in sys.argv[2:]))

    if command == "serve-http":
        import argparse

        parser = argparse.ArgumentParser(description="Serve Open Eyes over protected HTTP MCP.")
        parser.add_argument("--host", default=os.environ.get("OPEN_EYES_HOST", "127.0.0.1"))
        parser.add_argument("--port", type=int, default=int(os.environ.get("OPEN_EYES_PORT", "8766")))
        parser.add_argument("--profiles", default=os.environ.get("OPEN_EYES_PROFILES", ""))
        args = parser.parse_args(sys.argv[2:])
        token = os.environ.get("OPEN_EYES_TOKEN", "")
        if len(token) < 32:
            parser.error("Set OPEN_EYES_TOKEN to a random value at least 32 characters long.")
        import uvicorn

        _apply_remote_mode()
        app = _build_server(parse_profiles(args.profiles)).streamable_http_app()
        app.add_middleware(_BearerTokenMiddleware, token=token)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
        return

    if command:
        print(_USAGE)
        sys.exit(2)

    profiles = parse_profiles(os.environ.get("OPEN_EYES_PROFILES"))
    if sys.platform == "darwin" and "desktop" in profiles:
        _macos_preflight_permissions()

    _build_server(profiles).run(transport="stdio")


if __name__ == "__main__":
    main()
