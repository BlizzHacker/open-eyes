# Hermes and Open WebUI

## Hermes on Windows

Open Eyes is installed in the isolated environment at `%LOCALAPPDATA%\MoveWeight\OpenEyes` and registered with Hermes:

```powershell
hermes mcp test open-eyes
hermes mcp list
```

Hermes starts the desktop server in the signed-in session. After updating the package, restart Hermes or use `/reload-mcp`.

## Open WebUI on the Proxmox cluster

The Windows desktop runs the authenticated HTTP endpoint on loopback at port 8766. A scheduled task keeps an outbound SSH reverse tunnel to Slimmm (192.168.0.6). The host relay listens on 192.168.0.6:8766 and accepts connections only from the Open WebUI LXC (192.168.0.137), forwarding through SSH to the desktop endpoint.

Configure Open WebUI's MCP Streamable HTTP tool server to use:

- URL: `http://192.168.0.6:8766/mcp`
- Authorization: Bearer token from the Windows user's `OPEN_EYES_TOKEN` environment variable

Do not expose the relay publicly or open port 8766 to other LAN clients. Requests without the token receive HTTP 401. The token must stay out of source control, chat, and shell history. Desktop input and control tools can make real changes, so keep use task-scoped.
