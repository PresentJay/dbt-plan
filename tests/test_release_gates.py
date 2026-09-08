"""The CI gates must exercise optional integrations and built artifacts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_integrations_and_packaging_without_allowing_skips():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert ".[test,dbt,mcp]" in ci
    assert "dbt-core==1.11.7" in ci
    assert "dbt-duckdb==1.10.1" in ci
    assert "required-tests.py tests/test_dbt_e2e.py tests/test_mcp_server.py" in ci
    assert "required-tests.py tests/test_packaging.py" in ci
    assert ci.index("python -m build") < ci.index("required-tests.py tests/test_packaging.py")


def test_release_checks_artifacts_before_publication():
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert (
        release.index("python -m build")
        < release.index("required-tests.py tests/test_packaging.py")
        < release.index("uses: pypa/")
    )


def test_docs_protect_jinja_from_liquid():
    import re

    for page in (ROOT / "docs").rglob("*.md"):
        text = re.sub(
            r"{% raw %}.*?{% endraw %}", "", page.read_text(encoding="utf-8"), flags=re.S
        )
        assert "{%" not in text, page


def test_integration_job_runs_new_mcp_verdict_regressions():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    integrations = ci.split("  integrations:", 1)[1].split("  packaging:", 1)[0]
    assert "tests/test_review_safety_gaps.py" in integrations
