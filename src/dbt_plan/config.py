"""Configuration loading from .dbt-plan.yml and environment variables.

Precedence (highest wins): CLI flags > env vars > config file > defaults.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# pyyaml is not a dependency — use a simple parser for the minimal config format
# This avoids adding runtime dependencies per project rules


DEFAULT_DIALECT = "snowflake"

# dbt adapter names are mostly sqlglot dialect names already, so only the ones
# that genuinely differ are listed. Anything sqlglot does not know falls back to
# the default rather than raising -- vertica and firebolt have no dialect, and a
# project using them should still get a report.
_ADAPTER_ALIASES = {
    "sqlserver": "tsql",
    "synapse": "tsql",
    "glue": "spark",
    "spark_session": "spark",
}


def sqlglot_dialect_for_adapter(adapter_type: str | None) -> str | None:
    """The sqlglot dialect matching a dbt adapter, or None if there is not one."""
    name = (adapter_type or "").strip().lower()
    if not name:
        return None
    name = _ADAPTER_ALIASES.get(name, name)

    from sqlglot.dialects import Dialects

    return name if name in {d.value for d in Dialects} else None


@dataclass
class Config:
    """Resolved dbt-plan configuration."""

    ignore_models: list[str] = field(default_factory=list)
    # Models whose destructive change has been reviewed and accepted. Unlike
    # ignore_models these are still reported in full; they just stop driving
    # the exit code. Named models only -- there is deliberately no "all".
    acknowledge_models: list[str] = field(default_factory=list)
    warning_exit_code: int = 2
    format: str = "text"
    no_color: bool = False
    verbose: bool = False
    dialect: str = DEFAULT_DIALECT
    # Whether a human chose that dialect. "snowflake" is both the fallback and a
    # real answer, so without this the manifest could never override the default.
    dialect_explicit: bool = False
    compile_command: str = (
        "dbt compile"  # command to compile dbt project (e.g., "uv run dbt compile")
    )

    def resolve_dialect(self, adapter_type: str | None) -> str:
        """The dialect to parse with, once the project itself has had its say.

        Precedence matches everything else here -- CLI, env, file, project,
        default -- and `self.dialect` already carries the env and file layers.
        The manifest's `adapter_type` only speaks when no human did, which is why
        a BigQuery project used to be parsed as Snowflake with no flags passed.
        """
        if self.dialect_explicit:
            return self.dialect
        return sqlglot_dialect_for_adapter(adapter_type) or self.dialect

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

        lines = text.splitlines()
        list_keys = {"ignore_models", "acknowledge_models"}
        known_keys = list_keys | {
            "warning_exit_code",
            "format",
            "no_color",
            "verbose",
            "dialect",
            "include_packages",
            "compile_command",
        }

        line_number = 0
        while line_number < len(lines):
            raw_line = lines[line_number]
            line_number += 1
            stripped_line = self._strip_inline_comment(raw_line).strip()
            if not stripped_line:
                continue

            if ":" not in stripped_line:
                self._warn_config(project_dir, line_number, "cannot understand setting")
                continue

            key, _, value = stripped_line.partition(":")
            key = key.strip()
            value = value.strip()

            if not key:
                self._warn_config(project_dir, line_number, "cannot understand setting")
                continue

            if key not in known_keys:
                self._warn_config(project_dir, line_number, f"cannot understand {key}")
                continue

            if key in list_keys and not value:
                values = []
                key_indent = len(raw_line) - len(raw_line.lstrip())
                while line_number < len(lines):
                    list_line = lines[line_number]
                    list_content = self._strip_inline_comment(list_line).strip()
                    if not list_content:
                        line_number += 1
                        continue
                    list_indent = len(list_line) - len(list_line.lstrip())
                    if list_indent <= key_indent:
                        break
                    line_number += 1
                    if not list_content.startswith("-"):
                        self._warn_config(
                            project_dir,
                            line_number,
                            f"cannot understand {key} list item",
                        )
                        continue
                    item = list_content[1:].strip()
                    if item:
                        values.append(self._unquote_scalar(item))
                setattr(self, key, values)
                continue

            value = self._unquote_scalar(self._strip_inline_comment(value).strip())

            if key == "ignore_models":
                # Parse bracket list: [model1, model2] or comma-separated
                parsed = self._parse_inline_list(value)
                if parsed is None:
                    self._warn_config(project_dir, line_number, f"cannot understand {key}")
                else:
                    self.ignore_models = parsed
            elif key == "acknowledge_models":
                parsed = self._parse_inline_list(value)
                if parsed is None:
                    self._warn_config(project_dir, line_number, f"cannot understand {key}")
                else:
                    self.acknowledge_models = parsed
            elif key == "warning_exit_code":
                try:
                    val = int(value)
                    if 0 <= val <= 255:
                        self.warning_exit_code = val
                    else:
                        self._warn_config(project_dir, line_number, f"cannot understand {key}")
                except ValueError:
                    self._warn_config(project_dir, line_number, f"cannot understand {key}")
            elif key == "format":
                if value in ("text", "github", "json"):
                    self.format = value
                else:
                    self._warn_config(project_dir, line_number, f"cannot understand {key}")
            elif key == "no_color":
                if value.lower() in ("true", "1", "yes"):
                    self.no_color = True
                elif value.lower() in ("false", "0", "no"):
                    self.no_color = False
                else:
                    self._warn_config(project_dir, line_number, f"cannot understand {key}")
            elif key == "dialect":
                # Only allow alphanumeric dialect names (sqlglot dialect identifiers)
                self.dialect_explicit = True
                if value.isalnum():
                    self.dialect = self._unquote_scalar(value)
                else:
                    self._warn_config(project_dir, line_number, f"cannot understand {key}")
            elif key == "include_packages":
                # Still recognised so it can be told apart from a typo, but it no
                # longer does anything. It only ever widened the manifest index,
                # while the compiled scan covers the root project alone -- so the
                # models it added were never examined, and the uncompiled-model
                # check then reported them as a broken compile. See the removal
                # note in CHANGELOG 0.11.0.
                self._warn_config(
                    project_dir,
                    line_number,
                    "include_packages is ignored; it never checked package models "
                    "and reported a healthy compile as incomplete",
                )
            elif key == "compile_command":
                if value:
                    self.compile_command = value
                else:
                    self._warn_config(project_dir, line_number, f"cannot understand {key}")

    @staticmethod
    def _strip_inline_comment(value: str) -> str:
        quote = None
        for index, char in enumerate(value):
            if char in ("'", '"'):
                if quote == char:
                    quote = None
                elif quote is None:
                    quote = char
            elif char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
                return value[:index]
        return value

    @staticmethod
    def _unquote_scalar(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            return value[1:-1]
        return value

    @classmethod
    def _parse_inline_list(cls, value: str) -> list[str] | None:
        if value.startswith("[") != value.endswith("]"):
            return None
        if value.startswith("["):
            value = value[1:-1].strip()
        return [cls._unquote_scalar(item.strip()) for item in value.split(",") if item.strip()]

    @staticmethod
    def _warn_config(project_dir: Path, line_number: int, message: str) -> None:
        print(
            f"{project_dir / '.dbt-plan.yml'}:{line_number}: warning: {message}",
            file=sys.stderr,
        )

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
                self.dialect_explicit = True
        if ignore := os.environ.get("DBT_PLAN_IGNORE_MODELS"):
            self.ignore_models = [m.strip() for m in ignore.split(",") if m.strip()]
        if ack := os.environ.get("DBT_PLAN_ACKNOWLEDGE"):
            self.acknowledge_models = [m.strip() for m in ack.split(",") if m.strip()]
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
