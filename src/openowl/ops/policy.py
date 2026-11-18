"""Permission levels, command screening, audit logging and output shaping.

Three levels, set by ``[policy] mode`` or ``$OPEN_EYES_MODE``:

``read``     look only: GET requests, file reads, and an allow-list of
             read-only shell commands (``ls``, ``df``, ``pct list`` ...).
``operate``  change things: any command, file writes, guest start/stop,
             *arr commands, non-GET API calls. Destructive patterns are
             still refused.
``admin``    everything, including destructive commands and DELETE calls.

Every operate/admin action is appended to the audit log when one is set.
"""

from __future__ import annotations

import json
import re
import shlex
import threading
import time
from pathlib import Path

from openowl.ops.config import LEVELS, get_config


class PolicyError(PermissionError):
    """Raised when the configured mode does not allow an action."""


READ, OPERATE, ADMIN = 0, 1, 2

# Commands a model may run in read mode. Matched against the first word of
# every pipeline segment; arguments are screened separately.
READONLY_COMMANDS = frozenset({
    "ls", "dir", "cat", "head", "tail", "less", "wc", "grep", "egrep", "rg", "find",
    "stat", "file", "du", "df", "free", "uptime", "uname", "hostname", "whoami", "id",
    "date", "ps", "top", "pgrep", "ss", "netstat", "ip", "ping", "dig", "nslookup",
    "host", "lsblk", "blkid", "findmnt", "mount", "lscpu", "lsusb", "lspci", "sensors",
    "nproc", "env", "printenv", "echo", "sort", "uniq", "cut", "awk", "sed", "tr", "jq",
    "column", "basename", "dirname", "realpath", "readlink", "which", "type", "md5sum",
    "sha256sum", "journalctl", "dmesg", "zpool", "zfs", "pvs", "vgs", "lvs", "smartctl",
    "pct", "qm", "pvesh", "pvecm", "pveversion", "pvesm", "ha-manager", "docker",
    "systemctl", "git", "curl", "true",
    # PowerShell read verbs (matched case-insensitively)
    "get-childitem", "get-content", "get-item", "get-process", "get-service",
    "get-volume", "get-psdrive", "get-date", "get-computerinfo", "test-connection",
    "test-path", "select-string", "select-object", "where-object", "measure-object",
    "format-table", "format-list", "sort-object", "resolve-dnsname", "get-netipaddress",
})

# Subcommands that keep an otherwise read-only tool read-only.
_READONLY_SUBCOMMANDS = {
    "pct": {"list", "config", "status", "df", "listsnapshot", "pending", "cpusets"},
    "qm": {"list", "config", "status", "listsnapshot", "pending", "showcmd"},
    "pvesh": {"get", "ls", "usage"},
    "pvesm": {"status", "list", "path"},
    "ha-manager": {"status", "config"},
    "pvecm": {"status", "nodes"},
    "docker": {"ps", "images", "logs", "inspect", "stats", "version", "info", "top", "port"},
    "systemctl": {"status", "list-units", "list-timers", "is-active", "is-enabled",
                  "is-failed", "show", "cat", "list-unit-files"},
    "git": {"status", "log", "diff", "show", "branch", "remote", "rev-parse",
            "describe", "ls-files", "blame", "tag", "worktree", "stash"},
    "zpool": {"status", "list", "iostat", "get", "history"},
    "zfs": {"list", "get"},
    "ip": {"a", "addr", "address", "r", "route", "link", "neigh", "-br", "-4", "-6", "-j"},
}

# curl may only fetch: these flags send data or write files.
_CURL_WRITE_FLAGS = re.compile(r"(^|\s)(-d|--data\S*|-F|--form\S*|-T|--upload-file|-X|--request|-o|--output|-O|--remote-name)(\s|=|$)")
_SED_INPLACE = re.compile(r"(^|\s)(-i\S*|--in-place\S*)(\s|$)")
_FIND_WRITE = re.compile(r"(^|\s)(-delete|-exec|-execdir|-ok|-fprint\S*)(\s|$)")

# Shell constructs that can chain or redirect into an unscreened command.
_UNSAFE_SHELL = re.compile(r"(;|&&|\|\||(?<![|])&(?!&)|`|\$\(|>|<\(|\n)")

DESTRUCTIVE_PATTERNS = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+"), "recursive/forced rm"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "mkfs"),
    (re.compile(r"\bdd\b.*\bof=/dev/"), "dd to a device"),
    (re.compile(r"\bwipefs\b|\bsgdisk\b.*--zap|\bblkdiscard\b"), "disk wipe"),
    (re.compile(r"\b(pct|qm)\s+(destroy|rollback)\b"), "guest destroy/rollback"),
    (re.compile(r"\bpvesh\s+delete\b|\bpvesh\s+create\s+\S*/rollback\b"), "pvesh delete/rollback"),
    (re.compile(r"\b(zfs|zpool)\s+destroy\b"), "ZFS destroy"),
    (re.compile(r"\b(lvremove|vgremove|pvremove)\b"), "LVM remove"),
    (re.compile(r"\bpvecm\s+(delnode|expected)\b"), "cluster membership change"),
    (re.compile(r"(^|[\s;&|])(shutdown|poweroff|halt|reboot)(\s|$)"), "host power-off"),
    (re.compile(r"\bgit\s+push\b.*(--force|-f\b|--mirror)"), "force push"),
    (re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)"), "git hard reset/clean"),
    (re.compile(r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", re.I), "SQL drop/truncate"),
    (re.compile(r">\s*/dev/(sd|nvme|vd|hd)"), "write to block device"),
    (re.compile(r"\bchmod\s+-R\s+0*777\s+/(\s|$)|\bchown\s+-R\s+\S+\s+/(\s|$)"), "recursive perms on /"),
    (re.compile(r"Remove-Item\b.*-Recurse", re.I), "recursive Remove-Item"),
    (re.compile(r"\bFormat-(Volume|Disk)\b|\bClear-Disk\b", re.I), "disk format"),
]


def require(level: int, action: str) -> None:
    """Raise PolicyError unless the configured mode allows *level*."""
    policy = get_config().policy
    if policy.level < level:
        raise PolicyError(
            f"'{action}' needs mode '{LEVELS[level]}', but Open Eyes is in '{policy.mode}' mode. "
            f"Ask the user to raise [policy] mode in {get_config().path or 'the config'} "
            f"(or set OPEN_EYES_MODE) if this change is intended."
        )


def authorize(level: int, action: str, tool: str, **fields) -> None:
    """Check *level* against the mode and audit the action when it changes state."""
    if level > READ:
        require(level, action)
        audit(tool, action=action, **fields)


def authorize_method(method: str, action: str, tool: str, **fields) -> None:
    """authorize() for an HTTP call, with the level taken from its method."""
    authorize(method_level(method), f"{action} {method.upper()}", tool, **fields)


def exit_reason(code: int | None) -> str:
    return "timed out" if code is None else f"exit {code}"


def destructive_reason(command: str) -> str | None:
    """Return why *command* is destructive, or None."""
    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return reason
    return None


def _segment_is_readonly(segment: str) -> bool:
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        return False
    # Allow leading VAR=value assignments only for LC_ALL/LANG/TZ style env.
    while words and re.fullmatch(r"(LC_\w+|LANG|TZ|COLUMNS)=\S*", words[0]):
        words = words[1:]
    if not words:
        return True
    if words[0] in ("sudo", "nice", "timeout"):
        words = words[1:]
        if words and words[0].replace(".", "").isdigit():
            words = words[1:]
    if not words:
        return False
    cmd = Path(words[0]).name.lower()
    if cmd not in READONLY_COMMANDS:
        return False
    args = " ".join(words[1:])
    subs = _READONLY_SUBCOMMANDS.get(cmd)
    if subs is not None:
        first = next((w for w in words[1:] if not w.startswith("-") or cmd == "ip"), "")
        if cmd == "docker" and first == "compose":
            rest = [w for w in words[2:] if not w.startswith("-")]
            return bool(rest) and rest[0] in {"ps", "logs", "config", "ls", "images", "top"}
        if first.lower() not in subs:
            return False
        if cmd == "git" and first in {"branch", "tag", "worktree", "stash", "remote"}:
            extra = words[words.index(first) + 1:]
            allowed = {"branch": {"-a", "-r", "-v", "-vv", "--list", "--show-current"},
                       "tag": {"-l", "--list"}, "worktree": {"list"},
                       "stash": {"list", "show"}, "remote": {"-v", "show", "get-url"}}[first]
            if extra and extra[0] not in allowed:
                return False
    if cmd == "curl" and _CURL_WRITE_FLAGS.search(args):
        return False
    if cmd == "sed" and _SED_INPLACE.search(args):
        return False
    if cmd == "find" and _FIND_WRITE.search(args):
        return False
    if cmd == "awk" and re.search(r"system\s*\(|print\s*>|\|\s*\"", args):
        return False
    return True


def is_readonly_command(command: str) -> bool:
    """True when every pipeline segment of *command* is an allow-listed read."""
    if _UNSAFE_SHELL.search(command.replace("2>&1", "").replace("2>/dev/null", "")):
        return False
    return all(_segment_is_readonly(seg) for seg in command.split("|"))


def screen_command(command: str, action: str) -> None:
    """Enforce policy for a shell command run locally, over SSH or in a guest."""
    if not command.strip():
        raise ValueError("Command is empty.")
    if is_readonly_command(command):
        return
    reason = destructive_reason(command)
    if reason:
        require(ADMIN, f"{action} ({reason})")
        return
    require(OPERATE, action)


def method_level(method: str) -> int:
    method = method.upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return READ
    if method == "DELETE":
        return ADMIN
    return OPERATE


def check_path(path: str | Path) -> Path:
    """Resolve *path* and confirm it sits under an allowed root (if any are set)."""
    resolved = Path(path).expanduser().resolve()
    roots = get_config().policy.allow_paths
    if roots and not any(_is_within(resolved, Path(r).resolve()) for r in roots):
        raise PolicyError(
            f"{resolved} is outside the allowed paths: {', '.join(roots)}."
        )
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


_audit_lock = threading.Lock()


def audit(tool: str, **fields) -> None:
    """Append one JSON line describing a state-changing action."""
    log = get_config().policy.audit_log
    if not log:
        return
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, **fields}
    try:
        path = Path(log)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass  # auditing must never break the action itself


def clip(text: str) -> str:
    """Trim *text* to the configured output budget, keeping head and tail."""
    limit = get_config().policy.max_output_chars
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    dropped = len(text) - head - tail
    return f"{text[:head]}\n\n... [{dropped} characters omitted] ...\n\n{text[-tail:]}"


def to_text(data) -> str:
    """Render tool output as compact, model-friendly text."""
    if isinstance(data, str):
        return clip(data)
    return clip(json.dumps(data, indent=1, default=str, ensure_ascii=False))
