"""Run uv installs offline into the environment declared by ci-setup.

Only uv bootstrap is bypassed: the test runner provides uv. Dependency resolution,
locking, wheel installation, console scripts, and PATH propagation are real.
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.test_generated_ci_execution import execute

UV = shutil.which("uv")
pytestmark = pytest.mark.skipif(
    UV is None or sys.platform == "win32",
    reason="uv and a POSIX host are required for the Ubuntu installer",
)


def wheel(directory, name, command, module):
    distribution = name.replace("-", "_")
    metadata = f"{distribution}-1.0.0.dist-info"
    contents = {
        f"{module}.py": f'import json, sys\ndef main():\n    print(json.dumps({{"command": "{command}", "prefix": sys.prefix}}))\n',
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n",
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\nGenerator: dbt-plan-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{metadata}/entry_points.txt": f"[console_scripts]\n{command} = {module}:main\n",
    }
    contents[f"{metadata}/RECORD"] = "".join(
        f"{path},,\n" for path in [*contents, f"{metadata}/RECORD"]
    )
    with zipfile.ZipFile(directory / f"{distribution}-1.0.0-py3-none-any.whl", "w") as archive:
        for path, content in contents.items():
            archive.writestr(path, content)


@pytest.mark.parametrize(
    "layout",
    ["requirements", "pyproject", "locked", "both", "stale-lock", "missing", "no-adapter"],
)
def test_installer_shares_one_environment_and_honors_lock(tmp_path, layout):
    project = tmp_path / "project with spaces"
    runner = tmp_path / "runner with spaces"
    packages = tmp_path / "wheels"
    for path in (project, runner, packages):
        path.mkdir()
    wheel(packages, "dbt-fixture", "dbt", "ci_fixture_dbt")
    wheel(packages, "dbt-plan", "dbt-plan", "ci_fixture_plan")
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + str(Path(UV).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
        "RUNNER_TEMP": str(runner),
        "GITHUB_PATH": str(tmp_path / "github-path"),
        "UV_NO_INDEX": "1",
        "UV_FIND_LINKS": str(packages),
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_PYTHON": sys.executable,
        "UV_PYTHON_DOWNLOADS": "never",
    }
    # Parent development invocations must not change what this installer tests.
    for key in ("UV_NO_SYNC", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"):
        env.pop(key, None)
    if layout not in ("requirements", "missing"):
        dependency = '"dbt-fixture==1.0.0"' if layout != "no-adapter" else ""
        (project / "pyproject.toml").write_text(
            f'[project]\nname="workflow-fixture"\nversion="1.0.0"\ndependencies=[{dependency}]\n'
        )
    if layout in ("locked", "both", "stale-lock"):
        locked = subprocess.run(
            [UV, "lock", "--offline"],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert locked.returncode == 0, locked.stderr
    if layout in ("requirements", "both"):
        (project / "requirements.txt").write_text(
            "dbt-fixture==1.0.0\n" if layout == "requirements" else "must-not-install-this==1\n"
        )
    if layout == "stale-lock":
        path = project / "pyproject.toml"
        path.write_text(
            path.read_text()
            .replace("dependencies=[", 'description="changed after locking"\ndependencies=[')
            .replace('"dbt-fixture==1.0.0"', '"dbt-fixture>=1.0.0"')
        )
    lock = project / "uv.lock"
    before = lock.read_bytes() if lock.exists() else None
    bootstrap = 'pip() { return 0; }; python() { if [ "$*" = "-m pip install uv" ]; then return 0; else command python "$@"; fi; };\n'
    result = execute("Install", project, env, bootstrap)
    if layout in ("stale-lock", "missing", "no-adapter"):
        assert result.returncode != 0, result.stdout + result.stderr
        assert not Path(env["GITHUB_PATH"]).exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        bindir = Path(env["GITHUB_PATH"]).read_text().strip()
        assert bindir == str(runner / "dbt-plan-venv/bin")
        assert not (project / ".venv").exists()
        for command in ("dbt", "dbt-plan"):
            proc = subprocess.run(
                [str(Path(bindir) / command)],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert proc.returncode == 0, proc.stderr
            assert json.loads(proc.stdout) == {
                "command": command,
                "prefix": str(runner / "dbt-plan-venv"),
            }
    if before is not None:
        assert lock.read_bytes() == before
    else:
        assert not lock.exists(), "an untracked generated lock can block the baseline checkout"
