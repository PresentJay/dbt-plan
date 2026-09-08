"""Run a CI suite where any skip is a broken validation environment."""

import sys

import pytest


class RequireExecution:
    def __init__(self):
        self.skipped = False

    def pytest_collectreport(self, report):
        self.skipped |= report.skipped

    def pytest_runtest_logreport(self, report):
        self.skipped |= report.skipped


def main():
    gate = RequireExecution()
    code = pytest.main(sys.argv[1:], plugins=[gate])
    if gate.skipped:
        print(
            "Required suite skipped tests; install its dependencies and build artifacts.",
            file=sys.stderr,
        )
        return 1
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
