# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-11-16

### Added
- Ops toolkit (`openowl.ops`), 21 new tools in six groups selectable with `OPEN_EYES_PROFILES`:
  - system: run_command, read_file, write_file, edit_file, find_files, http_request
  - remote: ssh_run, ssh_transfer
  - proxmox: proxmox (overview, power, snapshots, tasks, raw API), guest_exec (pct/qm exec on the right node)
  - services: list_services, services_status, homelab_overview, service_api
  - media: arr (Sonarr, Radarr, Lidarr, Readarr, Prowlarr, Whisparr, ROMarr), qbittorrent, plex
  - vision: look and describe_image, which route screenshots through a vision model for text-only models
- Permission modes `read` / `operate` / `admin`: a read-only command allow-list, destructive-pattern
  screening, an audit log, and a separate `remote_mode` for `serve-http`.
- TOML config with secrets taken from the environment, files, commands, SSH, or inside a Proxmox guest
  (`*_guest`), so API keys are read where they live and never stored locally.
- `open-eyes doctor` and `open-eyes init-config` commands; MCP server instructions for models.
- Headless mode: desktop tools load only when the `desktop` profile is enabled.
- Python 3.10 support for the new config parser (`tomli`), and a Linux classifier.

### Fixed
- `manage_screenshots` imported a non-existent `server` module.

## [0.2.0] — 2026-10-11

### Added
- Open Eyes by Move Weight branding with Apache-2.0 attribution retained.
- Hermes stdio setup and a bearer-token-protected Streamable HTTP endpoint for remote MCP clients.
- Windows console alias open-eyes, preserving openowl for compatibility.

## [0.1.0] — Unreleased

Initial public release.

### Added
- FastMCP-based MCP server exposing 40 tools for desktop automation:
  screenshots, clicking, typing, OCR, window management, accessibility-tree
  queries, workflow recording/replay, framework detection, screen diffing,
  window watchers, virtual desktops.
- macOS backend via PyObjC (Accessibility + Screen Recording APIs).
- Windows backend via pywinauto + Win32.
- `pip install openowl` and `uvx openowl` installation paths.
- Console entry point: `openowl` (stdio transport).
- macOS permissions preflight at startup with system-settings deep-link.
- Workflow data dir: `~/.openowl/workflows/`.
