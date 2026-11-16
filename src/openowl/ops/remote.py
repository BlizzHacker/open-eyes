"""SSH tools: run commands on and copy files to/from configured hosts.

Uses the system ``ssh``/``scp`` clients in BatchMode, so keys and agents
already set up for the user work unchanged and no password is ever typed.
"""

from __future__ import annotations

import re
import shlex

from openowl.ops import policy
from openowl.ops.config import ConfigError, SshHost, get_config
from openowl.ops.system import default_timeout, run_process

_BASE_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
              "-o", "ServerAliveInterval=15", "-o", "StrictHostKeyChecking=accept-new"]
# user@host only; a leading "-" would be parsed by ssh as an option.
_RAW_TARGET = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9_\[][A-Za-z0-9._:\]-]*$")


def resolve_host(host: str) -> SshHost:
    """Map an alias from [ssh] (or a raw user@host) to an SshHost."""
    hosts = get_config().ssh
    if host in hosts:
        return hosts[host]
    if _RAW_TARGET.match(host):
        return SshHost(name=host, target=host)
    known = ", ".join(sorted(hosts)) or "none configured"
    raise ConfigError(f"Unknown SSH host '{host}'. Configured hosts: {known} (or pass user@host).")


def _jump_spec(host: SshHost) -> str:
    """Expand a jump that names another alias into its target."""
    if not host.jump:
        return ""
    hops = []
    for hop in host.jump.split(","):
        hop = hop.strip()
        alias = get_config().ssh.get(hop)
        hops.append(alias.target if alias else hop)
    return ",".join(hops)


def _host_opts(host: SshHost, port_flag: str) -> list[str]:
    opts = list(_BASE_OPTS)
    if host.port:
        opts += [port_flag, str(host.port)]
    if host.identity_file:
        opts += ["-i", host.identity_file]
    jump = _jump_spec(host)
    if jump:
        opts += ["-J", jump]
    return opts


def ssh_argv(host: SshHost, remote_command: str | None = None) -> list[str]:
    argv = ["ssh", *_host_opts(host, "-p"), host.target]
    if remote_command is not None:
        argv += ["--", remote_command]
    return argv


def scp_argv(host: SshHost, source: str, dest: str) -> list[str]:
    return ["scp", *_host_opts(host, "-P"), "-r", source, dest]


def run_on_host(host: str, command: str, timeout: float = 0, action: str = "ssh_run") -> str:
    """Screen *command* against policy and run it on *host*."""
    policy.screen_command(command, action)
    target = resolve_host(host)
    policy.audit(action, host=target.target, command=command)
    return run_process(ssh_argv(target, command), timeout=default_timeout(timeout))


def do_ssh_transfer(host: str, direction: str, remote_path: str, local_path: str,
                    timeout: float = 600) -> str:
    direction = direction.lower()
    if direction not in ("upload", "download"):
        raise ValueError("direction must be 'upload' or 'download'.")
    target = resolve_host(host)
    local = str(policy.check_path(local_path))
    policy.authorize(policy.OPERATE, f"ssh_transfer {direction}", "ssh_transfer",
                     host=target.target, remote=remote_path, local=local)
    remote = f"{target.target}:{shlex.quote(remote_path)}"
    src, dst = (local, remote) if direction == "upload" else (remote, local)
    return run_process(scp_argv(target, src, dst), timeout=timeout)


def register(server) -> None:
    @server.tool()
    def ssh_run(host: str, command: str, timeout: float = 0) -> str:
        """Run a shell command on a remote host over SSH (key auth, no prompts).

        Read mode allows read-only commands (df, ls, systemctl status, pct list,
        journalctl ...). Anything else needs operate mode; destructive commands
        (rm -rf, pct destroy, mkfs, reboot ...) need admin mode.

        Parameters:
            host: An alias from the [ssh] config section (e.g. "slimmm") or user@host.
            command: Command line to run remotely (POSIX shell).
            timeout: Seconds before giving up (default from config).
        """
        return run_on_host(host, command, timeout)

    @server.tool()
    def ssh_transfer(host: str, direction: str, remote_path: str, local_path: str) -> str:
        """Copy a file or folder to ("upload") or from ("download") a host with scp (operate mode).

        Parameters:
            host: SSH alias or user@host.
            direction: "upload" (local -> remote) or "download" (remote -> local).
            remote_path: Path on the remote host.
            local_path: Path on this machine.
        """
        return do_ssh_transfer(host, direction, remote_path, local_path)
