"""SQLGlot-based column extraction from compiled SQL."""

import sqlglot
from sqlglot import exp

# Resolution walks CTE references; the cap is a backstop against pathological
# nesting, not a real limit -- the `seen` set already stops cycles.
_MAX_CTE_DEPTH = 32


def _is_star(expr: exp.Expression) -> bool:
    """A bare `*` or a qualified `alias.*`."""
    return isinstance(expr, exp.Star) or (isinstance(expr, exp.Column) and expr.name == "*")


def _sole_source_name(select: exp.Select) -> str | None:
    """Name of the only FROM source, or None when there is not exactly one.

    An unqualified `*` over a join means every joined source's columns. Resolving
    just the FROM would silently drop the rest, so removing the join would compare
    equal and report safe. Refusing is the whole point.
    """
    if select.args.get("joins"):
        return None
    frm = select.args.get("from_") or select.args.get("from")
    if frm is None:
        return None
    source = frm.this
    if not isinstance(source, exp.Table):
        return None  # subquery, table function, UNNEST -- not a CTE reference
    return source.alias_or_name


def _cte_bodies(tree: exp.Expression) -> dict[str, exp.Expression]:
    """Top-level CTEs only. Nested ones are out of scope and must not shadow."""
    with_ = tree.args.get("with_") or tree.args.get("with")
    if with_ is None:
        return {}
    return {cte.alias_or_name: cte.this for cte in with_.expressions}


def _resolve_star_columns(
    select: exp.Select,
    ctes: dict[str, exp.Expression],
    seen: frozenset[str],
) -> list[str] | None:
    """Columns of `select`, expanding any star that points at a CTE.

    Returns None to mean "refuse" -- the caller then falls back to ["*"]. A column
    list that is merely plausible is worse than admitting ignorance: it gets
    compared against another plausible list and yields a silent "safe".
    """
    if len(seen) > _MAX_CTE_DEPTH:
        return None

    columns: list[str] = []
    for expr in select.expressions:
        if not _is_star(expr):
            name = expr.alias or expr.output_name
            if not name:
                return None
            columns.append(name.lower())
            continue

        if expr.args.get("except_"):
            return None  # the EXCEPT marker is the caller's business

        source = expr.table if isinstance(expr, exp.Column) and expr.table else None
        if source is None:
            source = _sole_source_name(select)
        if source is None or source in seen:
            return None

        body = ctes.get(source)
        if not isinstance(body, exp.Select):
            # Missing, or a set operation / recursive CTE, whose column list is not
            # a straight read of one SELECT's projections.
            return None
        if body.args.get("with_") or body.args.get("with"):
            # Its own CTE scope, which would resolve against the wrong names.
            return None

        inner = _resolve_star_columns(body, ctes, seen | {source})
        if not inner:
            return None
        columns.extend(inner)

    return columns or None


def extract_columns(sql: str, *, dialect: str = "snowflake") -> list[str] | None:
    """Extract column names from compiled SQL's final SELECT.

    Parses with the given SQL dialect. Returns lowercased column names
    using alias if available, otherwise output_name.

    Args:
        sql: Compiled SQL string.
        dialect: sqlglot dialect name (default: "snowflake").

    Returns:
        list[str]: Column names (lowercased).
        ["*"]: If final SELECT uses SELECT *.
        None: If parsing fails or no SELECT found.
    """
    # Strip BOM (U+FEFF) that some editors/tools prepend to UTF-8 files
    sql = sql.lstrip("\ufeff")

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError, RecursionError):
        # ParseError: malformed SQL; TokenError: untokenizable input (unclosed quotes, binary);
        # ValueError: unknown dialect; RecursionError: deeply nested SQL exceeds stack
        return None

    select = tree.find(exp.Select)
    if select is None:
        return None

    # The canonical dbt staging model ends in `select * from renamed`, where
    # `renamed` is a CTE listing its columns explicitly. Every name is in the
    # file. Try to read them before giving up; _resolve_star_columns refuses
    # rather than guessing, so a refusal leaves the behaviour below untouched.
    ctes = _cte_bodies(tree)
    if ctes and any(_is_star(e) for e in select.expressions):
        resolved = _resolve_star_columns(select, ctes, frozenset())
        if resolved:
            return resolved

    columns = []
    expr_count = 0
    for expr in select.expressions:
        expr_count += 1
        if isinstance(expr, exp.Star):
            # BigQuery SELECT * EXCEPT(col1, col2) — extract excluded columns
            except_cols = expr.args.get("except_")
            if except_cols:
                excluded = sorted(
                    col.output_name.lower() for col in except_cols if col.output_name
                )
                if excluded:
                    return [f"* except({', '.join(excluded)})"]
            return ["*"]
        name = expr.alias or expr.output_name
        # Qualified star (e.g. t1.*) produces a Column with name='*'
        if name == "*":
            return ["*"]
        if name:
            columns.append(name.lower())

    # If some expressions had no extractable name (e.g. CASE without AS),
    # we have ambiguity — return None so the caller treats as REVIEW REQUIRED
    if columns and len(columns) < expr_count:
        return None

    return columns if columns else None
