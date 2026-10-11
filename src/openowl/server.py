"""openowl — desktop automation MCP server."""

from __future__ import annotations

import hmac
import os
import subprocess
import sys
import tempfile

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

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

_VERSION = "0.2.0"


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
            f"[openowl] macOS permissions needed: {perms}\n"
            f"[openowl] Grant access in System Settings > Privacy & Security,\n"
            f"[openowl] then restart the server for changes to take effect.\n"
        )
    else:
        sys.stderr.write("[openowl] macOS permissions OK.\n")


def _build_server() -> FastMCP:
    mcp = FastMCP("open-eyes")

    screenshot_dir = os.path.join(tempfile.gettempdir(), "openowl_screenshots")
    screenshot.screenshot_manager = ScreenshotManager(screenshot_dir)

    for module in (
        screenshot, input_tools, windows, manage, uac, desktop, ui_automation,
        ocr, batch, framework_detect, target_window, visual_diff, watcher, workflow,
    ):
        module.register(mcp)

    return mcp


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v", "version"):
        print(f"Open Eyes by Move Weight v{_VERSION}")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "serve-http":
        import argparse

        parser = argparse.ArgumentParser(description="Serve Open Eyes over protected HTTP MCP.")
        parser.add_argument("--host", default=os.environ.get("OPEN_EYES_HOST", "127.0.0.1"))
        parser.add_argument("--port", type=int, default=int(os.environ.get("OPEN_EYES_PORT", "8766")))
        args = parser.parse_args(sys.argv[2:])
        token = os.environ.get("OPEN_EYES_TOKEN", "")
        if len(token) < 32:
            parser.error("Set OPEN_EYES_TOKEN to a random value at least 32 characters long.")
        import uvicorn

        app = _build_server().streamable_http_app()
        app.add_middleware(_BearerTokenMiddleware, token=token)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
        return

    if sys.platform == "darwin":
        _macos_preflight_permissions()

    mcp = _build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
