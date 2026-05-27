# gh-external-audit

Audit and update external GitHub Action references (the `uses:` lines that
pull in third-party code) in a repo's workflow files.

The main flow walks every outdated action with a `git add -p`-style prompt,
lets you pick a pin style per use, edits the workflow YAML files, commits
on a feature branch, and optionally opens a pull request via `gh`.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- [`gh`](https://cli.github.com/) on your `PATH`, authenticated (`gh auth login`)

## Run

### Try in Claude

This tool has be designed to work well with Claude. After cloning this repository, open Claude code in the root directory, and try prompts like:

* Update the external GitHub Action workflows in `monarch-initiative/mondo`
* Update the external GitHub Action workflows in the `periodo` organization

### Interactive update of a single repo

```sh
uv run gh-external-audit update OWNER/REPO
```

Sparse-clones the repo into `./working/OWNER/REPO/`, scans for outdated
external action references, and prompts you per `(file, use)` with a
`git add -p`-style menu:

- `m` / `M` — pin to the moving major-version tag (`m` = this occurrence
  only; `M` = this and every remaining occurrence of the same `uses:` target)
- `e` / `E` — pin to the latest exact release tag
- `s` / `S` — pin to the immutable commit SHA
- `n` / `N` — leave as is
- `q` — quit, keeping decisions made so far
- `?` — help

After the loop the tool stages the workflow files, commits on a
`github-workflows-update/<date>` branch, shows a PR preview, and asks
before running `gh pr create`.

Use `--here` to operate on cwd (a clone you already have) instead of
sparse-cloning; `OWNER/REPO` is inferred from `origin`. Use `--no-pr` to
stop after committing locally.

### Non-interactive modes

For scripts and agents:

- `update OWNER/REPO --emit > audit.tsv` — print a TSV of every external
  action use with current pin, latest release, status. Nothing is edited.
- `update OWNER/REPO --decisions FILE` — apply a JSON file of decisions
  (one entry per outdated `(workflow_path, uses_target)` pair). Each
  decision specifies a `choice` (`major` / `exact` / `sha` / `skip`) and
  an optional `note` that surfaces in the commit message and PR body.

Two Claude Code skills under `.claude/skills/` drive the emit-decide-apply
loop end-to-end:

- `gh-external-audit` — single-repo flow (asks pin preference once, reads
  release notes per outdated action, builds a decisions file, applies, PRs)
- `gh-external-audit-org` — same flow looped across every repo in a
  GitHub organization, with rate-limit awareness and safe / needs-review
  classification per repo before opening PRs

### Legacy: org-wide Node-runtime audit

```sh
uv run gh-external-audit org ORG > report.tsv
```

Scans every repo in an organization, runs a set of per-action checks
(currently just `check_node_runtime`, which flags JavaScript actions on
Node older than v24), and writes a TSV problem report (one row per
workflow use × problem). Predates the `update` flow and stays for its
focused per-action check pipeline.

## Flags

`update`:

- `--here` — operate on cwd; infer `OWNER/REPO` from `origin`
- `--work-dir DIR` — where sparse clones live (default `./working/`)
- `--force-reclone` — delete any existing clone before fetching
- `--branch NAME` — override the feature-branch name
- `--no-pr` — commit locally; skip `gh pr create`
- `--emit` — data-only TSV mode
- `--decisions FILE` — apply pre-computed decisions
- `--header` / `--no-header` — TSV header toggle (for `--emit`)

`org`:

- `--repo-limit N` (or `REPO_LIMIT=N`) — cap how many repos to scan

Common:

- `--dry-run` — print the planned configuration without calling GitHub
- `--progress` / `--no-progress` — stderr progress logs and the tqdm bar
- `--log-level LEVEL` — more (or less) verbose progress logging

## Adding org-audit checks

All `org` checks live in `src/gh_external_audit/checks.py`. Each check is a
function that takes a parsed `action.yml` (a `dict`) and yields zero or more
`ProblemRecord(code, detail)` values. The full list is `ACTION_CHECKS`, and
`audit_action` fans out over it.

To add a new check:

1. Write `check_my_thing(metadata: dict[str, Any]) -> Iterable[ProblemRecord]`
   that yields one `ProblemRecord` per finding. `code` is the
   machine-readable tag that lands in the `problem` TSV column; `detail` is
   an optional short string for the `detail` column.
2. Append it to `ACTION_CHECKS`.

See `check_node_runtime` for a worked example.

## Design notes

- `docs/update-command-design.md` — design history for the `update` command
- `docs/agent-skill.md` — single-repo agent skill (also at
  `.claude/skills/gh-external-audit/SKILL.md`)
- `docs/agent-skill-org.md` — org-wide agent skill (also at
  `.claude/skills/gh-external-audit-org/SKILL.md`)
