# Reading causal explanations

The CLI renders the same canonical findings and relevant causal graph in text,
GitHub Markdown, JSON and MCP. Start with the source change, the affected resource,
and the rule/configuration. Follow the original model file link to inspect the
model. A compiled SQL location is explicitly separate: it is **not a Jinja/source
line**. Original paths come from the trusted root-project manifest field; missing
or untrusted paths do not receive invented links. GitHub links encode UTF-8 paths
and punctuation; paths over 240 characters are shown as shortened text without a
link.

An association between a finding's source and affected resource is not a direct
read edge. A displayed route is one deterministic representative route through
retained evidence, not every possible path:

- `exact direct compiled read` establishes only that immediate read. It does not
  establish the original root's responsibility for an intermediate output loss.
- `candidate dependency; column flow unknown` comes from the manifest. An
  ephemeral/star step can be relevant without proving exact column flow.
- `fallback candidate read` is a conservative text reference, not an exact read.
- `resolved read check; not a column-flow edge` records a callback result without
  a removed-column match. In particular, an empty read never proves a route.
- `Route unknown/interrupted` means there is no supported route to display.
  No artificial direct edge fills the gap.

Observed output losses retain unknown root attribution. That uncertainty does
not promote every own DDL rule to warning. Exposures are declared consumers;
external consumer coverage remains unknown, even with zero impacts. That coverage
disclosure alone does not change severity or exit policy.

## Unresolved reads and policy

An actual unresolved read not already represented by a risk/refusal receives a
separate canonical `input.refusal`, with warning severity, `read_unresolved`
evidence and `waiver_allowed=false`. The original view/table DDL fact stays intact,
including its proven column delta. Acknowledging the source or reader cannot waive
the refusal. No SQL parsing or inference rules are changed by this integration.

Default policy exits 2 for this warning; an active destructive risk still exits 1.
`--fail-on never`, `--fail-on destructive`, and legacy `warning_exit_code: 0` may
allow exit 0 while the raw refusal remains. MCP returns `review_required` and a
nonempty `refusals` list for this case even when policy returns 0. Existing known
risk acknowledgements retain their policy behavior. Candidate dependencies,
unproved root attribution and resolved empty reads alone do not create refusals.

## Bounds and transport

The explanation section shows at most 10 associations, worst raw severity first,
then canonical array order. Each has at most 8 representative route edges, 8
columns per column list and 240 characters per label/message. Up to 8 additional
read/check observations are shown. Long labels disclose omitted characters;
long lists disclose omitted columns; long routes disclose omitted route steps.
The footer counts omitted associations and graph edges not displayed, using the
union of displayed edge identities. These counts are **not counts of paths**.
Existing model/downstream display limits from #102 are unchanged.

Breadth-first traversal uses a visited set, including on cyclic or shared-parent
graphs. No alternative simple paths are enumerated or counted. Rendering visits
the graph at most once per displayed association (at most 10); sorting is
deterministic. JSON has no rendering cap and retains all canonical findings and
all relevant nodes, edges, losses, read summaries and associations. It is not a
full callback/query audit: the producer summarizes resolved-empty nonrelationships.
See [the producer contract](causal-path-contract.md).

The additive JSON/MCP field is **`causal_graph`**. Its `finding_index` and
`cause_finding_indices` refer to the exact accompanying `findings` array, not
stable IDs. Neither is reordered or filtered for human rendering. Absence means
a legacy report, not an empty graph. The standard-library-only consumer validator
in the base package checks graph structure and local references. Unknown graph
semantics require review; malformed structures/references are errors. Unknown
additive object fields survive unchanged. Raw findings remain authoritative;
the graph cannot turn a destructive or unknown finding into safe. JSON Schema
checks structure; consumer validation also checks cross-array references.

## Three reproducible reports

From the repository root, using the installed checkout:

```bash
uv run --no-sync python examples/explained-findings/generate.py --output-dir .context/explanation-reports
uv run --no-sync pytest tests/test_explained_path_rendering.py tests/test_audit24_docs.py -q
```

The generator creates temporary prepared compiled SQL and manifests, runs the
real `snapshot` and `check` commands, and writes unmodified CLI stdout. It needs
no dbt adapter, credentials or warehouse. Its source is the complete fixture;
the fixed `file-only-example` revision and timestamp describe that synthetic
fixture, not a claimed Git commit or real compilation. Regenerate committed
reports by setting `--output-dir examples/explained-findings`.

| Report | Before and after | Result |
| --- | --- | --- |
| [Direct](../examples/explained-findings/direct.txt) ([GitHub](../examples/explained-findings/direct.md), [JSON](../examples/explained-findings/direct.json)) | `root` removes `amount`; unchanged `reader` explicitly selects it | Exact direct read; broken reference; exit 1 |
| [Multi-hop](../examples/explained-findings/multihop.txt) ([GitHub](../examples/explained-findings/multihop.md), [JSON](../examples/explained-findings/multihop.json)) | `root` removes `amount`; ephemeral `mid` passes `*`; incremental `reader` uses `sync_all_columns`; `report` reads `amount` | Candidate intermediate steps, inherited drop, exact final read; exit 1 |
| [Unknown](../examples/explained-findings/unknown.txt) ([GitHub](../examples/explained-findings/unknown.md), [JSON](../examples/explained-findings/unknown.json)) | `root` removes `amount`; unchanged `reader` contains `select * from root where` | Own view replacement SAFE plus separate unwaivable warning; exit 2 |

The tests execute these commands and compare every published report byte for
byte. These are reproducible interpretation examples, not a usability study;
reviewer observation belongs to #250.
