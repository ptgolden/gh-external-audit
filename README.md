# github-actions-scan

Scan every repository in a GitHub organization for workflow files that pin
external GitHub Actions, then audit each unique action ref against a set of
checks. Writes a TSV problem report (one row per workflow use × problem) to
stdout. Today the only check flags JavaScript actions running on Node older
than version 24.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- [`gh`](https://cli.github.com/) on your `PATH`, authenticated (`gh auth login`)

## Run

```sh
uv run github-actions-scan ORG > report.tsv
```

(Or equivalently `uv run python -m github_actions_scan ORG`.)

Useful flags:

- `--repo-limit N` (or `REPO_LIMIT=N`) cap how many repos to scan
- `--dry-run` print the planned configuration without calling GitHub
- `--no-progress` suppress stderr progress logs and the tqdm bar
- `--no-header` omit the TSV header row
- `--log-level DEBUG` more verbose progress logging

## Checks

All checks live in `src/github_actions_scan/checks.py`. Each check is a
function that takes a parsed `action.yml` (a `dict`) and yields zero or more
`ProblemRecord(code, detail)` values. The full list is `ACTION_CHECKS`, and
`audit_action` fans out over it.

To add a new check:

1. Write a function `check_my_thing(metadata: dict[str, Any]) -> Iterable[ProblemRecord]`
   that yields one `ProblemRecord` per finding. `code` is the machine-readable
   tag that lands in the `problem` TSV column; `detail` is an optional short
   string for the `detail` column.
2. Append it to `ACTION_CHECKS`.

See `check_node_runtime` for a worked example.
