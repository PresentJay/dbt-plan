# Action reference and package version

An Action reference chooses the composite wrapper. The wrapper's `version`
input chooses the Python package `pip` installs. The two choices are
independent. Release notes ask you to review both when you upgrade; this page
is the short guide for that review.

This page documents the wrapper that already ships. It adds no version-selection
feature and changes no runtime default. **This guide does not move the
Marketplace `v1` tag.**

The examples use an invented `bookstore` project (`stg_titles`,
`fct_title_sales`). They contain no credentials and no warehouse schema. A YAML
reader can check them locally; nothing here needs a deploy.

## Two pins

| Choice | What it selects | Bookstore example |
|---|---|---|
| Action reference (`uses:`) | The composite wrapper: `action.yml` and its steps | `PresentJay/dbt-plan@v0.16.0` |
| `version` input | The `dbt-plan` distribution pip installs | `version: '0.16.0'` |
| Full commit on `uses:` | That wrapper tree, even if a moving tag later changes | `PresentJay/dbt-plan@805a3fd80a55a359d36e5bb7071a3915e976d8a7` |

`v0.16.0` is an annotated tag. The commit it releases is
`805a3fd80a55a359d36e5bb7071a3915e976d8a7` (`git rev-parse v0.16.0^{}`).
Pin the wrapper to the tag when you want that release's wrapper, or to the
full commit when you want that exact tree.

```yaml
# Wrapper tag and package version, chosen separately.
- uses: PresentJay/dbt-plan@v0.16.0
  with:
    project-dir: bookstore
    version: '0.16.0'
```

```yaml
# Same released wrapper, pinned by commit.
- uses: PresentJay/dbt-plan@805a3fd80a55a359d36e5bb7071a3915e976d8a7
  with:
    project-dir: bookstore
    version: '0.16.0'
```

Quote the package version. `'0.16.0'` is a string. The wrapper assigns that
string to the `VERSION` environment variable and passes it to pip. A YAML 1.1
reader keeps the quoted value as a string. A bare float such as `0.10` loads as
a number (`0.1`) and would not round-trip as the text the install step expects.

Checked while writing this page: `git rev-parse v1^{}` is
`00454918acb63d7b5fa7791fe5665567c606c873`, and `git rev-parse v0.16.0^{}` is
`805a3fd80a55a359d36e5bb7071a3915e976d8a7`. Those commits differ. **This guide
does not move the Marketplace `v1` tag.** Inspect `v1` again when you need the
commit it names today.

## What the wrapper installs

On that released commit, and in the current `action.yml`, `version` defaults to
`''`. The input arrives through the environment
(`VERSION: ${{ inputs.version }}`). The install step then branches:

```bash
if [ -n "$VERSION" ]; then
  pip install --quiet "dbt-plan==$VERSION"
else
  pip install --quiet dbt-plan
fi
dbt-plan --version
```

A non-empty `VERSION` installs that distribution. An empty `VERSION` installs
whatever release pip currently selects for the name `dbt-plan`. The step prints
`dbt-plan --version` so the job log shows the CLI that was installed.

`tests/test_action_yml.py` loads this `action.yml` and checks that the wrapper
stays aligned with the CLI it invokes. Input names used above (`version`,
`project-dir`, and the other inputs declared beside them) are the names in that
file. The `version` value is not interpolated into the `run:` script.

### A package pin leaves dependencies unresolved

`version: '0.16.0'` makes pip request `dbt-plan==0.16.0`. That release declares
`sqlglot>=28.0.0`. Pip may select any sqlglot that satisfies the range, and any
further packages those dependencies allow, at the moment the job runs. The pin
selects the dbt-plan distribution. It leaves that resolved dependency set, the
Python patch level, and the runner image unpinned, so it is not a fully
reproducible environment. Use a lockfile in the bookstore project when the rest
of the environment must stay frozen.

## Empty, explicit, and unchanged

**Empty package version.** Omit `version`, or set `version: ''`. Both reach the
default. The install step runs `pip install --quiet dbt-plan`.

```yaml
- uses: PresentJay/dbt-plan@v0.16.0
  with:
    project-dir: bookstore
    version: ''
```

The `@v0.16.0` ref still selects the 0.16.0 wrapper. The empty input still
installs the current package. The pip requirement is `$VERSION` or the bare
name `dbt-plan`; the `uses:` ref stays on the wrapper.

**Explicit package version.** `version: '0.16.0'` takes the other branch:
`pip install --quiet "dbt-plan==$VERSION"`. The same input works with a tag or
with the commit pin above. The wrapper ref and the package version can differ
on purpose: an older wrapper can install a newer package, and a newer wrapper
can install `0.16.0`, because the install step reads `VERSION` and not the
`uses:` ref.

**Unchanged older workflow.** A workflow that nobody has edited keeps the text
it already has. Two common bookstore files:

```yaml
# Composite wrapper, package input left unset.
- uses: PresentJay/dbt-plan@v1
  with:
    project-dir: bookstore
```

```yaml
# Line written by `dbt-plan ci-setup`. No == pin.
- name: Install
  run: uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python" dbt-plan
```

`@v1` selects whichever wrapper commit that tag currently names. The package
comes from `inputs.version` inside that wrapper. With the input omitted, the
default is empty, and the install branch runs `pip install --quiet dbt-plan`.
The tag text `v1` stays on the wrapper ref and is not the pip version. That is
why `@v1` by itself leaves the installed package version to the empty-input
branch.

Publishing or upgrading the Python package does not rewrite generated workflow
files. `dbt-plan ci-setup` writes `.github/workflows/dbt-plan.yml` only when
that file is absent. When the file already exists, the command exits and leaves
the bytes unchanged. The generated install line is literal text. A new wheel
on PyPI does not edit that file in the bookstore repository. The file keeps
installing whatever it still says until you edit it, or until you delete it,
run `dbt-plan ci-setup` again, and review the new file. See
[Exit codes and migration](exit-codes.md).

## Upgrade checklist

1. Read the migration notes for the package you are installing. The 0.16.0
   contract starts in [Exit codes and migration](exit-codes.md) and the
   [v0.16.0 release notes](releases/v0.16.0.md). Later releases live in
   [the release-notes directory](releases/).
2. Review both refs in the bookstore workflow: the `uses:` Action reference
   and the `version` input, or the generated `uv pip install` line if you do
   not use the composite wrapper.
3. Confirm the installed CLI. The wrapper runs `dbt-plan --version` after
   install; the job log should show the distribution you meant to pin.
4. Verify the project-specific fail-on policy. The Action input is `fail-on`.
   A generated workflow uses its own `FAIL_ON` value. `.dbt-plan.yml` may set
   `warning_exit_code` and `fail_on`. Those meanings are defined in
   [Exit codes and migration](exit-codes.md). This page does not restate them.
