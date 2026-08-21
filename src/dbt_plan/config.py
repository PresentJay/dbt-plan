"""Configuration loading from .dbt-plan.yml and environment variables.

Precedence (highest wins): CLI flags > env vars > config file > defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# pyyaml is not a dependency — use a simple parser for the minimal config format
# This avoids adding runtime dependencies per project rules


@dataclass
class Config:
    """Resolved dbt-plan configuration."""

    ignore_models: list[str] = field(default_factory=list)
    warning_exit_code: int = 2
    format: str = "text"
    no_color: bool = False
    verbose: bool = False
    dialect: str = "snowflake"
    include_packages: bool = False  # if True, also check models from dbt packages
    compile_command: str = (
        "dbt compile"  # command to compile dbt project (e.g., "uv run dbt compile")
    )

    @classmethod
    def load(cls, project_dir: str | Path = ".") -> Config:
        """Load config from .dbt-plan.yml in project_dir, then overlay env vars."""
        config = cls()
        config._load_file(Path(project_dir))
        config._load_env()
        return config

    def _load_file(self, project_dir: Path) -> None:
        """Load .dbt-plan.yml if it exists. Simple key: value parser."""
        config_path = project_dir / ".dbt-plan.yml"
        if not config_path.exists():
            return

        try:
            text = config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # OSError: permission denied, I/O error, etc.
            # UnicodeDecodeError: non-UTF-8 file content (not a subclass of OSError)
            return

        # Strip BOM that some editors prepend to UTF-8 files
        text = text.lstrip("\ufeff")

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()

            if key == "ignore_models":
                # Parse bracket list: [model1, model2] or comma-separated
                value = value.strip("[]")
                self.ignore_models = [
                    m.strip().strip("'\"") for m in value.split(",") if m.strip()
                ]
            elif key == "warning_exit_code":
                try:
                    val = int(value)
                    if 0 <= val <= 255:
                        self.warning_exit_code = val
                except ValueError:
                    pass
            elif key == "format":
                if value in ("text", "github", "json"):
                    self.format = value
            elif key == "no_color":
                self.no_color = value.lower() in ("true", "1", "yes")
            elif key == "dialect":
                # Only allow alphanumeric dialect names (sqlglot dialect identifiers)
                if value.isalnum():
                    self.dialect = value
            elif key == "include_packages":
                self.include_packages = value.lower() in ("true", "1", "yes")
            elif key == "compile_command":
                if value:
                    self.compile_command = value

    def _load_env(self) -> None:
        """Override config with environment variables."""
        if fmt := os.environ.get("DBT_PLAN_FORMAT"):
            if fmt in ("text", "github", "json"):
                self.format = fmt
        if os.environ.get("DBT_PLAN_NO_COLOR", "").lower() in ("true", "1", "yes"):
            self.no_color = True
        if os.environ.get("DBT_PLAN_VERBOSE", "").lower() in ("true", "1", "yes"):
            self.verbose = True
        if dialect := os.environ.get("DBT_PLAN_DIALECT"):
            if dialect.isalnum():
                self.dialect = dialect
        if os.environ.get("DBT_PLAN_INCLUDE_PACKAGES", "").lower() in ("true", "1", "yes"):
            self.include_packages = True
        if ignore := os.environ.get("DBT_PLAN_IGNORE_MODELS"):
            self.ignore_models = [m.strip() for m in ignore.split(",") if m.strip()]
        if wec := os.environ.get("DBT_PLAN_WARNING_EXIT_CODE"):
            try:
                val = int(wec)
                if 0 <= val <= 255:
                    self.warning_exit_code = val
            except ValueError:
                pass
        if cc := os.environ.get("DBT_PLAN_COMPILE_COMMAND"):
            if cc:
                self.compile_command = cc
