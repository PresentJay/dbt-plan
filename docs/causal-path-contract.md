# Causal evidence graph (producer interface for #256)

This local producer adds no CLI, formatter, report schema, selection, exit policy
or global display-cap behavior. `dbt_plan.explained_paths` consumes existing
analysis; it does not parse SQL, contact a warehouse, or traverse external Mesh
projects. It imports canonical finding types and their identity resolver.
`predictor` never imports this module or `findings`, avoiding an import cycle.

## Composition

```python
from dbt_plan.explained_paths import explain_findings, explain_predictions

# Preferred at the transport boundary: preserve #254's enriched own deltas,
# explicit producer certainty, refusals, duplicates and exact canonical order.
explained = explain_findings(
    canonical_findings, predictions_with_provenance, current_manifest,
    base_manifest=base_manifest,
    ambiguous_resources=ambiguous_resources,
)
payload = explained.to_dict()  # {"findings": [...], "graph": {...}}

# For callers that have raw predictions and producer evidence instead:
explained = explain_predictions(
    predictions_with_provenance, current_manifest,
    base_manifest=base_manifest, evidence=producer_evidence, refusals=refusals,
    ambiguous_resources=ambiguous_resources,
)
```

Both return immutable `ExplainedAnalysis(findings, graph)`. `to_dict()` returns
fresh JSON-compatible objects. Supply canonical facts and predictions from the
**same analysis before filtering or acknowledgements**, using the same complete
manifests and ambiguity set. Composition rejects resource IDs, candidate sets
or trusted paths that disagree with the supplied manifests. The convenience function first calls the existing
`findings_from_predictions` adapter. It does not upgrade cascade findings from
an edge: exact direct reads do not prove the original root caused an intermediate
loss. Existing canonical fields, rule codes and severity remain unchanged.

`explain_findings` is especially useful for table/view changes: their original
predictions may have no column delta, whereas #254's canonical transport copy
has the observed removed/added columns. Neither entry point reconstructs that
delta from all observed evidence columns or from message text. Errors are errors,
never empty successful reports.

## Retained facts

`analyze_cascade_impacts` keeps its existing return signature and verdict logic.
Its predictions gain optional `provenance: CascadeProvenance | None`, excluded
from dataclass equality and repr to preserve existing value comparisons.
`None` means unavailable, not no risk. The pure records live in `predictor`:

- `ReadProvenance(source, reader, columns_read, columns_removed, text_matches)`
  captures each existing `columns_read_of(reader, source)` call. It retains the
  actual lookup spellings, complete returned column tuple, the source's removed
  columns under analysis, and separately any fallback text matches. Empty reads
  are resolved observations; `None` is unresolved. No additional SQL callback
  calls are made, including for ephemeral models skipped by the existing reader.
- `CascadeProvenance(reads, losses)` records these calls and the existing
  `lost_by_model` map, including ephemeral/intermediate output losses. Losses
  observed under a root do not prove that root caused them.

The graph has the following collections. There is no array of every path.

| Field | Meaning |
| --- | --- |
| `nodes` | Relevant resource identities, manifest materialization, explicit `unrendered_config.on_schema_change`, union of observed removed columns and optional loss evidence. Missing config remains null. |
| `edges` | All distinct representable read/check and relevant manifest dependency facts. Each has source, target, kind, evidence, removed-column intersection, optional observation root and revision labels. |
| `losses` | Output losses with their observation root, resource, exact stored columns and unknown causal attribution. This retains root context when node unions overlap. |
| `read_summaries` | Per-root counts of resolved-empty checks with no direct manifest dependency. These non-relationships are excluded from graph edges; complete callback records remain in prediction provenance. |
| `associations` | One per canonical finding, including duplicates and report-wide refusals. `finding_index` addresses this report's findings tuple; `cause_finding_indices` locates the source's own DDL/configuration facts. These indices are local references, not stable IDs. |
| `has_cycles` | Whether the retained candidate dependency graph contains a cycle. It does not claim SQL execution is cyclic. |
| `external_coverage` | Always `unknown`, even with zero findings or declared exposures. |

An association carries source, affected, original finding evidence and source
added/removed columns from the source's own canonical findings. Read
`finding.rule_code` for the configuration rule, `finding.severity/raw_risk` for
unchanged risk, and the source/target nodes for manifest configuration. Missing
source deltas stay empty; an empty tuple is not proof of no change. In particular,
`Finding.columns` includes observed evidence columns and is **not** the cause.

Edge kinds deliberately separate different claims:

- `read`: a removed column occurs in the resolved read or fallback match.
  `resolved_read / exact / read_checked` proves this direct compiled-SQL read
  only. `text_search / conservative / read_fallback` is a candidate reference,
  not a precise column-flow edge. `root` identifies the prediction under which
  the call occurred; it can differ from `source` (the immediate queried input).
- `read_check`: no removed-column match was established. A resolved empty read
  is exact evidence of that callback result, **not an exact path**. An unresolved
  callback without text matches has `input / unknown / read_unresolved` and
  does not prove safety. All nonempty returned read columns remain available.
  Resolved-empty checks remain individual edges for direct manifest dependencies;
  otherwise they contribute only to `read_summaries`. This relevance rule never
  discards positive, fallback, unknown or ambiguous observations and has no cap.
  The JSON graph is a complete relevant causal-fact graph, **not a lossless full
  query audit**: the summary does not retain each excluded pair's identity. The
  original prediction provenance retains those pairs for in-process inspection.
- `dependency`: manifest dependency only, with
  `manifest / unknown / dependency_not_column_flow`. `revisions` records `base`,
  `current`, or both. It can connect supported intermediate resources, but does
  not establish which column passes through, or prove the root attribution.
- `declared_consumer`: an attached exposure's relevant dependency, with unknown
  column flow. A declared exposure neither proves breakage nor covers every
  external consumer. Its resource ID remains in the graph even if no dependency
  was supplied; missing links are never fabricated.

Node/loss evidence has `output_loss_attribution_unproved`: the analyzer observed
the output loss, but did not prove upstream attribution or input-column origin.
Never upgrade this by matching column names across tables. Graph-local reason
codes are not changes to the public finding contract or report validator.

## Identity and source trust

The graph reuses the canonical `_Resources` implementation against **complete**
current and base manifests. It never trusts a short-name index winner. Lookup
spelling is resolved before normalizing a known endpoint to manifest name and
qualified ID. Unknown/ambiguous endpoints retain their name and candidate IDs;
their read evidence becomes unknown. Exact manifest dependency IDs can coexist
with an ambiguous read endpoint; they do not resolve that observation's ambiguity.

The existing SQL reader uses bare relation names. If any model/source name,
alias or identifier is shared by different resources in either full manifest,
read attribution to that resource is `unknown / ambiguous_relation`, even when
the short lookup is unique. This can conservatively flag differently qualified
relations; no SQL inference is added to disambiguate them. Actual callback
columns are preserved.

Canonical path trust remains authoritative: only trusted original_file_path
values from the resource's root project are exposed. Compiled paths stay in
canonical evidence. No graph field claims a source/Jinja line number. Current
resource metadata wins; base-only resources retain baseline identity/path data.

## Finite traversal and complexity

The producer stores the calls the existing cascade already makes: for each
changed root, at most `D * L` read observations, where `D` is checked downstream
models and `L` is models with observed losses. This is polynomial, not a claim
that cascade analysis is linear. Additional storage is proportional to those
observations and their returned column payloads. Existing callback costs and
SQL parsing behavior are unchanged.

Graph construction indexes the supplied manifests once. Seeds are finding
sources; targets are findings, observed losses/reads, and attached exposures.
It intersects forward reachability from seeds with reverse reachability from
targets and retains all edges between those relevant vertices. Dependency
revisions are unioned explicitly; a mixed-revision route is a candidate route,
not a claim that one revision executed it. Unrelated manifest branches are not
exported. Read/check observations (except the explicitly summarized empty
non-relationships) and loss records remain explicit even if no manifest route
connects them. References to a missing manifest source remain unresolved
dependency endpoints. No synthetic root-to-target edge fills a gap.

For `V` manifest vertices, `E` manifest dependency edges, `R` retained observation
and loss records, and `F` findings, traversal/cycle detection is `O(V + E)` with
visited sets and a queue, and graph storage is `O(V + E + R + F)` plus column
payload. Deterministic serialization sorts records, adding
`O((V + E + R + F) log(V + E + R + F))` comparisons; serialized key/payload size
also contributes. Association references additionally take `O(A)` serialized
space/time, where `A` is the number of cause-finding references (at most `F²`);
source deltas and cause index tuples are computed once per root in memory.
Duplicate graph records merge; distinct roots, revisions,
targets and evidence do not. Canonical duplicate findings never merge.

All producer observations are inspected once, including excluded empty checks;
construction therefore also costs `O(Q)` for `Q` original read queries, even when
the retained graph is small. Resource resolution is cached per lookup spelling.
The actual-producer 200-model star chain makes 39,601 queries; its graph retains
199 direct empty checks and 199 dependencies, plus all 200 losses and findings.
The other 39,402 empty non-relationship checks are counted, not exported as
39,402 artificial relationships. The test bounds serialized size below 1 MB;
this is a regression assertion, not a runtime truncation threshold. Truly dense
positive/unknown evidence can still require quadratic storage; no risks are cut
to meet a size target.

The independent stress test uses 40 two-way layers: 82 nodes, 160 dependencies,
and over a trillion possible root-to-target routes. Output retains 160 edges,
not those routes. Cycle handling does not recurse and never removes a known
risk. A later human renderer may choose a bounded representative route, but
must disclose omitted detail and retain this full relevant graph in raw JSON.
No #102 global display limit changes here.

## Reproducible interpretation controls

Run `pytest tests/test_explained_paths.py tests/test_precise_cascade.py
tests/test_audit24_cascade.py -q -rs` in the documented test environment.

1. Direct: `root` removes `book_id`; reader explicitly selects it. The source
   DDL finding carries the removed column, and the root-to-reader `read` records
   resolved reads including any unchanged columns. The root association retains
   its canonical certainty independently.
2. Multi-hop: `root -> mid (ephemeral SELECT *) -> reader SELECT book_id`.
   `mid -> reader` is a resolved read. `root -> mid` is a manifest candidate plus
   an observed intermediate loss, not a proven exact star-column mapping.
   Both resources and the uncertainty survive. In actual dbt compiled SQL an
   ephemeral CTE can be inlined; observed reads may name the underlying physical
   input rather than the logical intermediate. Preserve what the callback says.
3. Unknown: the callback refuses and the compiled text mentions `book_id`.
   Preserve the conservative candidate reference; do not render a precise path
   or downgrade its finding. Without a text match the unresolved check remains.

These are reproducible producer controls, not published renderer examples or a
usability study. #256 owns presentation/transport integration; the parent owns
real-dbt integration acceptance, exact-head review and remote CI.
