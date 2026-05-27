---
name: gh-external-audit-org
description: Audit and update external GitHub Action references across every repo in a GitHub organization. Use this when the user asks to audit, update, bump, or review actions org-wide (e.g. "audit all external actions in monarch-initiative", "update workflows across my-org"). Enumerates repos, then runs the per-repo gh-external-audit flow on each, with rate-limit awareness so the agent doesn't get throttled mid-run.
---

# gh-external-audit-org (agent driver)

This skill drives `gh-external-audit` across every repo in a GitHub
organization. It's the org-wide companion to the per-repo
`gh-external-audit` skill — read that skill first; the per-repo decision
rules (release notes / changelog / README fallback, `@main` flagging,
SHA non-downgrade, etc.) all apply here verbatim. This document only
adds the org-level orchestration on top.

## Invocation convention

Same as the per-repo skill: `uv run gh-external-audit ...` from this
project's directory. Bare `gh-external-audit` only works if installed
globally.

## End-to-end workflow

### 1. Confirm org-wide preferences

Ask the user **once** for these (use a single combined question if your
host UI supports multi-select, otherwise four short asks). Don't ask
again mid-run.

- **Pin style preference** — `major` / `exact` / `sha` / "you decide".
  Same options as the per-repo skill. If the user defers, default to
  `major` when the action publishes a moving major tag, else `exact`.
- **Activity filter** — by default, only repos pushed to in the last
  365 days. Workflows in dormant repos rarely benefit from updates and
  add noise to the report. Offer "all" as an override.
- **Archived repos** — skipped by default.
- **PR behavior** — `none` (default; just commit locally and let the
  user PR later), `all-at-end` (after all repos are processed, run
  `gh pr create --fill` on each repo with edits, with a single
  confirmation), or `per-repo` (ask before each PR — usually too noisy
  org-wide, but offered for completeness).

### 2. Check the API rate budget

Before enumerating, check the rate limit:

```sh
gh api rate_limit --jq .resources.core
```

Each per-repo `update` run typically costs **15–25 API calls** (one
listing call for workflow files, one per workflow file, plus per
unique action: latest-release lookup + major-tag probe + commit-SHA
resolve + per-unique-pinned-ref commit info). Estimate **~20 calls per
repo** as a working figure.

If `remaining / 20 < repos_to_process`, surface the math to the user
and offer:
- Proceed anyway (will throttle near the end; agent will pause when
  remaining gets low — see step 4a).
- Limit scope: process only the first N repos that fit the budget.
- Wait for reset (the response includes `reset` as a Unix timestamp).

### 3. Enumerate repos

```sh
gh repo list ORG \
  --limit 1000 \
  --json name,owner,pushedAt,isArchived \
  --jq '[.[] | select(.isArchived == false)
        | select(.pushedAt > "ONE_YEAR_AGO_ISO")
        | "\(.owner.login)/\(.name)"]'
```

Where `ONE_YEAR_AGO_ISO` is today minus 365 days in RFC 3339, e.g.
`2025-05-26T00:00:00Z`. If the user opted out of either filter, drop
the corresponding `select`. Use `--limit 1000` (or higher) to make
sure the org's full repo list is fetched in one call when possible.

The result is an array of `OWNER/REPO` slugs. Keep this list in memory
for the loop.

### 4. Per-repo loop

For each `OWNER/REPO` in the list, **in sequence** (do not parallelize):

a. **Rate-limit guard.** Every 5 repos, re-check
   `gh api rate_limit --jq .resources.core.remaining`. If it drops
   below `30`, pause and tell the user:
   - How many repos are left
   - When the limit resets (convert the Unix timestamp to a human-
     readable delta)
   - Three choices: continue and throttle, stop here, wait until reset
     before continuing.

b. **Run the per-repo flow inline.** Don't try to invoke the per-repo
   skill recursively — the host doesn't necessarily support nested
   skill loads. Just inline the logic from steps 2–6 of the per-repo
   skill, with the org-wide pin preference already chosen:

   1. `uv run gh-external-audit update OWNER/REPO --emit --no-progress
      > /tmp/audit-OWNER-REPO.tsv`
   2. If no `outdated` rows, record "no updates needed" and move on.
   3. For each outdated row, find authoritative documentation
      (release-notes range with the `published_at` filter, then
      CHANGELOG, then README at `?ref=LATEST_TAG`). Apply the per-repo
      skill's decision rules.

      **Re-decide per repo.** Do *not* cache decisions across repos:
      `actions/checkout@v4` in repo A may be safe to bump to `v6` while
      repo B uses a `with:` parameter that v6 changed. The release-note
      reads can be cached across repos if you want (the notes don't
      vary by caller), but the per-row decision must consider each
      workflow's actual `with:` block.
   4. Write `decisions-OWNER-REPO.json`.
   5. `uv run gh-external-audit update OWNER/REPO --decisions
      /tmp/decisions-OWNER-REPO.json --no-pr --no-progress`

c. **Classify and record** per repo. After the apply step, assign one
   of these four classifications based on what happened:

   - **`safe`** — at least one action was updated, *and* every
     decision was clean: no `skip` due to breaking changes, no
     `@main`/`@master` flag, no missing-docs skip. This repo's commit
     can be opened as a PR without further review.
   - **`needs-review`** — at least one action was updated, but the
     run also produced at least one flag worth a human's eyes
     (skipped-due-to-breaking-change, `@main` security flag, or
     missing-docs skip). The commit is real and may be PR-worthy, but
     the user should look at the flags first.
   - **`no-edits`** — the per-action decisions all resolved to `skip`
     or the action set was empty. Nothing to PR.
   - **`error`** — the `--emit` or `--decisions` step exited non-zero,
     or the clone failed. Surface the error message.

   Record the slug, classification, count of updates, list of flags
   (with one-line reasons), and any error message.

d. **Repos with no workflows** show up as the `--emit` step yielding
   no rows (or no `.github/workflows/` directory at all). Classify as
   `no-edits` with reason "no workflows" and continue.

### 5. Optional batch PR phase

If the user chose `all-at-end` at step 1, after the loop:

1. Partition the repos by classification:
   - **Safe**: `M` repos. No flags, no skipped-due-to-breaking-change
     decisions, no `@main`/`@master` warnings.
   - **Needs review**: `K` repos. A real commit exists, but at least
     one flag the user should know about (list a one-line reason per
     repo: which action, which workflow file, what kind of flag).
   - **No edits / errors**: `P` repos. Nothing to PR.

2. Show the user the partitions and ask once, with four choices:

   > Of N repos with edits:
   > - **Safe** (M): ready to auto-PR.
   > - **Needs review** (K): each has a flag (listed below).
   > - **No edits / errors** (P): nothing to PR.
   >
   > [list one line per needs-review repo here, e.g.
   >  "monarch-initiative/mondo: 1 action skipped — `peter-evans/create-pull-request@v5` v8 renamed `commit-message` to `commit-msg` which this workflow uses"]
   >
   > 1. Open PRs for the M safe repos only. (Recommended.)
   > 2. Open PRs for all M + K repos (you'll see the flags on GitHub).
   > 3. Walk me through the K needs-review repos so I can decide each.
   > 4. Don't open any PRs.

3. For each selected repo, run the PR flow sequentially. `gh pr
   create` won't auto-push or auto-fork in a non-TTY context (its
   docs claim it prompts; that's interactive-only), so push and fork
   explicitly. First check write access:

   ```sh
   gh api /repos/OWNER/REPO --jq '.permissions.push // false'
   ```

   Then either:

   - **Write access (`push == true`):**
     ```sh
     (cd working/OWNER/REPO && \
       git push -u origin BRANCH && \
       gh pr create --fill)
     ```

   - **No write access (`push == false`):**
     ```sh
     cd working/OWNER/REPO
     gh repo fork OWNER/REPO --remote --remote-name=fork
     git push -u fork BRANCH
     gh pr create --fill --head $(gh api /user --jq .login):BRANCH
     ```

   `BRANCH` is the feature branch the per-repo tool created
   (`github-workflows-update/<date>` by default; the same name was
   used across all repos in this run).

4. Don't stop on the first error — collect URLs (or error messages)
   for the roll-up. Common failures to expect:
   - `gh repo fork` fails because a divergent fork already exists.
   - `git push` rejected (rare for the fork case; possible if the
     branch already exists on the remote with a different history).
   - Surface these as a "couldn't auto-PR" list per repo so the user
     can fix them one by one.

If the user chose `none` at step 1, or declines at the confirmation
above, all commits remain on local branches at `working/OWNER/REPO/`.
They can run `gh pr create --fill` themselves later from each clone
directory (with the same push/fork steps above as needed).

### 6. Roll-up report

Group the final summary by classification:

- **Safe** (auto-PR-eligible): list of `OWNER/REPO` with one-line
  change summary each (e.g. "monarch-initiative/mondo: 3 actions
  bumped to major"). If PRs were opened (step 5), include the URL
  per repo here.
- **Needs review**: list of `OWNER/REPO` with one-line summary of the
  flag(s). Include the workflow file path and the offending
  `uses_target` so the user can jump straight in. Note whether PRs
  were opened for these (option 2 or 3 from step 5) or not.
- **No edits**: collapsed list (slugs only), grouped by reason
  ("already up to date", "no workflows", "all decisions resolved to
  skip").
- **Errors**: any per-repo failures (clone failures, gh errors, etc.)
  with the slug and short error message.

Then the org-wide tallies:

- **Rate-limit summary**: API calls used (`before.remaining -
  after.remaining`), how many remained at the end.
- **PRs created**: count + URLs. Separately list repos that
  `gh pr create` couldn't auto-PR (typically fork-required); those
  need a manual run.

## Invocation preferences the user might pass

Watch for these in the user's natural-language request and adjust
defaults:

- "audit / scan only" — skip the apply step and just produce a roll-up
  of what *would* change (run `--emit`, do the docs review, but don't
  write `decisions.json` or run `--decisions`).
- "include archived" — drop the `isArchived == false` filter.
- "all repos / regardless of activity" — drop the `pushedAt` filter.
- "open PRs" — set PR behavior to `all-at-end`.
- "prefer SHA / major / exact" — set pin preference accordingly,
  skipping step 1's first ask.
- "limit to first N" — slice the enumerated list before the loop.

## Don'ts

In addition to the per-repo skill's don'ts:

- **DO NOT parallelize repos.** Sequential execution keeps the rate
  limit predictable and the report ordered. Parallel calls also make
  it harder to surface a single repo's failure cleanly.
- **DO NOT cache per-row decisions across repos.** Different repos may
  call the same action with different `with:` parameters; a bump
  that's safe in repo A can break repo B. Re-do the decision per repo.
  (Caching the release-notes *reads* themselves across repos is fine
  — those don't vary by caller.)
- **DO NOT open PRs mid-loop without explicit user opt-in.** Batch at
  the end is the default; per-repo confirmation is opt-in only.
- **DO NOT keep running into a depleted rate limit.** When remaining
  drops below 30, stop and confer with the user.
