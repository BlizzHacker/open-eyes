"""Local shell/file/HTTP tools and SSH argument building."""

import sys

import httpx
import pytest

from openowl.ops import policy, remote, system
from openowl.ops.config import ConfigError


def test_read_file_numbers_lines_and_pages(ops_config, tmp_path):
    ops_config()
    f = tmp_path / "a.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")
    out = system.do_read_file(str(f), offset=3, limit=2)
    assert "     3\tline3" in out and "     4\tline4" in out
    assert "line5" not in out
    assert "of 10" in out


def test_read_file_lists_directory_and_skips_binary(ops_config, tmp_path):
    ops_config()
    (tmp_path / "sub").mkdir()
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02")
    assert "sub/" in system.do_read_file(str(tmp_path))
    assert "binary" in system.do_read_file(str(tmp_path / "bin.dat"))


def test_write_and_edit_need_operate(ops_config, tmp_path):
    target = tmp_path / "n.txt"
    ops_config(mode="read")
    with pytest.raises(policy.PolicyError):
        system.do_write_file(str(target), "x")
    ops_config(mode="operate")
    system.do_write_file(str(target), "alpha beta alpha")
    with pytest.raises(ValueError, match="2 times"):
        system.do_edit_file(str(target), "alpha", "gamma")
    system.do_edit_file(str(target), "beta", "delta")
    system.do_edit_file(str(target), "alpha", "omega", replace_all=True)
    assert target.read_text(encoding="utf-8") == "omega delta omega"
    with pytest.raises(ValueError, match="not found"):
        system.do_edit_file(str(target), "missing", "x")


def test_find_files_by_name_and_content(ops_config, tmp_path):
    ops_config()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("import os\nTOKEN = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.txt").write_text("TOKEN here\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "c.py").write_text("TOKEN\n", encoding="utf-8")
    names = system.do_find_files(str(tmp_path), "*.py")
    assert "a.py" in names and "c.py" not in names
    hits = system.do_find_files(str(tmp_path), "*", contains="token")
    assert "a.py:2:" in hits and "b.txt:1:" in hits


def test_run_command_read_mode_allows_reads_only(ops_config):
    ops_config(mode="read")
    out = system.do_run_command("echo hello", shell="sh" if sys.platform != "win32" else "auto")
    assert "hello" in out and "[exit 0]" in out
    with pytest.raises(policy.PolicyError):
        system.do_run_command("touch /tmp/x")


def test_run_command_timeout(ops_config):
    ops_config(mode="operate")
    argv = [sys.executable, "-c", "import time; time.sleep(5)"]
    out = system.run_process(argv, timeout=0.5)
    assert "timed out" in out


def test_shell_argv_variants():
    assert system.shell_argv("ls", "bash") == ["bash", "-c", "ls"]
    assert system.shell_argv("dir", "powershell")[0] == "powershell"
    with pytest.raises(ValueError):
        system.shell_argv("x", "fish")


def test_html_to_text_strips_scripts():
    html = "<html><head><title>T</title><script>evil()</script></head><body><p>Hi</p><p>there</p></body></html>"
    text = system.html_to_text(html)
    assert text.startswith("# T") and "Hi" in text and "evil" not in text


def test_format_response_json_and_html():
    r = httpx.Response(200, json={"a": 1}, request=httpx.Request("GET", "http://x"))
    assert '"a": 1' in system.format_response(r)
    r = httpx.Response(200, text="<p>Hello</p>", headers={"content-type": "text/html"},
                       request=httpx.Request("GET", "http://x"))
    assert "Hello" in system.format_response(r) and "<p>" not in system.format_response(r)


def test_http_request_post_needs_operate(ops_config):
    ops_config(mode="read")
    with pytest.raises(policy.PolicyError):
        system.do_http_request("http://127.0.0.1:9/", method="POST")
    with pytest.raises(ValueError):
        system.do_http_request("file:///etc/passwd")


def test_ssh_argv_resolves_alias_jump(ops_config):
    ops_config({"ssh": {"slim": {"target": "root@10.0.0.6"},
                        "thic": {"target": "root@10.0.0.5", "jump": "slim", "port": 2222}}})
    argv = remote.ssh_argv(remote.resolve_host("thic"), "uptime")
    assert argv[0] == "ssh" and "BatchMode=yes" in argv
    assert argv[argv.index("-J") + 1] == "root@10.0.0.6"
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[-3:] == ["root@10.0.0.5", "--", "uptime"]


def test_resolve_host_rejects_unknown_and_injection(ops_config):
    ops_config()
    assert remote.resolve_host("me@host.lan").target == "me@host.lan"
    with pytest.raises(ConfigError):
        remote.resolve_host("unknown")
    with pytest.raises(ConfigError):
        remote.resolve_host("-oProxyCommand=evil@x")


def test_ssh_run_screens_before_connecting(ops_config, monkeypatch):
    ops_config({"ssh": {"slim": "root@10.0.0.6"}}, mode="read")
    calls = []
    monkeypatch.setattr(remote, "run_process", lambda argv, timeout: calls.append(argv) or "ok")
    assert remote.run_on_host("slim", "df -h") == "ok"
    with pytest.raises(policy.PolicyError):
        remote.run_on_host("slim", "systemctl restart x")
    assert len(calls) == 1


def test_ssh_transfer_builds_scp(ops_config, monkeypatch, tmp_path):
    ops_config({"ssh": {"slim": "root@10.0.0.6"}}, mode="operate")
    seen = {}
    monkeypatch.setattr(remote, "run_process", lambda argv, timeout: seen.setdefault("argv", argv) and "done")
    remote.do_ssh_transfer("slim", "download", "/var/log/syslog", str(tmp_path / "syslog"))
    assert seen["argv"][0] == "scp"
    assert seen["argv"][-2] == "root@10.0.0.6:/var/log/syslog"
