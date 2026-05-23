# `update` command — design notes

Working notes for the interactive workflow-updater command. Living document;
edit freely as decisions change.

## Goal

Walk the user (or an agent) through each outdated external action used by a
repo's workflows, let them choose how to update it (à la `git add -p`), then
commit and push the result as a PR.

## Shape

```
github-actions-scan update OWNER/REPO            # default: interactive
github-actions-scan update --here                # use cwd; infer OWNER/REPO from origin
github-actions-scan update OWNER/REPO --emit     # print TSV; do not edit
github-actions-scan update OWNER/REPO --decisions decisions.json
```

Default end-to-end flow:

1. Sparse-clone `OWNER/REPO` (just `.github/`) into `./working/OWNER/REPO/`,
   or reuse the existing clone there.
2. Run the scan + release-lookup pipeline against the local files.
3. Show a one-line summary, then interactively prompt per outdated action.
4. Check out a feature branch (`github-workflows-update/<date>`).
5. Apply chosen edits to the workflow YAML files.
6. Show the `git diff`.
7. Stage + commit on the feature branch.
8. Show a PR preview; prompt y/n.
9. Run `gh pr create`. `gh` handles the fork-and-push prompt if the user
   lacks write access.

The user never has to clone the repo themselves; the tool is usable against
any public repo from any cwd.

## Per-action prompt sketch

```
[3/5] actions/checkout@v4
      File:   working/monarch-initiative/koza/.github/workflows/test.yaml:42
      Action: https://github.com/actions/checkout

      40│     steps:
      41│       - name: Check out
  >   42│         uses: actions/checkout@v4
      43│         with:
      44│           fetch-depth: 0

  Current: v4               34e11487  2025-11-13
  Latest:  v6.0.2           de0fac2e  2026-01-09
  Major:   v6

  Options:
    (m) pin to major tag (v6)
    (e) pin to exact tag (v6.0.2)
    (s) pin to SHA (de0fac2e)
    (n) leave as is

    Uppercase (M/E/S/N) applies to this and 1 more occurrence of actions/checkout@v4.

    (q) quit (keep changes made so far)
    (?) help

  Choose [m/e/s/n/q/?/M/E/S/N]:
```

- The `Action:` link points to the *action's* repo so the user can read
  changelog / release notes / commit diff in a browser.
- The `File:` line shows the local path the tool would edit and the
  matched line number.
- ±5 lines of context come from grepping `uses_target` in the workflow
  file at prompt time (no YAML line-tracking loader needed).

### Missing major tag

When the action repo doesn't publish a moving major-version tag (e.g.
`astral-sh/setup-uv`, which intentionally stopped doing so for supply-chain
reasons), `(m)` stays in the menu but with an annotation:

```
    (m) pin to major tag — not available for astral-sh/setup-uv
```

Pressing `m` or `M` in that case prints a short note and re-prompts.

### Bulk-apply via uppercase variants

When the same `uses_target` appears in more than one workflow file, uppercase
`M`/`E`/`S`/`N` apply the choice to *this occurrence and every remaining
WORKFLOW@TAG match in the queue* — one keypress, batch operation. This
matches `git add -p` convention (lowercase = this hunk, uppercase = this and
all later).

The uppercase variants appear in the valid-key list only when there are
pending matches. Otherwise the menu shows only lowercase.

### Quit semantics

`(q)` follows `git add -p`: it ends the prompt loop but keeps every choice
already made. The user proceeds to the apply + commit + PR-preview steps
with that partial set.

### Statuses we prompt on

Only `outdated`. `up_to_date`, `no_release`, and `ref_unknown` are mentioned
in the summary but skip the per-action prompt — nothing actionable.

## Clone management

The tool sparse-clones (`--filter=blob:none`, sparse-checkout limited to
`.github/`) into a directory it owns. Sparse so the on-disk footprint is
tiny even across many repos.

- **Default clone location:** `./working/<owner>/<repo>/` (relative to cwd).
  Visible, easy to inspect. This repo's `.gitignore` already covers
  `working/` so users running the tool from inside it won't see clone
  contents in `git status`.
- **In-place mode (`--here`):** skip the clone step entirely; use cwd as the
  working tree. `OWNER/REPO` is inferred from `git remote get-url origin`;
  bail if there's no `origin` remote. A dirty working tree triggers a
  warning but the run proceeds — the user is expected to start from the
  remote HEAD of the default branch, and any deviation is their concern.
- **Re-use vs re-clone:** if the target clone already exists, leave it alone
  and continue from whatever state it's in (resume semantics).
  `--force-reclone` to delete and start fresh.
- **Cleanup after run:** keep the clone around. User can `rm -rf working/`
  themselves.

## Branch / commit / PR

- **Branch name.** Default `github-workflows-update/<date>` (e.g.
  `github-workflows-update/2026-05-23`). Configurable via `--branch`.
- **Branch collision.** If the named branch already exists in the clone
  (from a prior run on the same day), reuse it — same resume semantics as
  the clone itself.
- **Commit.** Done automatically after applying decisions. Commit message
  is built from a deduplicated bullet list keyed on
  `(uses_repo, current_ref, choice)`, so three identical updates to
  `astral-sh/setup-uv@v7` become a single bullet.
- **PR preview.** Always shown after the commit (branch, title, body).
  Visible even under `--no-pr` so the user can sanity-check what would be
  PR'd before deciding.
- **PR creation.** Prompts `Create pull request? [Y/n]`. Yes runs
  `gh pr create` with stdio inherited so gh's own fork/push prompt works
  interactively. Declining leaves the local branch + commit in place — the
  user can run `gh pr create` manually later.
- **`--no-pr`** skips the y/n prompt and the `gh pr create` call entirely.
  Branch + commit still happen.
- **PR title/body template:**
  ```
  title: Update GitHub Actions
  body:
    Updates from `github-actions-scan update`:

    - actions/checkout: v4 → v6 (major tag)
    - actions/setup-python: main → v6 (major tag)
    - astral-sh/setup-uv: v7 → 08807647 (SHA)
  ```
- **Forking.** `gh pr create` handles auto-forking when the user lacks
  write access to the upstream repo; nothing to do on our end.

## Decision producers (human + agent paths)

The interactive prompt is one of three ways to produce the list of decisions
applied to workflow files. The architecture separates *producing decisions*
from *applying them*:

```
ActionUpdate[]  →  Decision[]  →  apply to files  →  branch/commit/PR
                  ^^^^^^^^^^^
                  pluggable producers
```

### Interactive (default — humans)

The prompt loop described above. Runs when neither `--emit` nor `--decisions
FILE` is set.

### Data-only emit (`--emit`)

Prints the same TSV as `repo`, then exits without prompting, editing,
committing, or PR'ing. Used by an agent (or a script) as the input for its
own decision-making process.

### Pre-computed decisions (`--decisions FILE`)

Reads a JSON file of explicit `Decision` records and applies them
non-interactively. After applying, it follows the same commit + PR-preview +
confirm path as interactive. This is the agent-friendly producer: an agent
runs `--emit`, examines each outdated action (release notes, changelog,
commit diff), then writes a `decisions.json` like:

```json
[
  {"workflow_path": ".github/workflows/test.yaml",
   "uses_target": "actions/checkout@v4",
   "choice": "major"},
  {"workflow_path": ".github/workflows/documentation.yaml",
   "uses_target": "actions/checkout@main",
   "choice": "exact"}
]
```

Valid `choice` values: `major`, `exact`, `sha`, `skip`. Malformed JSON,
missing keys, and invalid choices all raise clear typer errors. Decisions
referencing unknown `(workflow_path, uses_target)` pairs log a warning and
are skipped.

### Intentionally absent: `--auto STRATEGY`

We deliberately do **not** offer a blanket "auto-update everything to major"
flag. Every update should be a deliberate choice the user or agent stands
behind, informed by the action's release notes, changelog, or commit diff.
`--decisions FILE` supports automation without giving up that per-action
intentionality — the agent has to enumerate each decision explicitly.

## Human review checkpoints

Both producers preserve human-in-the-loop review:

| Trust level | Agent does | Human does |
|---|---|---|
| Low | `--emit` + write `decisions.json` | review json, apply, review diff, push, PR |
| Medium | `--emit` + write `decisions.json` + apply with `--no-pr` | review diff locally, push, PR manually |
| High (full chain) | everything through `gh pr create` | review PR on GitHub, merge |

The PR on GitHub is always the final gate.

## Code sharing with `repo` command

Pipeline stays the same — only the workflow source changes:

- `find_action_updates(client, records, progress)` takes a records iterable
  instead of scanning internally.
- `repo` command builds records from `scan_repo_workflows(client, repo)`
  (remote API path; useful for "just survey a repo, no clone").
- `update` command builds records from `scan_cloned_workflows(clone_dir,
  owner_repo)` (reads `.github/workflows/*.y{,a}ml` from the sparse clone).

Both feed the same downstream pipeline.

## Implementation status

Built (across `Add update command with sparse-clone scaffolding` →
`PR preview + gh pr create`):

- `clone.py` — `ensure_clone`, `sparse_clone`, `resolve_here`,
  `working_tree_is_dirty`.
- `scan.py` — `scan_cloned_workflows` (alongside existing
  `scan_repo_workflows`).
- `editor.py` — `target_for_choice`, `rewrite_workflow`,
  `apply_decisions`, `load_decisions`, `diff`.
- `prompts.py` — `prompt_for_decisions` with the menu shown above; ±5 lines
  of context, uppercase bulk-apply variants, missing-major-tag annotation.
- `git_ops.py` — `default_branch_name`, `ensure_branch`,
  `commit_workflows`, `summarize_changes`, `build_commit_message`,
  `build_pr_title`, `build_pr_body`, `create_pr`.
- `cli.py` `update` subcommand wiring all of the above, with flags:
  `--here`, `--work-dir`, `--force-reclone`, `--emit`, `--decisions FILE`,
  `--branch`, `--no-pr`, `--header`, `--dry-run`,
  `--progress/--no-progress`, `--log-level`.

## Out of scope (or deferred)

- **`--yes` / `-y`** to skip the PR-confirmation prompt. Useful for fully
  scripted agent runs; currently the agent has to pipe `y\n` via stdin.
- **Composite actions / reusable workflows** referenced via `uses:`. Same
  parsing path; write-back should "just work" but is untested.
- **Operating on the user's own pre-existing local clone** (rather than a
  tool-managed one). `--here` covers this case but assumes cwd is the
  intended working tree.
- **Per-file bulk actions** (`a` apply-to-rest-of-file, `d` skip-rest-of-file)
  from `git add -p`. Not added; the uses_target uppercase variants serve a
  similar purpose for the common "same action across many files" case.
