---
name: gh-external-audit
description: Audit and update external GitHub Action references (`uses:` lines) in a target repo's workflow files. Use this when the user asks to audit, review, bump, or update outdated GitHub Action pins for a specific repo. Drives the `gh-external-audit` CLI in non-interactive mode (no TTY), so the agent has to produce a decisions file rather than answering prompts.
---

# gh-external-audit (agent driver)

This skill drives the `gh-external-audit` CLI to update outdated external
GitHub Action references in a repository's workflow files.

## Invocation convention

All `gh-external-audit` commands in this skill are written as
`uv run gh-external-audit ...` because the tool is installed editably in
this project's `.venv/` and isn't on `PATH` outside `uv run`. Run them
from this project's directory (where `pyproject.toml` lives) so `uv` can
find the venv.

If the tool has been installed globally (e.g. `pipx install
gh-external-audit`), drop the `uv run` prefix.

## What the tool does

It sparse-clones the target repo (just `.github/`), scans every workflow
file for external `uses:` references, looks up each action's latest release
(and major-tag + commit SHA), then either:

- **`--emit`** prints a TSV of every action use, current pin, and what the
  latest release is — for an agent to read and decide on.
- **`--decisions FILE`** applies a JSON list of explicit decisions
  (one per `(workflow_path, uses_target)`) and shows the resulting diff.

The default mode is an interactive `git add -p`-style prompt. **You can't
use that** — there's no TTY. Always use `--emit` followed by `--decisions`.

## End-to-end workflow

### 1. Confirm the user's preference

Before anything else, ask the user **once** which pin style they prefer for
the routine "no breaking changes, just bump it" case:

- `major` — pin to the moving major-version tag (e.g. `v6`). Auto-tracks
  future v6.x.y releases. Lightest-touch, accepts future patches without
  re-review.
- `exact` — pin to the latest exact release tag (e.g. `v6.0.2`). Immutable
  for that release. Bumps need explicit re-runs of this tool.
- `sha` — pin to the immutable commit SHA. Most secure (no supply-chain
  surprise from a tag being silently re-pointed).

If the user said it in their initial request ("update koza, prefer SHA
pins"), skip this step.

If they decline to pick or say "you decide", default to **`major`** when
the action publishes a moving major tag, else **`exact`**.

### 2. Emit the data

```sh
uv run gh-external-audit update OWNER/REPO --emit --no-progress > /tmp/audit.tsv
```

This sparse-clones the repo to `./working/OWNER/REPO/` if it isn't there
already (or reuses it if it is) and prints a TSV with columns:

```
workflow_path, uses_target, uses_repo, uses_path, current_ref,
current_sha, current_published_at, latest_tag, latest_major_tag,
latest_sha, latest_published_at, latest_url, status
```

Only rows with `status == "outdated"` need a decision. Skip the rest.

### 3. Find authoritative documentation of the new version

For each unique `(uses_repo, current_ref → latest_tag)` pair, walk this
fallback chain — stop as soon as you find something useful:

**a. Release notes for every release in the range** (best signal — the
maintainer documents each release individually, and deprecations or
breaking changes can land at any point in the range, not just at the
latest one):

```sh
gh api "/repos/USES_REPO/releases?per_page=100" --paginate \
  --jq '[.[] | select(.published_at > "CURRENT_PUBLISHED_AT")
        | {tag_name, body, published_at, prerelease}]'
```

`CURRENT_PUBLISHED_AT` is the `current_published_at` column from the
TSV (we already resolve and emit it per row). One paginated call —
GitHub returns 100 releases per page and the jq filter prunes
everything older than the current pin. For most actions that's a
single HTTP request.

Read the `body` of every release in the result, in order. Pay
attention to:
- Deprecation notices that landed mid-range (the removal may not come
  until a later release).
- Breaking changes flagged with **Breaking**, **BREAKING CHANGE**, or
  similar conventions.
- Required-input additions (a new mandatory `with:` key the workflow
  doesn't pass).

Notes on edge cases:
- For branch pins (`@main`, `@master`), `current_published_at` is the
  branch head's commit date — the filter will mostly return the latest
  release, which is fine. Surface the branch pin as a security risk
  separately (step 4).
- If the result is empty (no releases since `current_published_at`, or
  the column is missing), fall through to source (b).

**b. Changelog** (most projects don't have one, but some do):

```sh
gh api /repos/USES_REPO/contents/CHANGELOG.md --jq .content 2>/dev/null | base64 -d
```

Also try `CHANGELOG`, `CHANGES.md`, `HISTORY.md`.

**c. README at the latest tag** (the fallback when nothing else exists —
gives you the action's *currently documented* usage, which you can compare
against the workflow's actual invocation):

```sh
gh api /repos/USES_REPO/readme?ref=LATEST_TAG --jq .content | base64 -d
```

The `/readme` endpoint auto-finds the file regardless of case or
extension. The point isn't to read the README cover-to-cover — it's to
find the usage example / "Usage" section that shows the action being
called, and check whether the workflow's `uses:` invocation still
matches that documented pattern (see step 4).

**Do not fetch the commit log.** It's too noisy for this purpose and not
the source of truth the maintainer is asking you to read.

### 4. Decide per action

For each outdated row, choose one of `major` / `exact` / `sha` / `skip`:

1. **Inspect the current workflow's usage.** Open the local file at
   `working/OWNER/REPO/<workflow_path>` and read the entire step the
   `uses:` line is in — its `with:` parameters, env vars, and surrounding
   context. (The CLI's interactive mode shows you the whole step block in
   one snippet; replicate that mentally.)

2. **Cross-reference the workflow with whatever you found in step 3.**

   *If you got release notes or a changelog*, look for breaking changes:
   - Has any **input** the workflow uses been renamed, deprecated, or
     changed in meaning?
   - Has any **output** the workflow consumes downstream been changed?
   - Has the action's runtime (Node version, container) changed in a way
     that affects this workflow?
   - Anything removed that the workflow doesn't use is fine — proceed.

   *If you only got the README*, check that the workflow's invocation
   pattern still matches the documented usage:
   - The `with:` keys the workflow passes are all in the README's
     documented inputs (or in an obvious "advanced/optional" section).
   - The README doesn't show a required input that the workflow is
     missing.
   - The `uses:` target itself hasn't been renamed (e.g.
     `foo/bar` → `foo/bar/sub`).

3. **Decide:**
   - If the documentation describes breaking changes that affect what the
     workflow uses, choose `skip` and surface a clear summary of what
     changed and what the user would need to update.
   - If the documentation is silent on anything the workflow uses (or
     confirms the invocation still works), apply the user's preference
     from step 1.
   - If you couldn't find *any* documentation (no release notes, no
     changelog, no README), choose `skip` — we have no signal at all.
   - If the current pin is `@main`, `@master`, or another branch name,
     **flag it as a security risk** in your summary. Recommend pinning to
     a release (use the user's preference for the target).
   - If the current pin is already a SHA, prefer `sha` (don't downgrade
     security).
   - If the action publishes no moving major tag (`latest_major_tag` is
     empty), `major` isn't available — `exact` or `sha` only.

### 5. Write the decisions file

```json
[
  {
    "workflow_path": ".github/workflows/test.yaml",
    "uses_target": "actions/checkout@v4",
    "choice": "major"
  },
  {
    "workflow_path": ".github/workflows/documentation.yaml",
    "uses_target": "actions/checkout@main",
    "choice": "exact"
  }
]
```

Include one entry per `(workflow_path, uses_target)` pair from the TSV's
outdated rows. `skip` decisions can be omitted, but including them
explicitly makes the agent's reasoning auditable.

### 6. Apply

```sh
uv run gh-external-audit update OWNER/REPO --decisions /tmp/decisions.json --no-pr --no-progress
```

`--no-pr` is **important**: it stops the run after the commit, before
`gh pr create` would run. Do not create PRs without the user's explicit
permission.

After applying, run `git -C working/OWNER/REPO log -1 -p` to show the user
exactly what landed.

### 7. Report to the user

Summarize:

- How many actions were updated and to what
- Which were skipped and why (especially "couldn't find release notes" and
  "breaking change in X parameter")
- Any `@main`/`@master` pins you flagged as security risks

### 8. Offer to open the PR

After the summary, show the user the exact `gh` command that would open
a pull request and ask whether you should run it. Use the `--fill` form
so the title and body come from the commit message the tool already
generated (it's already templated as "Update external GitHub workflows"
with a deduped bullet list):

> Open a pull request? I'd run:
>
> ```
> (cd working/OWNER/REPO && gh pr create --fill)
> ```
>
> Reply "yes" and I'll run it; reply "no" and I'll stop here (you can
> always run that command yourself later).

If the user confirms, run that exact command. `gh pr create` may prompt
about forking if the user lacks write access to the upstream repo —
that prompt won't work in a non-TTY context, so if the command fails
that way, surface the error and tell the user to run it interactively
themselves.

If the user declines, leave the branch and commit in place at
`working/OWNER/REPO/` — the user can `gh pr create` later whenever they
want.

## Invocation preferences the user might pass

These come from the user's natural-language request. Watch for them and
adjust behavior:

- "prefer SHA / use SHA pins / pin everything to commit" — preference =
  `sha`
- "prefer major / use major tags" — preference = `major`
- "exact only / pin to exact versions" — preference = `exact`
- "create a PR / open a PR" — drop `--no-pr` from the final command (run
  the full `update` invocation; the tool will prompt for confirmation that
  you can answer via stdin)
- "use my existing clone / I'm already in the repo" — add `--here` instead
  of `OWNER/REPO`, and omit the slug

## Don'ts

- **DO NOT** run `gh-external-audit update` without `--emit` or
  `--decisions`. The interactive prompt needs a TTY.
- **DO NOT** create a PR (`gh pr create` or running `update` without
  `--no-pr`) unless the user explicitly said to.
- **DO NOT** use `gh-external-audit org` for this — that's the org-wide
  audit command with a different purpose.
- **DO NOT** fetch and read commit logs as a substitute for release notes.
  If the maintainer didn't document a release, that's signal, not noise.
- **DO NOT** auto-decide every action regardless of release notes. The
  whole point of the tool is per-action intentionality.
