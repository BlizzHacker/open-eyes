# Ops toolkit

Open Eyes 0.3 adds tools beyond the desktop: a shell, file editing, HTTP, SSH, Proxmox VE, the *arr media stack, qBittorrent, Plex, any HTTP service, and a vision bridge for text-only models. These are the same abilities a coding agent uses. They let an open model running in Hermes or Open WebUI manage a homelab directly, without typing into a terminal window it can only see as pixels.

## Tools

| Group (`OPEN_EYES_PROFILES`) | Tools | Purpose |
|---|---|---|
| `desktop` | screenshot, click, type_text, find_text, list_elements, … | See and drive the desktop (the original 40 tools) |
| `system` | run_command, read_file, write_file, edit_file, find_files, http_request | Shell, files and web on the Open Eyes machine |
| `remote` | ssh_run, ssh_transfer | Commands and file copies on configured hosts |
| `proxmox` | proxmox, guest_exec | Cluster overview, VM/LXC power, snapshots, tasks, exec inside guests |
| `services` | list_services, services_status, homelab_overview, service_api | Health checks and authenticated API calls for every configured service |
| `media` | arr, qbittorrent, plex | Sonarr, Radarr, Lidarr, Readarr, Prowlarr, Whisparr, ROMarr; downloads; Plex libraries |
| `vision` | look, describe_image | Screen or image → text through a vision model, so any model can "see" |

`OPEN_EYES_PROFILES` takes a comma-separated list: `all` (the default), `ops` (everything except the desktop), or individual groups. Fewer tools help small models choose well. With `ops`, Open Eyes runs headless and needs no display, so it can live on a server.

The server also sends MCP `instructions` that explain how to use the tools together. Clients that pass these on to the model give it a working playbook.

## Permission modes

| Mode | Allows |
|---|---|
| `read` (default) | GET requests, file reads, and an allow-list of read-only commands: `ls`, `cat`, `df`, `ps`, `journalctl`, `systemctl status`, `pct list/config`, `qm list`, `pvesh get`, `docker ps/logs`, `git status/log/diff`, `curl` without upload flags, PowerShell `Get-*` |
| `operate` | Any command, file writes, guest start/stop/snapshot/config, *arr commands, qBittorrent add/pause, Plex refresh, POST/PUT/PATCH |
| `admin` | Also destructive actions: `rm -rf`, `pct/qm destroy` and `rollback`, `zfs destroy`, `mkfs`, `dd` to a device, `git push --force`, `git reset --hard`, host reboot, SQL `DROP`, HTTP DELETE, snapshot rollback/delete, torrent delete |

The read-only check refuses `;`, `&&`, `||`, backticks, `$(…)`, redirects and background `&`, so a read-only command cannot chain into a write. When a tool is refused, the message names the mode it needs, and the model is told to report that rather than retry.

Set the mode with `[policy] mode` or `OPEN_EYES_MODE`. `open-eyes serve-http` uses `[policy] remote_mode` (default `read`), so a remote client such as Open WebUI stays read-only even when local Hermes is in `operate` mode. Desktop input tools can still type into any window, so give remote clients the `ops` profile when they should not touch the desktop.

When `audit_log` is set, every operate/admin action is appended to it as one JSON line.

## Configuration

Run `open-eyes init-config` to write a commented example, edit it, then run `open-eyes doctor`, which checks SSH, Proxmox and every service. The file is TOML, read from `$OPEN_EYES_CONFIG`, else `%LOCALAPPDATA%\MoveWeight\OpenEyes\config.toml` on Windows, else `~/.config/open-eyes/config.toml`. The full example is [src/openowl/ops/config.example.toml](../src/openowl/ops/config.example.toml).

```toml
[policy]
mode = "operate"
remote_mode = "read"
audit_log = "~/.open-eyes/audit.jsonl"

[ssh.pve1]
target = "root@192.168.1.10"

[proxmox]
ssh_host = "pve1"            # pvesh over SSH; or url + token_id + token_secret_env

[services.radarr]
kind = "arr"
url = "http://192.168.1.20:7878"
api_key_guest = { guest = "radarr", command = '''sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /var/lib/radarr/config.xml''' }
```

### Secrets without storing secrets

Any secret field `x` (`api_key`, `token`, `password`, `token_secret`) accepts:

- `x = "…"`: a literal (avoid)
- `x_env = "NAME"`: an environment variable
- `x_file = "path"`: first line of a file
- `x_cmd = "command"`: a local command's output
- `x_ssh = { host = "alias", command = "…" }`: a command run over SSH
- `x_guest = { guest = 105, command = "…" }`: a command run inside a Proxmox guest

The command forms run once per process and keep the value only in memory. With `x_guest`, an *arr API key is read straight from the app's own `config.xml` through Proxmox, so no key is ever copied to the machine that runs Open Eyes. Remote shell profiles are not loaded, and the last line of output is used.

### Services

`kind` is one of:

- `arr`: `app` is inferred from the service name; API `v3` for Sonarr/Radarr/Whisparr, `v1` otherwise; `X-Api-Key` auth.
- `qbittorrent`: `username` + `password`.
- `plex`: `token`.
- `http`: anything else. Set `auth` to `none`, `bearer`, `basic`, `x-api-key`, `header:<Name>` or `query:<param>`.

`health` overrides the path used by `services_status`.

### Proxmox

With `ssh_host`, Open Eyes runs `pvesh` on that node over SSH, so no API token is needed. `guest_exec` finds the node that hosts a guest and runs `pct exec` (LXC) or `qm guest exec` (VMs with the QEMU agent) there. Map node names to SSH aliases in `[proxmox.nodes]`. Unmapped nodes are reached at their cluster IP, hopping through `ssh_host`.

### Vision

`look` screenshots the desktop and sends it to `[vision]`. The response comes back as text, so models that cannot read images can still see. Supported: `api = "openai"` (Ollama `/v1`, LiteLLM, vLLM, LM Studio, OpenRouter), `"ollama"` (native `/api/chat`) and `"anthropic"` (Messages API). A local option is `ollama pull qwen2.5vl:7b`, then `base_url = "http://<ollama-host>:11434"`, `api = "ollama"`.

## Examples a model can run

```text
homelab_overview()
proxmox(action="overview", status="stopped")
guest_exec(target="radarr", command="journalctl -u radarr -n 50 --no-pager")
arr(service="sonarr", action="health")
arr(service="romarr", action="missing")
arr(service="radarr", action="search", item_id=42)          # operate
qbittorrent(service="qbittorrent", action="list", filter="stalled")
plex(service="plex", action="refresh", section="1")          # operate
service_api(service="yarrit", method="GET", path="/api/health")
ssh_run(host="pve1", command="zpool status")
look(question="Is there an error dialog on screen?")
```
