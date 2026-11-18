"""Open Eyes ops toolkit: system, SSH, Proxmox, homelab services, media stack and vision.

Tool groups ("profiles") are chosen with ``$OPEN_EYES_PROFILES`` (comma
separated). ``all`` (the default) enables everything; ``ops`` enables every
group except the desktop tools, which lets Open Eyes run headless on a server.
"""

from __future__ import annotations

from importlib import import_module

OPS_PROFILES = ("system", "remote", "proxmox", "services", "media", "vision")
ALL_PROFILES = ("desktop", *OPS_PROFILES)

INSTRUCTIONS = """\
Open Eyes gives you eyes and hands on the user's computer and homelab.

Start with list_services to learn what is configured and which permission mode
is active (read = look only, operate = change things, admin = destructive).
A refused action explains which mode it needs: tell the user, do not retry.

Seeing: screenshot returns an image. If you cannot read images, call look
(it asks a vision model and answers in text). find_text / list_elements give
OCR text and accessibility controls with coordinates.

Doing on the desktop: click, click_text, click_element, type_text, send_keys,
focus_window, launch_app. Verify with another look/screenshot after acting.

System: run_command, read_file, write_file, edit_file, find_files, http_request.
Prefer these over typing into a terminal window.

Homelab: homelab_overview for the big picture; proxmox for nodes, VMs and LXC
containers (status, start/stop, snapshots, tasks); guest_exec to run a command
inside a container or VM; ssh_run for any host; arr / qbittorrent / plex for the
media stack; service_api for any configured service (ROMarr, Yarrit, Forgeverse,
RomM, Authentik ...); services_status for health.

Work in small verified steps: read state, act once, check the result.
"""


def parse_profiles(value: str | None) -> set[str]:
    """Turn a comma-separated profile list into a set of known profile names."""
    if not value or not value.strip():
        return set(ALL_PROFILES)
    wanted: set[str] = set()
    for part in value.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name == "all":
            wanted |= set(ALL_PROFILES)
        elif name == "ops":
            wanted |= set(OPS_PROFILES)
        elif name in ALL_PROFILES:
            wanted.add(name)
        else:
            raise ValueError(f"Unknown profile '{name}'. Use: all, ops, {', '.join(ALL_PROFILES)}.")
    return wanted


def register_ops(server, profiles: set[str]) -> None:
    """Register the ops tool groups in *profiles* on *server* (module name = profile name)."""
    for name in OPS_PROFILES:
        if name not in profiles:
            continue
        module = import_module(f"openowl.ops.{name}")
        if name == "vision":
            module.register(server, desktop="desktop" in profiles)  # `look` needs the screen
        else:
            module.register(server)
