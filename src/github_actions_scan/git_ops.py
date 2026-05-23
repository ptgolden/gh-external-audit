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
) -> list[str]:
    """Build a deduplicated bullet list summarizing the applied decisions.

    One bullet per (uses_repo, current_ref, choice) tuple. Skip decisions are
    omitted. SHA targets are abbreviated to 8 characters.
    """
    update_map = {(u.workflow_path, u.uses_target): u for u in updates}
    seen: dict[tuple[str, str, str], str] = {}
    order: list[tuple[str, str, str]] = []

    for decision in decisions:
        if decision.choice == CHOICE_SKIP:
            continue
        update = update_map.get((decision.workflow_path, decision.uses_target))
        if update is None:
            continue
        new_target = target_for_choice(update, decision.choice)
        if new_target is None:
            continue
        new_ref = new_target.rsplit("@", 1)[1]
        if decision.choice == CHOICE_SHA:
            new_ref = new_ref[:8]
        key = (update.uses_repo, update.current_ref, decision.choice)
        if key not in seen:
            seen[key] = new_ref
            order.append(key)

    bullets = []
    for key in order:
        uses_repo, current_ref, choice = key
        new_ref = seen[key]
        kind = _CHOICE_LABELS.get(choice, choice)
        bullets.append(f"- {uses_repo}: {current_ref} → {new_ref} ({kind})")
    return bullets


def build_commit_message(
    decisions: list[Decision], updates: list[ActionUpdate]
) -> str:
    """Build the commit message body from the applied decisions."""
    bullets = summarize_changes(decisions, updates)
    if not bullets:
        return "Update GitHub Actions\n"
    return "Update GitHub Actions\n\n" + "\n".join(bullets) + "\n"
