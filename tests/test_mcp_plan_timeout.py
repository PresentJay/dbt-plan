"""A stalled plan must fail explicitly, without trusting partial reports."""

import json
import os
import subprocess
import sys

import pytest

server = pytest.importorskip("dbt_plan_mcp.server", reason="needs the optional mcp extra")
ENV = "DBT_PLAN_MCP_PLAN_TIMEOUT_SECONDS"


@pytest.fixture(autouse=True)
def isolated_timeout(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def report():
    return json.dumps(
        {
            "summary": {"total": 0, "safe": 0, "warning": 0, "destructive": 0},
            "models": [],
            "parse_failures": [],
            "skipped_models": [],
            "uncompiled_models": [],
            "stale_sources": [],
        }
    )


@pytest.mark.parametrize(
    "setting,expected", [(None, 120), ("1", 1), ("3600", 3600), ("42", 42), ("00001", 1)]
)
def test_timeout_bounds_and_utf8(monkeypatch, setting, expected):
    if setting is not None:
        monkeypatch.setenv(ENV, setting)

    def run(args, **kwargs):
        assert args[:4] == [sys.executable, "-m", "dbt_plan.cli", "check"]
        assert kwargs["timeout"] == expected
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["text"] is True
        assert kwargs["capture_output"] is True
        assert not kwargs.get("shell", False)
        return subprocess.CompletedProcess(args, 0, report(), "")

    monkeypatch.setattr(server.subprocess, "run", run)
    assert server.plan("/unused")["verdict"] == "safe"


@pytest.mark.parametrize(
    "setting",
    [
        "",
        "0",
        "-1",
        "3601",
        "1.5",
        "abc",
        "1e2",
        "1_000",
        " 1",
        "1 ",
        "+1",
        "１",
        "00000",
        pytest.param("9" * 5000, id="oversized-integer"),
    ],
)
def test_invalid_setting_does_not_launch(monkeypatch, setting, capsys):
    monkeypatch.setenv(ENV, setting)

    def run(*args, **kwargs):
        pytest.fail("invalid configuration launched a child")

    monkeypatch.setattr(server.subprocess, "run", run)
    out = server.plan("/unused")
    assert out["verdict"] == "error"
    assert out["reason"] == "configuration_error"
    assert ENV in out["error"]
    assert "1" in out["error"] and "3600" in out["error"]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("partial", [None, b"", b'{"summary":', report().encode(), report()])
def test_timeout_discards_all_output(monkeypatch, partial, capsys):
    monkeypatch.setenv(ENV, "7")

    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 7, output=partial, stderr=b"private detail")

    monkeypatch.setattr(server.subprocess, "run", run)
    out = server.plan("/unused")
    assert out["verdict"] == "error"
    assert out["reason"] == "timeout"
    assert out["timeout_seconds"] == 7
    assert "timed out" in out["error"]
    assert "summary" not in out and "exit_code" not in out
    assert "private detail" not in json.dumps(out)
    assert capsys.readouterr().out == ""


def test_setting_is_read_for_each_call(monkeypatch):
    limits = []

    def run(args, **kwargs):
        limits.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(args, 0, report(), "")

    monkeypatch.setattr(server.subprocess, "run", run)
    for value in ("2", "3", None):
        if value is None:
            monkeypatch.delenv(ENV)
        else:
            monkeypatch.setenv(ENV, value)
        assert server.plan("/unused")["verdict"] == "safe"
    assert limits == [2, 3, 120]


@pytest.mark.parametrize("setting", ["invalid", "1"])
def test_snapshot_is_not_limited(monkeypatch, setting):
    monkeypatch.setenv(ENV, setting)

    def run(args, **kwargs):
        assert args[3] == "snapshot"
        assert kwargs.get("timeout") is None
        return subprocess.CompletedProcess(args, 0, "baseline saved", "")

    monkeypatch.setattr(server.subprocess, "run", run)
    assert server.snapshot("/unused") == {"ok": True, "detail": "baseline saved"}


@pytest.mark.parametrize(
    "code,expected", [(0, "safe"), (1, "destructive"), (2, "review_required"), (3, "error")]
)
def test_completed_verdicts(monkeypatch, code, expected):
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, code, report(), ""),
    )
    assert server.plan("/unused")["verdict"] == expected


def test_real_child_is_killed_and_reaped(monkeypatch, tmp_path, capsys):
    # Block on a pipe owned by the test, not a long sleep. The context manager is
    # only fallback cleanup: assert reaping before it could wait for the child.
    program = tmp_path / "blocked.py"
    program.write_text("import sys\nsys.stdin.buffer.read()\n", encoding="utf-8")
    real_popen = subprocess.Popen
    real_run = subprocess.run
    children = []
    with real_popen(
        [sys.executable, str(program)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:

        def popen(args, **kwargs):
            assert args[3] == "check"
            children.append(child)
            return child

        monkeypatch.setattr(server.subprocess, "Popen", popen)

        def bounded_run(args, **kwargs):
            # Fail promptly if production code stops forwarding the timeout.
            assert kwargs.get("timeout") == 0.05
            return real_run(args, **kwargs)

        monkeypatch.setattr(server.subprocess, "run", bounded_run)
        # Force the real communicate() to wait on the open stdin pipe. run()
        # normally closes it, so detach it while retaining our write handle.
        writer = child.stdin
        child.stdin = None
        monkeypatch.setattr(server, "_plan_timeout_seconds", lambda: 0.05)
        try:
            assert child.poll() is None
            out = server.plan("/unused")
            assert children == [child]
            assert out["verdict"] == "error" and out["reason"] == "timeout"
            assert out["timeout_seconds"] == 0.05
            assert child.returncode is not None
            if os.name == "posix":
                with pytest.raises(ChildProcessError):
                    os.waitpid(child.pid, os.WNOHANG)
        finally:
            writer.close()
            if child.poll() is None:
                child.kill()
                child.wait()
    assert capsys.readouterr().out == ""
