import subprocess
from datetime import date
from pathlib import Path

from loguru import logger

from .editor import target_for_choice
from .models import (
    CHOICE_EXACT,
    CHOICE_MAJOR,
    CHOICE_SHA,
    CHOICE_SKIP,
    ActionUpdate,
    Decision,
)


def default_branch_name() -> str:
    """Return the default branch name for today's session."""
    return f"github-workflows-update/{date.today().isoformat()}"


def _run(
    args: list[str], cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, check=check, capture_output=True, text=True
    )


def _branch_exists(clone_path: Path, branch_name: str) -> bool:
    result = _run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=clone_path,
        check=False,
    )
    return result.returncode == 0


def ensure_branch(clone_path: Path, branch_name: str) -> None:
    """Check out branch_name, creating it from current HEAD if it doesn't exist.

    Reuses an existing branch by name (resume semantics). Working tree changes
    carry over via git's normal checkout behavior.
    """
    if _branch_exists(clone_path, branch_name):
        logger.info("reusing branch {}", branch_name)
        _run(["git", "checkout", branch_name], cwd=clone_path)
    else:
        logger.info("creating branch {}", branch_name)
        _run(["git", "checkout", "-b", branch_name], cwd=clone_path)


def commit_workflows(clone_path: Path, message: str) -> bool:
    """Stage `.github/workflows/` and commit. Returns False if nothing was staged."""
    _run(["git", "add", ".github/workflows"], cwd=clone_path)
    staged = _run(
        ["git", "diff", "--cached", "--quiet"], cwd=clone_path, check=False
    )
    if staged.returncode == 0:
        return False
    _run(["git", "commit", "-m", message], cwd=clone_path)
    return True


_CHOICE_LABELS = {
    CHOICE_MAJOR: "major tag",
    CHOICE_EXACT: "exact tag",
    CHOICE_SHA: "SHA",
}


def summarize_changes(
    decisions: list[Decision], updates: list[ActionUpdate]
) -> tuple[list[str], list[str]]:
    """Build deduplicated bullet lists summarizing the applied decisions.

    Returns (update_bullets, skip_bullets):
    - `update_bullets`: one per (uses_repo, current_ref, choice) where
      choice != skip. Includes the per-decision `note` inline if present.
    - `skip_bullets`: one per (uses_repo, current_ref) where the decision
      was `skip` *and* a note was given. Skips without notes are silent
      (the agent had no reason to surface).

    SHA targets in update bullets are abbreviated to 8 characters.
    """
    update_map = {(u.workflow_path, u.uses_target): u for u in updates}
    updates_seen: dict[tuple[str, str, str], tuple[str, str]] = {}
    skips_seen: dict[tuple[str, str], str] = {}

    for decision in decisions:
        update = update_map.get((decision.workflow_path, decision.uses_target))
        if update is None:
            continue

        if decision.choice == CHOICE_SKIP:
            if decision.note:
                key = (update.uses_repo, update.current_ref)
                skips_seen.setdefault(key, decision.note)
            continue

        new_target = target_for_choice(update, decision.choice)
        if new_target is None:
            continue
        new_ref = new_target.rsplit("@", 1)[1]
        if decision.choice == CHOICE_SHA:
            new_ref = new_ref[:8]
        key3 = (update.uses_repo, update.current_ref, decision.choice)
        if key3 not in updates_seen:
            updates_seen[key3] = (new_ref, decision.note)

    update_bullets: list[str] = []
    for key3 in sorted(updates_seen, key=lambda k: (k[0].lower(), k[1].lower())):
        uses_repo, current_ref, choice = key3
        new_ref, note = updates_seen[key3]
        kind = _CHOICE_LABELS.get(choice, choice)
        line = f"- {uses_repo}: {current_ref} → {new_ref} ({kind})"
        if note:
            line += f" — {note}"
        update_bullets.append(line)

    skip_bullets: list[str] = []
    for key2 in sorted(skips_seen, key=lambda k: (k[0].lower(), k[1].lower())):
        uses_repo, current_ref = key2
        note = skips_seen[key2]
        skip_bullets.append(f"- {uses_repo}: {current_ref} (kept) — {note}")

    return update_bullets, skip_bullets


_TITLE = "Update external GitHub workflows"


def _format_body(update_bullets: list[str], skip_bullets: list[str]) -> str:
    """Render update + skip bullets into the shared body text."""
    sections: list[str] = []
    if update_bullets:
        sections.append(
            "Updates from `gh-external-audit update`:\n\n"
            + "\n".join(update_bullets)
        )
    if skip_bullets:
        sections.append("Not updated:\n\n" + "\n".join(skip_bullets))
    return "\n\n".join(sections)


def build_commit_message(
    decisions: list[Decision], updates: list[ActionUpdate]
) -> str:
    """Build the commit message body from the applied decisions."""
    update_bullets, skip_bullets = summarize_changes(decisions, updates)
    body = _format_body(update_bullets, skip_bullets)
    if not body:
        return f"{_TITLE}\n"
    return f"{_TITLE}\n\n{body}\n"


def build_pr_title(
    decisions: list[Decision], updates: list[ActionUpdate]
) -> str:
    return _TITLE


def build_pr_body(
    decisions: list[Decision], updates: list[ActionUpdate]
) -> str:
    update_bullets, skip_bullets = summarize_changes(decisions, updates)
    body = _format_body(update_bullets, skip_bullets)
    return body or "(no changes)"


def create_pr(clone_path: Path, title: str, body: str) -> int:
    """Run `gh pr create` with stdio inherited so gh can prompt interactively.

    Returns gh's exit code. gh will offer to fork and push if the user lacks
    write access to the upstream repo.
    """
    result = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body],
        cwd=clone_path,
    )
    return result.returncode
