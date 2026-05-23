import subprocess
from pathlib import Path

from loguru import logger

from .models import (
    CHOICE_EXACT,
    CHOICE_MAJOR,
    CHOICE_SHA,
    CHOICE_SKIP,
    ActionUpdate,
    Decision,
)


def target_for_choice(update: ActionUpdate, choice: str) -> str | None:
    """Construct the new `uses_target` string a choice would produce.

    Returns None for `skip`, for unknown choices, or when the requested pin
    isn't available (e.g. `major` chosen but no moving major tag exists).
    """
    if choice == CHOICE_SKIP:
        return None

    latest = update.latest_release
    if latest is None:
        return None

    if choice == CHOICE_MAJOR:
        new_ref = latest.latest_major_tag
    elif choice == CHOICE_EXACT:
        new_ref = latest.tag_name
    elif choice == CHOICE_SHA:
        new_ref = latest.latest_sha
    else:
        return None

    if not new_ref:
        return None

    base = update.uses_target.rsplit("@", 1)[0]
    return f"{base}@{new_ref}"


def rewrite_workflow(file_path: Path, old_target: str, new_target: str) -> int:
    """Rewrite `uses: <old_target>` to `uses: <new_target>` on each matching line.

    Matches lines that contain both `uses:` and the exact `old_target` substring.
    Preserves surrounding formatting (whitespace, quoting, trailing comments) by
    using a literal string replacement rather than YAML round-tripping.

    Returns the number of lines actually edited.
    """
    text = file_path.read_text()
    lines = text.splitlines(keepends=True)
    edits = 0
    for i, line in enumerate(lines):
        if "uses:" in line and old_target in line:
            lines[i] = line.replace(old_target, new_target)
            edits += 1
    if edits > 0:
        file_path.write_text("".join(lines))
    return edits


def apply_decisions(
    clone_path: Path,
    decisions: list[Decision],
    updates: list[ActionUpdate],
) -> int:
    """Apply decisions to the workflow files inside `clone_path`.

    Decisions are matched to ActionUpdates by (workflow_path, uses_target).
    Unknown matches and skip/no-op decisions are silently ignored (with a
    debug log). Returns the total number of lines edited across all files.
    """
    update_map = {
        (update.workflow_path, update.uses_target): update for update in updates
    }

    total_edits = 0
    for decision in decisions:
        update = update_map.get((decision.workflow_path, decision.uses_target))
        if update is None:
            logger.warning(
                "decision references unknown action: {} {}",
                decision.workflow_path,
                decision.uses_target,
            )
            continue

        new_target = target_for_choice(update, decision.choice)
        if new_target is None:
            logger.debug(
                "no edit applied for {} ({}@{}, choice={})",
                decision.workflow_path,
                update.uses_repo,
                update.current_ref,
                decision.choice,
            )
            continue

        if new_target == decision.uses_target:
            continue

        workflow_file = clone_path / decision.workflow_path
        edits = rewrite_workflow(workflow_file, decision.uses_target, new_target)
        if edits > 0:
            total_edits += edits
            logger.info(
                "{}: {} -> {} ({} line{})",
                decision.workflow_path,
                decision.uses_target,
                new_target,
                edits,
                "" if edits == 1 else "s",
            )

    return total_edits


def diff(clone_path: Path, paths: list[str] | None = None) -> str:
    """Return the git diff of pending changes (defaults to `.github/workflows/`)."""
    target = paths if paths is not None else [".github/workflows"]
    result = subprocess.run(
        ["git", "-C", str(clone_path), "diff", "--", *target],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout
