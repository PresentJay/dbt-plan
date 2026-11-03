"""Exercise the demo's exit handling through its real Bash entry point."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "examples" / "sample-project" / "run-example.sh"
EXPECTED_EXPLANATION = "이 예제에서는"


def _run_example(tmp_path, code, fail_at=1):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required to execute the sample runner")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    count = tmp_path / "count"
    stub = bin_dir / "dbt-plan"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$CALL_LOG"\n'
        "n=0\n"
        'if [ -f "$COUNT_FILE" ]; then read -r n < "$COUNT_FILE"; fi\n'
        "n=$((n + 1))\n"
        'printf "%s\\n" "$n" > "$COUNT_FILE"\n'
        'if [ "$n" -ge "$FAIL_AT" ]; then exit "$STUB_CODE"; fi\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    result = subprocess.run(
        [bash, str(SCRIPT)],
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "CALL_LOG": calls.as_posix(),
            "COUNT_FILE": count.as_posix(),
            "STUB_CODE": str(code),
            "FAIL_AT": str(fail_at),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return result, calls.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("code", [0, 1, 2])
def test_completed_verdicts_show_every_format(tmp_path, code):
    result, calls = _run_example(tmp_path, code)

    assert result.returncode == 0, result.stderr
    assert len(calls) == 4
    for call, output_format in zip(calls, ("text", "github", "json"), strict=False):
        assert f"--format {output_format}" in call
    assert f"exit code: {code}" in result.stdout
    assert EXPECTED_EXPLANATION in result.stdout


@pytest.mark.parametrize("code", [3, 127])
def test_execution_error_stops_the_demo(tmp_path, code):
    result, calls = _run_example(tmp_path, code)

    assert result.returncode == 3
    assert len(calls) == 1
    assert "execution failed" in result.stderr.lower()
    assert str(code) in result.stderr
    assert EXPECTED_EXPLANATION not in result.stdout


@pytest.mark.parametrize("fail_at", [2, 3, 4])
def test_error_in_a_later_invocation_stops_the_demo(tmp_path, fail_at):
    result, calls = _run_example(tmp_path, 3, fail_at=fail_at)

    assert result.returncode == 3
    assert len(calls) == fail_at
    assert "execution failed" in result.stderr.lower()
    assert EXPECTED_EXPLANATION not in result.stdout
