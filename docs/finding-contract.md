# Canonical finding contract, version 1

`dbt_plan.findings` is a pure producer adapter. It reads Python objects already
loaded by the caller; it performs no filesystem, database or network operations.
The CLI supplies these facts to JSON, text, GitHub and MCP output. The additive
`causal_graph` field carries the relevant graph described in
[causal-path-contract.md](causal-path-contract.md); a source-to-affected pair is
not a claim of an immediate edge. See [explained-findings.md](explained-findings.md)
for rendering limits, reproducible reports and transport validation.

## Entry points

```python
from dbt_plan.findings import Evidence, InputRefusal, findings_from_result

facts = findings_from_result(
    result,                 # formatter.CheckResult, before policy filtering
    manifest,               # complete current manifest dictionary
    base_manifest=baseline, # optional, includes removed resources
    evidence={
        ("model.shop.orders.v2", "model.shop.order_report", "cascade.broken_ref"):
            Evidence("resolved_read", "exact", "read_checked",
                     columns=("book_id",), compiled_path="target/compiled/shop/report.sql"),
    },
)
payload = [fact.to_dict() for fact in facts]
```

`findings_from_result(result, manifest=None, *, node_index=None,
base_manifest=None, evidence=None) -> tuple[Finding, ...]` adapts all predictions
and these refusal fields: `parse_failures`, `skipped_models`, `uncompiled_models`,
`stale_sources`, `baseline_problem`. It imports `CheckResult` only under
`TYPE_CHECKING`; formatter may import this adapter without a runtime cycle.
Acknowledgements and exit policy are deliberately never consulted.

Supply either `manifest` or `node_index`. The latter accepts the existing
`Mapping[str, ModelNode]`, retaining each actual `node_id`, `name` and ID-derived
version alias. It cannot recover collisions discarded by `build_node_index`,
test resources or original source paths. Use complete current/base manifests for
path graphs, package collisions and source links. Omitted identity inputs leave
names unresolved, with warning or destructive severity, never guessed IDs.

`findings_from_predictions(predictions, manifest, *, base_manifest=None,
refusals=(), evidence=None, ambiguous_resources=()) -> tuple[Finding, ...]` is the lower-level entry point.
Pass lists/tuples of `DDLPrediction` and `InputRefusal(reason_code, message,
resource=None)`. Explicit refusals allow producers outside `CheckResult` to
retain unreadable inputs. A refusal with no resource is report-wide; stale source
paths remain in the message, not in a fabricated model identity.
The result adapter also retains `CheckResult.ambiguous_resources`: known
ambiguity prevents an ID selection even if a lossy node index kept one candidate.

These functions are typed producer APIs, not legacy report deserializers.
Malformed input raises `TypeError` or `ValueError`; callers must surface that as
an error/review, never catch it and substitute an empty successful report.
An empty tuple means no supplied facts, not proof that compilation succeeded.

## Facts and identities

Each immutable `Finding` has:

| Field | Meaning |
| --- | --- |
| `contract_version` | Integer `1`; independent of dbt-plan package version. |
| `rule_code` | Stable machine code; independent of message wording. |
| `source`, `affected` | `Resource` or null for report-wide refusals. Own DDL uses the same resource for both. Cascade source is the changed prediction owner, not a claimed immediate SQL parent. |
| `column`, `columns` | Optional operation column and sorted union of that column with explicitly supplied evidence columns. No extraction from prose. |
| `columns_added`, `columns_removed` | Sorted unique prediction-level changes, retained even for rules with no per-column operation. |
| `severity` | Raw policy-independent `safe`, `warning`, or `destructive`, elevated to warning if evidence/identity is uncertain. Never downgraded. |
| `raw_risk` | Original own safety string, or original cascade risk (including unrecognized values). |
| `message` | Original operation/reason text. Presentation may change without changing rule meaning. |
| `evidence` | Origin, state, reason code, sorted columns and optional separate compiled path. |
| `uncertainty` | Sorted reasons requiring review. |
| `waiver_allowed` | Conservative eligibility, not an applied waiver. Refusals and uncertain facts are never eligible. |

`Resource` has `name` (original producer spelling), `unique_id`, sorted
`candidates`, and optional `original_file_path`. IDs come from manifest keys,
including package, resource kind and version, such as `model.shop.orders.v2`
or `test.shop.not_null_orders_book_id.hash`. A conflicting node `unique_id` is
invalid. Never construct an ID from a short name or select the first match.

Lookup accepts an exact ID, a node name, a compiled `path` stem (`defined_in`),
or the existing `model_key` version spelling. A warehouse relation `alias` is
not a resource name. Multiple candidates leave `unique_id=null` and add
`ambiguous_resource`; no candidates add `unresolved_resource`. Current and base
aliases are combined. Current node paths take precedence; base-only resources
retain their base paths. Disabled nodes are not newly indexed.

Original paths are copied only from `original_file_path`, never `path` or
`compiled_path`. Absolute, Windows-drive, traversal, target and dbt_packages
paths are withheld. A path is exposed only when that resource's manifest declares
`metadata.project_name` matching the ID's package. Missing or empty metadata never
establishes root identity, even with a single package. A missing path does not
invalidate a known resource ID. This
is syntactic provenance checking, not an assertion that the file exists.
There are **no source line numbers**. Compiled offsets do not locate lines in
Jinja source. Compiled paths, when supplied, live only in `evidence.compiled_path`.

## Evidence and stable codes

Evidence `state` has exactly three meanings:

| State | Meaning | Typical origin / reason |
| --- | --- | --- |
| `exact` | The stated fact follows a column-independent resource/configuration rule, or has explicitly supplied producer provenance. | `prediction` / `ddl_rule`, explicit `resolved_columns` / `ddl_rule`, or `resolved_read` / `read_checked` |
| `conservative` | A fallback may over-report the relationship, or the prediction lacks evidence of how columns were obtained. | `prediction` / `provenance_unavailable`, or explicit `text_search` / `read_fallback` |
| `unknown` | The input or provenance is unresolved. | `legacy_cascade` / `provenance_unavailable`, `input` / refusal code |

Existing `DDLPrediction` does not retain whether its columns came from SQL or a
manifest fallback. Column-dependent DDL operations therefore default to conservative
`provenance_unavailable`, cannot be waived, and promote a raw safe verdict to
warning. Raw destructive risk stays destructive. A caller that has proven the
input origin can supply explicit exact evidence for the DDL rule; an operation
name alone does not establish it. Unknown/review operations remain unknown.
Column-independent rules retain exact evidence without column provenance:
`ddl.replace_table`, `ddl.replace_view`, `ddl.model_removed`,
`ddl.materialization_changed`, `ddl.schema_policy_changed`, `ddl.relation_changed`,
and `ddl.verdict` for ephemeral materialization. In particular, table/view
replacement remains safe when the rule is safe, even with unknown columns.
An explicit unknown/conservative evidence entry still requires review for these
rules; input refusals remain separate facts and are never erased.

### Handoff to #254's CLI evidence producer

The CLI's raw predictions and exit policy are unchanged by this module. The
transport adapter must retain the real producer flags (`used_manifest_columns`,
`parse_failed`, `partial_unknown`) and input availability before converting to
canonical facts. Confirmed parsed/resolved SQL can supply exact evidence; the
absence of flags in a legacy object is not that confirmation. Manifest fallback
should supply conservative evidence, and failed/partial resolution should supply
unknown evidence. Never label an entire report exact because one model parsed.

Evidence keys are `(source.unique_id, affected.unique_id, rule_code)`; own DDL
has the same ID in both positions. The complete rule enumeration is below.
For example, a proven column diff may supply
`("model.shop.orders.v2", "model.shop.orders.v2", "ddl.drop_column")` with
`Evidence("resolved_columns", "exact", "column_diff_checked", columns=("book_id",))`.
Use each emitted operation's code, including `ddl.verdict` for no operations;
a single prediction may emit multiple rule codes. A consumer can obtain the
codes from a conservative first conversion, then attach producer evidence and
convert again, without parsing message strings or importing private helpers.
Do not attach exact evidence to unresolved or ambiguous identities. Exact
evidence for an own DDL rule says nothing about its separate cascade facts.

An exact DDL rule is not proof of an exact SQL read. Existing `DownstreamImpact`
does not carry machine-readable read provenance. Its default is therefore
unknown, even when its prose says “reads”. Never infer columns or certainty
from prose. Producers that actually have read evidence can supply it keyed by
`(source unique_id, affected unique_id, rule_code)`. This evidence applies to
all matching facts; supply the conservative union if multiple reads share that
key. This adapter does not claim a full causal path. A later path producer must
retain actual edge identities separately.

Explicit evidence cannot erase an input refusal, unknown rule, unreadable test
or unresolved prediction. Destructive findings remain destructive under
uncertainty. An uncertain safe input becomes warning; `raw_risk` still records
the supplied value. `columns` never implies more certainty than `evidence.state`.

Version 1 rule codes:

- `ddl.model_removed`, `ddl.replace_table`, `ddl.replace_view`, `ddl.no_ddl`,
  `ddl.add_column`, `ddl.drop_column`, `ddl.build_failure`, `ddl.stale_columns`,
  `ddl.ignored_removal`, `ddl.ignored_addition`.
- `ddl.contract_violation`, `ddl.materialization_changed`,
  `ddl.schema_policy_changed`, `ddl.relation_changed`.
- `ddl.verdict` for predictions with no operations; `ddl.review_required`,
  `ddl.unknown_configuration`, `ddl.unknown_operation` for unresolved rules.
- `cascade.broken_ref`, `cascade.build_failure`, `cascade.unit_test_failure`,
  `cascade.unit_test_unreadable`, `cascade.inherited_drop`,
  `cascade.inherited_change`, `cascade.data_test_failure`,
  `cascade.data_test_unreadable`, `cascade.contract_violation`,
  `cascade.unknown_risk`.
- `input.refusal`.

Built-in reason codes are `ddl_rule`, `unresolved_prediction`,
`provenance_unavailable`, `ambiguous_resource`, `unresolved_resource`,
`unknown_rule`, `unresolved_input`, `parse_failed`, `missing_manifest_resource`,
`missing_compiled_sql`, `stale_source`, `baseline_missing`, `baseline_corrupt`,
and `baseline_unknown`. Explicit evidence/refusal producers may add reason codes;
consumers must preserve unfamiliar codes and require review rather than infer
safety. Existing codes must not be reassigned to different meanings.

## Transport migration and consumer examples

During 0.x migration, the CLI adds a `findings` array to existing report objects,
including an empty array when it produced no facts. Legacy fields and valid
legacy fixtures remain supported. Absence of `findings` means an older report,
not zero findings. Direct formatter callers default to `CheckResult.findings=None`
and remain legacy unless they supply canonical facts. Consumers
must preserve unknown additive fields; a consumer that does not understand a
rule, severity, risk or contract version must require review or return an error.
No production JSON Schema dependency is needed by this producer.

The CLI records column provenance before applying exit policy. Resolved SQL uses
`compiled_sql / exact / column_diff_checked`; manifest fallback uses
`manifest / conservative / manifest_fallback`. Partial projection resolution uses
`partial_unknown`, and failed extraction uses `parse_failed` or `unresolved_input`
with unknown evidence. These are reason codes, not new finding fields. Known
column names can survive partial resolution, but the evidence remains unknown.
Column-independent rules keep their rule evidence, so missing column metadata
alone does not elevate a raw safe table/view replacement. Separate input refusals
and downstream uncertainty still survive. When both SQL column sets are proven,
the CLI preserves their added/removed delta in the canonical transport copy even
for table/view rules that return before the predictor records columns. The rule
remains raw safe; losing a view projection is still a recorded fact. Fallback or
partial column sets do not establish that complete delta. Current legacy cascade
producers lack read provenance, so those facts remain unknown; the CLI never
extracts certainty from a human-readable cascade message.

Text and GitHub output append a canonical section with raw severity, rule code,
qualified identities, evidence, column changes, uncertainty and separate compiled
paths. Existing model rows retain acknowledgement policy. JSON and MCP carry the
same canonical array without rewriting it. MCP also forwards existing report
fields, including analysis, ignore lists and unknown extensions; missing legacy
fields are not synthesized. MCP's review verdict is spelled `review_required`.

When `causal_graph` is present, the human section uses bounded causal explanations;
JSON and MCP preserve the entire accompanying `findings` array and relevant
graph. Graph association indices are local to that exact array. Legacy absence
of the graph is valid and does not mean zero edges. Malformed graph structures
or invalid references fail validation; unfamiliar graph semantics require review.
Candidate dependencies, resolved-empty checks, unknown external coverage and
unproved loss attribution alone do not elevate known safe resource rules.

The CLI integrates actual unresolved reader observations not already represented
by a risk/refusal as separate `input.refusal` facts (`read_unresolved`, warning,
unwaivable). This preserves own DDL facts and proven column deltas. MCP forwards
these in `refusals`; acknowledgements cannot waive them. Default warnings exit 2,
while explicit fail-on policy and legacy warning exit settings still apply to
the policy code without erasing the raw warning. No parser/inference behavior or
canonical rule codes change.

CLI compiled locations are relative to the resolved project root and use `/`
separators. Resolving both paths keeps aliases such as macOS `/var` and
`/private/var` consistent. Files outside that root (including an explicitly
external baseline), or paths that cannot be resolved, have `compiled_path=null`.
No location is fabricated for them. Original source paths still come only from
the trusted manifest field; compiled locations never supply source lines.

`dbt_plan_mcp.report_validation` is a standard-library-only transport validator
shipped in the base wheel; importing it does not require the optional MCP package.
It validates nested facts and impacts before interpretation. Unknown rule, risk,
version, origin, state or reason codes are preserved and require review. The
minimum severity of known destructive/warning rules also survives contradictory
`safe` fields. Malformed qualified identities are rejected; unresolved identities
remain reviewable. The schema checks structure and permits additive fields;
schema validity alone does not mean safe. Action and generated workflow consumers validate canonical reports
with this module, and continue to accept older pinned CLI reports without a
canonical array. Their raw verdict includes nested risks, while the existing
gate uses the policy exit code: acknowledgements, `warning_exit_code=0` and
the gate's `fail-on` input remain authoritative.

For example, a round trip keeps an acknowledged destructive fact intact:

```python
result.findings = findings_from_result(result, manifest, evidence=producer_evidence)
report = json.loads(format_json(result))
validate_report(report)  # returns this report, preserving additive fields
assert report["findings"] == [fact.to_dict() for fact in result.findings]
# MCP forwards this exact array even if the CLI policy exit code is zero.
```

An acknowledged drop still yields `rule_code="ddl.drop_column"`,
`severity="destructive"`, `raw_risk="destructive"`, `column="book_id"`.
Record policy decisions separately; never remove or recolor that raw fact safe.
If parsing also failed, retain the additional `input.refusal` with
`evidence.reason_code="parse_failed"` and `waiver_allowed=false`.

Two changed sources affecting the same reader produce two cascade facts with
different source IDs. Do not deduplicate only by affected ID/rule. Ambiguous
`orders` across `model.a.orders` and `model.b.orders` keeps both candidate IDs
and no selected ID. A qualified `model.a.orders` resolves only that resource.
Versioned nodes behave the same way; do not silently pick the latest version.

Returned facts and all set-like column/candidate fields have deterministic
ordering. Exact duplicate input facts remain duplicates; consumers must not
interpret array position as a durable finding ID. `to_dict()` returns a fresh
JSON-compatible object (tuples become arrays), suitable for additive transport
fields. No consumer may convert a malformed report or missing evidence into
a proven safe edge.
