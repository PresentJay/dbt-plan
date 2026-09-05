#!/usr/bin/env bash
#
# Repository settings that live in the GitHub API rather than in this tree.
#
# Cloning does not bring them along, and recreating the repository loses them.
# This script puts them back. It is idempotent -- run it after any change to the
# repository's protection or Actions configuration, and to verify drift.
#
#   ./scripts/apply-repo-settings.sh            # apply
#   ./scripts/apply-repo-settings.sh --check    # report differences, change nothing
#
# Requires the gh CLI, authenticated with admin rights on the repository.

set -euo pipefail

REPO="${REPO:-PresentJay/dbt-plan}"
BRANCH="main"
CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true

# Every job in ci.yml. A name here that does not exist as a job blocks all
# merges permanently, so keep this list in step with .github/workflows/ci.yml.
REQUIRED_CHECKS='["lint","minimum-deps","test (3.10)","test (3.11)","test (3.12)","test (3.13)","test (3.14)"]'

# Third-party actions are allowed by explicit pattern only. release.yml is the
# only workflow holding PYPI_API_TOKEN, so anything running beside it is a
# credible path to that token.
ACTION_PATTERNS='["pypa/gh-action-pypi-publish@*","softprops/action-gh-release@*"]'

say() { printf '  %s\n' "$*"; }

apply() { # apply <description> <method> <endpoint> <json>
  local desc="$1" method="$2" endpoint="$3" body="$4"
  if $CHECK_ONLY; then
    say "would set: $desc"
    return
  fi
  printf '%s' "$body" > /tmp/.repo-settings-body.json
  # gh api -f sends booleans as strings and the API rejects them; use --input.
  gh api -X "$method" "$endpoint" --input /tmp/.repo-settings-body.json >/dev/null
  rm -f /tmp/.repo-settings-body.json
  say "set: $desc"
}

echo "Repository: $REPO"
$CHECK_ONLY && echo "(check only -- nothing will be changed)"

echo
echo "Branch protection on $BRANCH"
# enforce_admins is true: the rules bind the maintainer too. Everything reaches
# main through a pull request with green CI. Tag pushes are unaffected, so
# releases still work, and no review is required, so a solo maintainer can merge
# their own PR as soon as the checks pass.
apply "required checks, no force-push, no deletion, applies to admins" PUT \
  "repos/$REPO/branches/$BRANCH/protection" \
  "{\"required_status_checks\":{\"strict\":true,\"contexts\":$REQUIRED_CHECKS},
    \"enforce_admins\":true,
    \"required_pull_request_reviews\":null,
    \"restrictions\":null,
    \"allow_force_pushes\":false,
    \"allow_deletions\":false,
    \"required_conversation_resolution\":true}"

echo
echo "Actions"
# Order matters: allowed_actions must be 'selected' before the allowlist can be
# written, otherwise the second call returns 409.
apply "restrict to selected actions, require SHA pinning" PUT \
  "repos/$REPO/actions/permissions" \
  '{"enabled":true,"allowed_actions":"selected","sha_pinning_required":true}'

apply "allowlist: GitHub-owned plus release actions" PUT \
  "repos/$REPO/actions/permissions/selected-actions" \
  "{\"github_owned_allowed\":true,\"verified_allowed\":false,\"patterns_allowed\":$ACTION_PATTERNS}"

apply "GITHUB_TOKEN read-only, cannot approve PRs" PUT \
  "repos/$REPO/actions/permissions/workflow" \
  '{"default_workflow_permissions":"read","can_approve_pull_request_reviews":false}'

# Fork pull requests already run without secrets because CI triggers on
# `pull_request` rather than `pull_request_target`. This additionally stops them
# consuming CI or probing the workflows before a maintainer has looked.
apply "external pull requests need approval to run workflows" PUT \
  "repos/$REPO/actions/permissions/fork-pr-contributor-approval" \
  '{"approval_policy":"all_external_contributors"}'

echo
echo "Discoverability"
# Topics and description are the only search surface GitHub gives a repository,
# and like everything else here they live in the API -- recreating the repo in
# 2026-08 lost them once already.
#
# The description is written in the words somebody with this problem would type,
# not the words the tool uses for itself. "Preview the DDL changes dbt run will
# execute" describes the mechanism; nobody searches for that. They search for a
# breaking change, a dropped column, a data contract.
apply "description and homepage" PATCH "repos/$REPO" \
  '{"description":"Catch breaking dbt schema changes before dbt run executes them: dropped columns, the downstream models and data tests they break, contract violations. Static analysis of compiled SQL, no warehouse connection. Like terraform plan, for dbt.","homepage":"https://presentjay.github.io/dbt-plan"}'

# Topics are exact-match search terms, capped at 20, so each one has to be a term
# a person would actually filter by. "sql" was dropped for that reason -- hundreds
# of thousands of repositories carry it and this one will never rank among them.
apply "topics" PUT "repos/$REPO/topics" \
  '{"names":["dbt","dbt-core","breaking-changes","data-contracts","analytics-engineering","data-engineering","data-quality","static-analysis","sqlglot","ci","ci-cd","github-actions","developer-tools","schema-migration","snowflake","bigquery","databricks","duckdb","redshift","postgres"]}'

echo
echo "Current state"
gh api "repos/$REPO/branches/$BRANCH/protection" > /tmp/.rs-prot.json
gh api "repos/$REPO/actions/permissions" > /tmp/.rs-perm.json
gh api "repos/$REPO/actions/permissions/selected-actions" > /tmp/.rs-sel.json
gh api "repos/$REPO/actions/permissions/workflow" > /tmp/.rs-wf.json
python3 - <<'PY'
import json
p = json.load(open('/tmp/.rs-prot.json'))
a = json.load(open('/tmp/.rs-perm.json'))
s = json.load(open('/tmp/.rs-sel.json'))
w = json.load(open('/tmp/.rs-wf.json'))
print(f"  required checks     : {len(p['required_status_checks']['contexts'])}")
print(f"  force push          : {p['allow_force_pushes']['enabled']}")
print(f"  branch deletion     : {p['allow_deletions']['enabled']}")
print(f"  applies to admins   : {p['enforce_admins']['enabled']}")
print(f"  allowed actions     : {a['allowed_actions']}")
print(f"  sha pinning required: {a['sha_pinning_required']}")
print(f"  verified allowed    : {s['verified_allowed']}")
print(f"  default token perms : {w['default_workflow_permissions']}")
PY
rm -f /tmp/.rs-*.json
