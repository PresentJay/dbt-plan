"""SQLGlot-based column extraction from compiled SQL."""

from collections.abc import Callable, Iterable

import sqlglot
from sqlglot import exp

# Resolution walks CTE references; the cap is a backstop against pathological
# nesting, not a real limit -- the `seen` set already stops cycles.
_MAX_CTE_DEPTH = 32


def _is_star(expr: exp.Expression) -> bool:
    """A bare `*` or a qualified `alias.*`."""
    return isinstance(expr, exp.Star) or (isinstance(expr, exp.Column) and expr.name == "*")


def _star_is_modified(expr: exp.Expression) -> bool:
    """True when the star carries EXCEPT / EXCLUDE / REPLACE / RENAME.

    Every one of these changes what the star expands to, so resolving the star
    while ignoring the modifier yields the *unmodified* list. The same thing
    happens on both sides of the diff, so adding an `EXCEPT(secret)` compares
    equal and reports safe while dbt drops the column.

    Whitelisted rather than enumerated: any argument on the Star node counts. A
    modifier this code has not heard of must not default to being ignored, and
    for a qualified `a.*` the modifier hangs off the inner Star rather than the
    Column wrapper -- which is precisely how the first version of this missed it.
    """
    star = expr.this if isinstance(expr, exp.Column) else expr
    if not isinstance(star, exp.Star):
        return False
    return any(value for value in star.args.values())


def _sole_source(select: exp.Select) -> exp.Table | None:
    """The only FROM source, or None when there is not exactly one.

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
        return None  # subquery, table function, UNNEST -- not a resolvable name
    return source


def _output_select(tree: exp.Expression) -> exp.Select | None:
    """Read set-operation names from the left branch, including parentheses."""
    while isinstance(tree, (exp.Subquery, exp.SetOperation)):
        tree = tree.this
    return tree if isinstance(tree, exp.Select) else None


def _relation_key(table: exp.Table) -> str:
    """`"j"."main"."stg_orders"` -> `j.main.stg_orders`, to match manifest relations."""
    parts = [
        part.name
        for part in (table.args.get("catalog"), table.args.get("db"), table.this)
        if part is not None
    ]
    return ".".join(parts).lower()


def _cte_bodies(tree: exp.Expression) -> dict[str, exp.Expression]:
    """Top-level CTEs only. Nested ones are out of scope and must not shadow."""
    with_ = tree.args.get("with_") or tree.args.get("with")
    if with_ is None:
        return {}
    return {cte.alias_or_name: cte.this for cte in with_.expressions}


def _projection_cast(expr: exp.Expression, dialect: str) -> str | None:
    """Rendered target type of an explicit CAST on this projection, else None.

    Only an explicit cast is readable from compiled SQL. A column with no cast has
    whatever type the warehouse gave it, which is exactly the thing dbt-plan does
    not ask.
    """
    node = expr.this if isinstance(expr, exp.Alias) else expr
    if isinstance(node, exp.Cast):
        return node.to.sql(dialect=dialect)
    return None


def _resolve_star_columns(
    select: exp.Select,
    ctes: dict[str, exp.Expression],
    seen: frozenset[str],
    dialect: str,
    table_columns: Callable[[str], list[str] | None] | None = None,
) -> list[tuple[str, str | None]] | None:
    """Columns of `select`, expanding any star that points at a CTE.

    Each entry is (column name, explicit cast type or None), so column extraction
    and cast comparison share one walker rather than two that drift apart.

    Returns None to mean "refuse" -- the caller then falls back to ["*"]. A column
    list that is merely plausible is worse than admitting ignorance: it gets
    compared against another plausible list and yields a silent "safe".
    """
    if len(seen) > _MAX_CTE_DEPTH:
        return None

    # PIVOT/UNPIVOT transform a source schema. Never resolve its input star as
    # the transformed output, including qualified stars referencing a CTE.
    if any(table.args.get("pivots") for table in select.find_all(exp.Table)):
        return None

    columns: list[tuple[str, str | None]] = []
    for expr in select.expressions:
        if not _is_star(expr):
            name = expr.alias or expr.output_name
            if not name:
                return None
            columns.append((name.lower(), _projection_cast(expr, dialect)))
            continue

        if _star_is_modified(expr):
            return None  # EXCEPT / EXCLUDE / REPLACE / RENAME -- not a plain star

        # Resolve the named source in this SELECT scope before consulting CTEs:
        # a qualified physical relation must not be shadowed by a CTE name.
        if isinstance(expr, exp.Column) and expr.table:
            frm = select.args.get("from_") or select.args.get("from")
            sources = ([frm.this] if frm else []) + [
                join.this for join in select.args.get("joins") or []
            ]
            matches = [
                t for t in sources if isinstance(t, exp.Table) and t.alias_or_name == expr.table
            ]
            if len(matches) != 1:
                return None
            table = matches[0]
        else:
            table = _sole_source(select)
            if table is None:
                return None
        # An alias identifies this source in the SELECT, not a different CTE.
        # Both star forms must resolve CTE identity from the relation name.
        source = table.name
        if table.alias_column_names:
            # FROM source AS alias(new_names) changes the output schema.
            return None

        if source in seen:
            return None

        body = ctes.get(source) if table is None or not (table.db or table.catalog) else None
        if body is not None:
            if isinstance(body.parent, exp.CTE) and body.parent.alias_column_names:
                # WITH source(new_names) AS (...) also renames the body output.
                return None
            if not isinstance(body, exp.Select):
                # A set operation or recursive CTE, whose column list is not a
                # straight read of one SELECT's projections.
                return None
            if body.args.get("with_") or body.args.get("with"):
                # Its own CTE scope, which would resolve against the wrong names.
                return None
            inner = _resolve_star_columns(body, ctes, seen | {source}, dialect, table_columns)
            if not inner:
                return None
            columns.extend(inner)
            continue

        # Not a CTE: it is a physical relation, which for a dbt project is
        # another model whose compiled SQL the caller already has on disk.
        if table is None or table_columns is None or isinstance(expr, exp.Column):
            return None
        found = table_columns(_relation_key(table)) or table_columns(source.lower())
        if not found:
            return None
        # Casts are not carried across a model boundary; that model is checked
        # on its own, where its casts are visible.
        columns.extend((name.lower(), None) for name in found)

    return columns or None


def extract_columns(
    sql: str,
    *,
    dialect: str = "snowflake",
    table_columns: Callable[[str], list[str] | None] | None = None,
) -> list[str] | None:
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

    select = _output_select(tree)
    if select is None:
        return None

    # The canonical dbt staging model ends in `select * from renamed`, where
    # `renamed` is a CTE listing its columns explicitly. Every name is in the
    # file. Try to read them before giving up; _resolve_star_columns refuses
    # rather than guessing, so a refusal leaves the behaviour below untouched.
    ctes = _cte_bodies(tree)
    if (ctes or table_columns) and any(_is_star(e) for e in select.expressions):
        resolved = _resolve_star_columns(select, ctes, frozenset(), dialect, table_columns)
        if resolved:
            return [name for name, _ in resolved]

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


def extract_cast_types(
    sql: str,
    *,
    dialect: str = "snowflake",
    table_columns: Callable[[str], list[str] | None] | None = None,
) -> dict[str, str] | None:
    """Map column name -> declared type, for columns carrying an explicit CAST.

    Deciding whether a column's type changed generally needs the warehouse's
    current type, which is why dbt-plan does not attempt it. But when both
    revisions carry an explicit CAST on the same column, the comparison is
    compiled SQL against compiled SQL -- the only thing this tool ever does.

    Returns:
        dict: column name -> rendered type. Empty when nothing is cast.
        None: parse failure, or a star this cannot resolve -- same refusal
            contract as extract_columns, so callers never compare a guess.
    """
    sql = sql.lstrip("\ufeff")

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError, RecursionError):
        return None

    select = _output_select(tree)
    if select is None:
        return None

    resolved = _resolve_star_columns(
        select, _cte_bodies(tree), frozenset(), dialect, table_columns
    )
    if resolved is None:
        return None
    return {name: cast for name, cast in resolved if cast}


def columns_read_by_relation(
    sql: str,
    schema: dict[str, dict[str, str]],
    *,
    dialect: str = "snowflake",
) -> dict[str, list[str]] | None:
    """Every relation this SQL names columns from, and which columns -- in one parse.

    Cascade detection was textual -- look for the dropped column's name anywhere in
    the downstream SQL -- which fires on a comment, a string literal, and any column
    of the same name belonging to a different table:

        SELECT c.customer_id
        FROM stg_orders o JOIN dim_customers c ON o.order_id = c.order_id

    Nothing there reads `stg_orders.customer_id`, and the text search says it does.
    Resolving the reference instead needs a schema, and dbt-plan has one: every
    model's columns, worked out from the project's own compiled SQL. `schema` is
    keyed by bare relation name, which sqlglot matches against the fully qualified
    relation that compiled dbt SQL actually contains.

    One parse per downstream model, answering for every relation it touches. The
    cascade asks about each model that is losing a column -- in a `SELECT *` chain
    that is every model upstream -- and re-qualifying the same SQL once per question
    put a 200-model project at 38 seconds against a 5 second budget.

    Named explicitly, and stars deliberately left out. This exists to decide whether
    a query will *fail*, and `select *` never fails when a column disappears -- it
    returns one column fewer. That is a different finding, and predict_ddl already
    makes it for the model that inherits the loss.

    Keys are the relation names as written in the SQL, lowercased. Returns None when
    the answer would be a guess: the SQL will not parse, or a column cannot be
    attributed to a relation. The caller falls back to searching the text, which is
    wider -- a refusal here must never narrow what gets reported.
    """
    from sqlglot.errors import OptimizeError, SqlglotError
    from sqlglot.optimizer.qualify import qualify

    sql = sql.lstrip("\ufeff")
    try:
        tree = qualify(
            sqlglot.parse_one(sql, dialect=dialect),
            schema=schema,
            dialect=dialect,
            expand_stars=False,
        )
    except (SqlglotError, OptimizeError, ValueError, KeyError, RecursionError):
        return None

    alias_to_relation = {
        table.alias_or_name: table.name.lower() for table in tree.find_all(exp.Table)
    }
    read: dict[str, set[str]] = {name: set() for name in alias_to_relation.values()}
    for column in tree.find_all(exp.Column):
        relation = alias_to_relation.get(column.table)
        if relation is not None and not isinstance(column.this, exp.Star):
            read[relation].add(column.name.lower())
    return {relation: sorted(columns) for relation, columns in read.items()}


def columns_read_from(
    sql: str,
    relation: str | Iterable[str],
    schema: dict[str, dict[str, str]],
    *,
    dialect: str = "snowflake",
) -> list[str] | None:
    """Which columns this SQL names explicitly from `relation`.

    `relation` is every name the model is known by -- its own, and the relation an
    `alias:` config makes it write to. Compiled SQL only ever contains the latter,
    so matching on the model name alone answers "does not read it" for a model that
    reads nothing else. See columns_read_by_relation for the rest.
    """
    by_relation = columns_read_by_relation(sql, schema, dialect=dialect)
    if by_relation is None:
        return None
    names = {relation.lower()} if isinstance(relation, str) else {r.lower() for r in relation}
    return sorted({col for name in names for col in by_relation.get(name, [])})


# Coarse on purpose. sqlglot parses `varchar` and `text` to different types, and on
# Snowflake or duckdb they are the same one -- comparing more finely than this means
# a per-adapter table, and a wrong answer about a type is worse than no answer.
# Measured against dbt 1.11.7: a contract declaring `varchar` accepts a `TEXT` cast
# and rejects an `INTEGER` one. These families are the line no adapter disagrees with.
_TYPE_FAMILIES = (
    ("text", "TEXT_TYPES"),
    ("number", "NUMERIC_TYPES"),
    ("date/time", "TEMPORAL_TYPES"),
)


def type_family(declared: str, *, dialect: str = "snowflake") -> str | None:
    """The broad family a declared SQL type belongs to, or None if it has no obvious one.

    `varchar`, `text` and `string` are all "text". `int`, `bigint` and `numeric` are
    all "number", so a widening within a family is deliberately not reported -- see
    the module comment above for why that line is where it is.
    """
    try:
        parsed = sqlglot.parse_one(declared, into=exp.DataType, read=dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError, RecursionError):
        return None

    kind = parsed.this
    if kind == exp.DataType.Type.BOOLEAN:
        return "boolean"
    for name, attribute in _TYPE_FAMILIES:
        if kind in getattr(exp.DataType, attribute):
            return name
    return None
