"""dbt-plan: Static analysis tool that warns about risky DDL changes before dbt run."""

__version__ = "0.3.5"

# Re-export key symbols for library usage
from dbt_plan.columns import extract_columns
from dbt_plan.config import Config
from dbt_plan.diff import ModelDiff, diff_compiled_dirs
from dbt_plan.formatter import CheckResult, format_github, format_json, format_text
from dbt_plan.manifest import ModelNode, build_node_index, load_manifest
from dbt_plan.predictor import (
    DDLOperation,
    DDLPrediction,
    DownstreamImpact,
    Safety,
    predict_ddl,
)

__all__ = [
    "__version__",
    "CheckResult",
    "Config",
    "DDLOperation",
    "DDLPrediction",
    "DownstreamImpact",
    "ModelDiff",
    "ModelNode",
    "Safety",
    "build_node_index",
    "diff_compiled_dirs",
    "extract_columns",
    "format_github",
    "format_json",
    "format_text",
    "load_manifest",
    "predict_ddl",
]
