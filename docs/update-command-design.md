# `update` command — design notes

Working notes for the interactive workflow-updater command. Living document; edit
freely as decisions firm up.

## Goal

Walk the user through each outdated external action used by a repo's workflows
and let them choose how to update it (à la `git add -p`), then push the result
as a PR.

## Shape

```
github-actions-scan update OWNER/REPO
```

The tool owns the working copy:

1. Sparse-clone `OWNER/REPO` (just `.github/`) into a tool-managed directory.
2. Run the existing scan+release-lookup pipeline against the local files.
3. Show a one-line summary, then interactively prompt per outdated action.
4. Commit the chosen changes on a new branch.
5. Run `gh pr create`. `gh` handles the fork-and-PR flow if the user lacks
   write access.

The user never has to clone the repo themselves; the tool is usable against
any public repo from any cwd.

## Per-action prompt sketch

```
[3/5] actions/checkout@v4
      File:   working/monarch-initiative/koza/.github/workflows/test.yaml:42
      Action: https://github.com/actions/checkout

  40│     steps:
  41│       - name: Check out
  42│       - uses: actions/checkout@v4
  43│         with:
  44│           fetch-depth: 0

  Currently: actions/checkout@v4
             → 34e11487, committed 2025-11-13

  Options:
    (m) pin to major tag     v6              de0fac2e  2026-01-09
    (e) pin to exact tag     v6.0.2          de0fac2e  2026-01-09
    (s) pin to SHA           de0fac2e        de0fac2e  2026-01-09
    (n) leave as is
    (q) quit (keep changes made so far)
    (?) help

  Choose [m/e/s/n/q/?]:
```

The `Action:` link points to the *action's* repo (e.g. `actions/checkout`), so
the user can open changelog/release notes in a browser. The `File:` line is
the local path the tool will edit.

For actions where the major tag doesn't exist (e.g. `astral-sh/setup-uv`),
`(m)` stays in the menu but with an annotation, e.g.:

```
    (m) pin to major tag     (no major tag published for astral-sh/setup-uv)
```

Pressing `m` in that case prints a short note and re-prompts.

## Bulk-apply for repeat (action, ref) pairs

When the same `uses_target` (e.g. `actions/checkout@v4`) appears in more than
one workflow file, the user almost certainly wants to handle them the same
way. So:

- The first prompt for a given `uses_target` has the normal menu.
- All later prompts for the same `uses_target` gain an extra option `(A)`
  that applies the previously-chosen decision (m/e/s/n) to *every* remaining
  occurrence of that `uses_target` in the queue — one keypress, batch
  operation.
- The `(A)` line should remind the user what they'd be applying, e.g.:
  ```
  (A) apply previous choice (exact tag v6.0.2) to 2 more occurrences
  ```

The `(A)` option does not appear on the first occurrence of a `uses_target`
(no prior decision to copy).

### Quit semantics

`(q)` follows `git add -p`: it ends the prompt loop but keeps every choice
already made. The user is then taken to the PR-preview prompt with that
partial set of changes.

## Smaller decisions

### Context lines

We already read each workflow's text during scan. Cache it. At prompt time,
grep for `uses_target` to find the line number(s), slice ±5 lines. A
yaml-loader that tracks line numbers is fancier but overkill — `uses:
owner/repo@ref` is unique enough to grep.

### Same action twice in one file

If `actions/checkout@v4` appears on two lines of the same workflow, one user
decision applies to both. `dedupe_scan_records` already collapses identical
rows.

### Statuses to skip

Only prompt for `outdated`. Mention `up_to_date`, `no_release`, `ref_unknown`
in the summary but don't prompt — nothing actionable.

### Quit and skip-file actions

Start with `n` (skip this one), `q` (quit, no more changes written), `?`
(help). Add `a` (apply same choice to all remaining in file) and `d` (skip
rest of file) later if iteration shows need.

### Press-any-key after the summary

Skip it. If there's nothing to update, print the summary and exit. If there
is, prompt y/n "proceed?" so they can bail upfront.

### Command name

`update` as a third subcommand alongside `org` and `repo`.

## Clone management

The tool sparse-clones (`--filter=blob:none`, sparse-checkout limited to
`.github/`) into a directory it owns. Sparse so the on-disk footprint is tiny
even across many repos.

- **Default clone location:** `./working/<owner>/<repo>/` (relative to cwd).
  Visible, easy to inspect. (This repo's `.gitignore` already covers
  `working/` so a user running the tool from inside it won't see clone
  contents in `git status`.)
- **In-place mode (`--here`):** skip the clone step entirely; use cwd as the
  working tree. `OWNER/REPO` is inferred from `git remote get-url origin`;
  bail if there's no `origin` remote. A dirty working tree triggers a
  warning but the run proceeds — the user is expected to start from the
  remote HEAD of the default branch, and any deviation is their concern.
- **Re-use vs re-clone (cloned mode).** If `./working/<owner>/<repo>/` already
  exists, leave it alone and continue from whatever state it's in. This
  supports resuming a partially-finished session. `--force-reclone` to delete
  and start fresh.
- **Cleanup after run.** Keep the clone around (user can `rm -rf working/`
  themselves).

## Branch / commit / PR

- **Branch name.** Default `github-workflows-update/<date>` (e.g.
  `github-workflows-update/2026-05-22`). Configurable via `--branch`.
- **Branch collision.** If the default-named branch already exists in the
  clone (from a prior run on the same day), reuse it — same resume semantics
  as the clone itself.
- **PR creation.** After the interactive flow ends, show a preview of the
  proposed PR (branch, title, body, diff summary) and prompt y/n. Default is
  to PR; `--no-pr` skips and leaves the local branch for manual handling.
- **PR title/body template:**
  ```
  title: Update GitHub Actions
  body:
    Updates from `github-actions-scan update`:

    - actions/checkout: v4 → v6 (major tag)
    - actions/setup-python: main → v6 (major tag)
    - astral-sh/setup-uv: v7 → 08807647 (SHA)
  ```
- **Forking.** `gh pr create` handles auto-forking if the user lacks write
  access to the upstream repo; nothing to do on our end.

## Code sharing with `repo` command

Pipeline stays the same — only the workflow source changes:

- `find_action_updates(client, records, progress)` is refactored to take a
  records iterable instead of scanning internally.
- `repo` command builds records from `scan_repo_workflows(client, repo)`
  (existing remote path; still useful for "just survey a repo, no clone").
- `update` command builds records from `scan_cloned_workflows(clone_dir)`
  (reads `.github/workflows/*.y{,a}ml` from the sparse clone).

Both feed the same downstream pipeline.

## Decision producers (human + agent paths)

The interactive prompt is one of two ways to produce the list of decisions
applied to workflow files. The architecture deliberately separates *producing
decisions* from *applying them*:

```
ActionUpdate[]  →  Decision[]  →  apply to files  →  branch/commit/PR
                  ^^^^^^^^^^^
                  pluggable producers
```

### Interactive (default — humans)

The prompt loop described above. Runs when neither `--emit` nor `--decisions
FILE` is set.

### Data-only emit (`--emit`)

Prints the same TSV as `repo`, then exits without prompting or editing. Used
by an agent (or a script) as the input for its own decision-making process.

### Pre-computed decisions (`--decisions FILE`)

Reads a JSON file of explicit `Decision` records and applies them. No prompts.
This is the agent-friendly path: an agent runs `--emit`, examines each
outdated action (release notes, changelog, commit diff), then writes a
`decisions.json` like:

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

Valid `choice` values: `major`, `exact`, `sha`, `skip`. Decisions referencing
unknown `(workflow_path, uses_target)` pairs log a warning and are ignored.

### Intentionally absent: `--auto STRATEGY`

We deliberately do **not** offer a blanket "auto-update everything to major"
flag. Every update should be a deliberate choice the user or agent stands
behind, informed by the action's release notes, changelog, or commit diff.
The `--decisions FILE` path supports automation without giving up that per-
action intentionality — the agent has to enumerate each decision explicitly.

## Human review checkpoints

Both producers preserve human-in-the-loop review:

| Trust level | Agent does | Human does |
|---|---|---|
| Low | `--emit` + write `decisions.json` | review json, apply, review diff, push, PR |
| Medium | `--emit` + write `decisions.json` + apply | review diff/PR on GitHub, merge |
| High (full chain) | everything through `gh pr create` | review PR on GitHub, merge |

The PR on GitHub is always the final gate.

## Out of scope for v1

- Updating composite actions or reusable workflows referenced via `uses:`
  (same parsing path; write-back should "just work" but untested)
- Operating on the user's own pre-existing local clone instead of a
  tool-managed one
