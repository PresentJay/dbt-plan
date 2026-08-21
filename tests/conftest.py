from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_dir():
    return FIXTURES_DIR


@pytest.fixture
def explicit_columns_sql():
    return (FIXTURES_DIR / "explicit_columns.sql").read_text()


@pytest.fixture
def select_star_sql():
    return (FIXTURES_DIR / "select_star.sql").read_text()


@pytest.fixture
def variant_access_sql():
    return (FIXTURES_DIR / "variant_access.sql").read_text()
