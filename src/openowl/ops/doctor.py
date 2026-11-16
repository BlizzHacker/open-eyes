"""``open-eyes doctor`` and ``open-eyes init-config``: check and bootstrap the ops config."""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from importlib import resources

from openowl.ops.config import ConfigError, default_config_path, get_config
from openowl.ops.remote import ssh_argv
from openowl.ops.services import check_services, do_list_services, format_status
from openowl.ops.system import exec_process


def _check_ssh(host) -> tuple[bool, str]:
    _, err, code = exec_process(ssh_argv(host, "true"), timeout=20)
    if code == 0:
        return True, f"  OK   {host.name} ({host.target})"
    reason = err.strip().splitlines()[-1] if err.strip() else code
    return False, f"  FAIL {host.name} ({host.target}): {reason}"


def run_doctor() -> int:
    try:
        cfg = get_config()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 2
    print(do_list_services())
    problems = 0
    if not cfg.loaded:
        print(f"\nNo config at {cfg.path}. Run `open-eyes init-config` to create one.")
        return 1
    for tool in ("ssh", "scp"):
        if not shutil.which(tool):
            print(f"\n{tool} is not on PATH; SSH, guest_exec and pvesh tools will fail.")
            problems += 1
    if cfg.ssh:
        print("\nSSH:")
        with ThreadPoolExecutor(max_workers=8) as pool:
            for ok, line in pool.map(_check_ssh, cfg.ssh.values()):
                print(line)
                problems += not ok
    if cfg.proxmox.configured:
        from openowl.ops.proxmox import guests
        try:
            items = guests()
            running = sum(1 for g in items if g.get("status") == "running")
            print(f"\nProxmox: OK, {len(items)} guests ({running} running)")
        except Exception as exc:
            print(f"\nProxmox: FAIL {exc}")
            problems += 1
    if cfg.services:
        results = check_services()
        print("\n" + format_status(results))
        problems += sum(1 for r in results if not r["ok"])
    if cfg.vision.configured:
        print(f"\nVision: {cfg.vision.model} via {cfg.vision.api} (not called by doctor)")
    else:
        print("\nVision: not configured; text-only models cannot use `look`.")
    print(f"\n{'All checks passed.' if not problems else f'{problems} problem(s) found.'}")
    return 0 if not problems else 1


def run_init_config(force: bool = False) -> int:
    target = default_config_path()
    if target.exists() and not force:
        print(f"{target} already exists; pass --force to overwrite.")
        return 1
    template = resources.files("openowl.ops").joinpath("config.example.toml").read_text(encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template, encoding="utf-8")
    print(f"Wrote {target}. Edit it, then run `open-eyes doctor`.")
    return 0
