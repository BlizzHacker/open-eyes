# Open Eyes by Move Weight

Open Eyes gives Hermes and other MCP clients live vision and control of your Windows desktop: screenshots, OCR, accessibility controls, clicking, typing, window management, and repeatable workflows.

The desktop process runs on the Windows PC, where its screen and input devices are. Hermes can launch it persistently over local stdio. Open WebUI on a trusted server can connect over token-protected Streamable HTTP.

## Install on Windows

Python 3.10 or newer:

```powershell
$runtime = "$env:LOCALAPPDATA\MoveWeight\OpenEyes"
py -3.12 -m venv $runtime
& "$runtime\Scripts\python.exe" -m pip install --no-cache-dir "open-eyes-by-moveweight[windows] @ git+https://github.com/BlizzHacker/open-eyes.git"
hermes mcp add open-eyes --command "$runtime\Scripts\open-eyes.exe"
hermes mcp test open-eyes
```

Restart Hermes or use `/reload-mcp`. See [the Hermes and Open WebUI guide](docs/HERMES-OPENWEBUI.md).

## Vision

The Windows extra installs pywinauto, RapidOCR, and NumPy. Screenshot image results let vision-capable models read the screen; OCR and the accessibility tree provide structured text and control details. Keep the desktop signed in and unlocked. Windows UAC secure-desktop prompts remain outside ordinary automation.

## Open WebUI remote connection

Set a random `OPEN_EYES_TOKEN` of at least 32 characters, then run `open-eyes serve-http --host <private-desktop-ip> --port 8766`. Add an MCP Streamable HTTP server in Open WebUI at `http://<private-desktop-ip>:8766/mcp` with bearer authorization. Restrict the Windows firewall rule to the Open WebUI server. Use a private network or encrypted VPN; never port-forward the endpoint to the public internet. See the guide for persistence details.

## Development

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev,windows]"
pytest
ruff check .
```

## Attribution and license

Open Eyes is a Move Weight rebrand and continuation of [OpenOwl](https://github.com/mihir-kanzariya/openowl), created by Mihir Kanzariya under Apache-2.0. Original copyright and notices remain in [LICENSE](LICENSE) and [NOTICE](NOTICE). This repository preserves that attribution.
