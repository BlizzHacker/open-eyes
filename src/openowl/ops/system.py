"""Local system tools: shell, files, search and HTTP.

These mirror the everyday abilities of a coding agent so that a model driving
Open Eyes does not have to type into a terminal window to inspect a file or
run a command.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import httpx

from openowl.ops import policy
from openowl.ops.config import USER_AGENT, get_config

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", "target"}
MAX_READ_BYTES = 2_000_000


# ------------------------------------------------------------------
# Shell
# ------------------------------------------------------------------

def shell_argv(command: str, shell: str = "auto") -> list[str]:
    """Build the argv that runs *command* in the requested shell."""
    shell = shell.lower()
    if shell == "auto":
        shell = "powershell" if sys.platform == "win32" else "bash"
    if shell in ("powershell", "pwsh"):
        exe = "pwsh" if shell == "pwsh" else "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "cmd":
        return ["cmd", "/d", "/s", "/c", command]
    if shell in ("bash", "sh"):
        return [shell, "-c", command]  # not a login shell: profiles print banners
    raise ValueError(f"Unknown shell '{shell}'. Use auto, bash, sh, powershell, pwsh or cmd.")


def format_process(proc_stdout: str, proc_stderr: str, code: int | None, timeout: float) -> str:
    """Render a finished process; a None *code* means it timed out."""
    parts = []
    if proc_stdout.strip():
        parts.append(proc_stdout.rstrip())
    if proc_stderr.strip():
        parts.append(f"[stderr]\n{proc_stderr.rstrip()}")
    if code is None:
        parts.append(f"[timed out after {timeout:g}s]")
    else:
        parts.append(f"[exit {code}]")
    return policy.clip("\n".join(parts))


def exec_process(argv: list[str], timeout: float,
                 cwd: str | None = None) -> tuple[str, str, int | None]:
    """Run *argv*; return (stdout, stderr, exit code). The code is None on timeout."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return out, err, None
    except FileNotFoundError as exc:
        return "", f"could not start {argv[0]}: {exc}", 127
    return proc.stdout, proc.stderr, proc.returncode


def run_process(argv: list[str], timeout: float, cwd: str | None = None) -> str:
    out, err, code = exec_process(argv, timeout, cwd)
    return format_process(out, err, code, timeout)


def default_timeout(timeout: float) -> float:
    return timeout or get_config().policy.command_timeout


def do_run_command(command: str, cwd: str = "", timeout: float = 0, shell: str = "auto") -> str:
    policy.screen_command(command, "run_command")
    workdir = str(policy.check_path(cwd)) if cwd else None
    policy.audit("run_command", command=command, cwd=workdir)
    return run_process(shell_argv(command, shell), timeout=default_timeout(timeout), cwd=workdir)


# ------------------------------------------------------------------
# Files
# ------------------------------------------------------------------

def do_read_file(path: str, offset: int = 1, limit: int = 400) -> str:
    target = policy.check_path(path)
    if target.is_dir():
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = [f"{p.name}/" if p.is_dir() else f"{p.name}  ({p.stat().st_size} bytes)"
                 for p in entries[:500]]
        more = f"\n... {len(entries) - 500} more" if len(entries) > 500 else ""
        return f"Directory {target} ({len(entries)} entries):\n" + "\n".join(lines) + more
    if not target.exists():
        raise FileNotFoundError(f"{target} does not exist.")
    size = target.stat().st_size
    with target.open("rb") as fh:
        raw = fh.read(MAX_READ_BYTES)
    if b"\x00" in raw[:4096]:
        return f"{target} is binary ({size} bytes); not shown."
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, offset)
    chunk = lines[start - 1:start - 1 + max(1, limit)]
    body = "\n".join(f"{start + i:>6}\t{line}" for i, line in enumerate(chunk))
    tail = ""
    if start - 1 + len(chunk) < len(lines):
        tail = f"\n[showing lines {start}-{start + len(chunk) - 1} of {len(lines)}; pass offset to read more]"
    return policy.clip(f"{target}\n{body}{tail}")


def do_write_file(path: str, content: str, append: bool = False) -> str:
    target = policy.check_path(path)
    policy.authorize(policy.OPERATE, "write_file", "write_file", path=str(target),
                     chars=len(content), append=append)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding="utf-8", newline="") as fh:
        fh.write(content)
    return f"{'Appended' if append else 'Wrote'} {len(content)} characters to {target}."


def do_edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    target = policy.check_path(path)
    policy.require(policy.OPERATE, "edit_file")
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)
    if not old_text or count == 0:
        raise ValueError(f"old_text was not found in {target}. Read the file and copy it exactly.")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_text occurs {count} times in {target}. Add surrounding lines to make it "
            f"unique, or pass replace_all=true."
        )
    updated = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
    target.write_text(updated, encoding="utf-8", newline="")
    policy.audit("edit_file", path=str(target), replacements=count if replace_all else 1)
    return f"Replaced {count if replace_all else 1} occurrence(s) in {target}."


def do_find_files(root: str, pattern: str = "*", contains: str = "",
                  max_results: int = 200) -> str:
    base = policy.check_path(root)
    if not base.is_dir():
        raise NotADirectoryError(f"{base} is not a directory.")
    regex = re.compile(contains, re.IGNORECASE) if contains else None
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), base)
            if not (fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel.replace("\\", "/"), pattern)):
                continue
            full = Path(dirpath) / name
            if regex is None:
                results.append(rel)
            else:
                try:
                    if full.stat().st_size > MAX_READ_BYTES:
                        continue
                    with full.open("r", encoding="utf-8", errors="ignore") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if regex.search(line):
                                results.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                                if len(results) >= max_results:
                                    break
                except OSError:
                    continue
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break
    if not results:
        return f"No matches under {base} for pattern={pattern!r} contains={contains!r}."
    suffix = f"\n[stopped at {max_results} results]" if len(results) >= max_results else ""
    return policy.clip(f"{len(results)} result(s) under {base}:\n" + "\n".join(results) + suffix)


# ------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    _HIDDEN = {"script", "style", "noscript", "svg"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "header", "footer", "pre", "table"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._HIDDEN and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    title = parser.title.strip()
    return f"# {title}\n\n{text}" if title else text


def format_response(resp: httpx.Response, raw: bool = False) -> str:
    ctype = resp.headers.get("content-type", "")
    head = f"HTTP {resp.status_code} {resp.reason_phrase} ({ctype or 'no content-type'}, {len(resp.content)} bytes)"
    if not resp.content:
        return head
    if "json" in ctype:
        try:
            return head + "\n" + policy.to_text(resp.json())
        except ValueError:
            pass
    if "html" in ctype and not raw:
        return head + "\n" + policy.clip(html_to_text(resp.text))
    if ctype.startswith(("text/", "application/xml", "application/javascript")) or not ctype:
        return head + "\n" + policy.clip(resp.text)
    return head + "\n[binary body not shown]"


def do_http_request(url: str, method: str = "GET", headers: dict | None = None,
                    body: str = "", json_body: dict | list | None = None,
                    timeout: float = 30, raw: bool = False) -> str:
    method = method.upper()
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    policy.authorize_method(method, "http_request", "http_request", url=url)
    with httpx.Client(follow_redirects=True, timeout=timeout,
                      headers={"User-Agent": USER_AGENT}) as client:
        resp = client.request(method, url, headers=headers or {},
                              content=body or None, json=json_body)
    return format_response(resp, raw=raw)


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------

def register(server) -> None:
    @server.tool()
    def run_command(command: str, cwd: str = "", timeout: float = 0, shell: str = "auto") -> str:
        """Run a shell command on the machine running Open Eyes and return its output.

        Read mode allows only read-only commands (ls, cat, df, git status,
        Get-ChildItem ...); operate mode allows anything non-destructive.

        Parameters:
            command: The command line to run.
            cwd: Working directory (optional).
            timeout: Seconds before the command is killed (default from config, 120).
            shell: auto (PowerShell on Windows, bash elsewhere), bash, sh, powershell, pwsh, cmd.
        """
        return do_run_command(command, cwd, timeout, shell)

    @server.tool()
    def read_file(path: str, offset: int = 1, limit: int = 400) -> str:
        """Read a text file with line numbers, or list a directory.

        Parameters:
            path: File or directory path.
            offset: First line to show (1-based).
            limit: Maximum number of lines to show.
        """
        return do_read_file(path, offset, limit)

    @server.tool()
    def write_file(path: str, content: str, append: bool = False) -> str:
        """Create or overwrite a text file (operate mode). Parent folders are created.

        Parameters:
            path: File path.
            content: Full new content (or text to append).
            append: Append instead of overwriting.
        """
        return do_write_file(path, content, append)

    @server.tool()
    def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Replace exact text in a file (operate mode). old_text must match exactly and be unique.

        Parameters:
            path: File path.
            old_text: Exact existing text, including whitespace.
            new_text: Replacement text.
            replace_all: Replace every occurrence instead of requiring one.
        """
        return do_edit_file(path, old_text, new_text, replace_all)

    @server.tool()
    def find_files(root: str, pattern: str = "*", contains: str = "",
                   max_results: int = 200) -> str:
        """Find files by name glob and optionally search their contents with a regex.

        Parameters:
            root: Directory to search.
            pattern: Filename or relative-path glob, e.g. "*.py" or "src/**/*.ts".
            contains: Optional regex; when set, returns matching lines as path:line: text.
            max_results: Stop after this many results.
        """
        return do_find_files(root, pattern, contains, max_results)

    @server.tool()
    def http_request(url: str, method: str = "GET", headers: dict | None = None,
                     body: str = "", json_body: dict | list | None = None,
                     timeout: float = 30, raw: bool = False) -> str:
        """Fetch a URL or call an HTTP API. HTML pages come back as readable text.

        GET is allowed in read mode; POST/PUT/PATCH need operate; DELETE needs admin.
        For configured homelab services prefer service_api, which adds their auth.

        Parameters:
            url: Full http(s) URL.
            method: HTTP method.
            headers: Optional request headers.
            body: Optional raw request body.
            json_body: Optional JSON body (sets content-type).
            timeout: Seconds to wait.
            raw: Return HTML source instead of extracted text.
        """
        return do_http_request(url, method, headers, body, json_body, timeout, raw)
