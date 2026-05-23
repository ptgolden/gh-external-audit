from pathlib import Path

import typer
from rich.console import Console
from rich.rule import Rule
from rich.syntax import Syntax

from .models import (
    CHOICE_EXACT,
    CHOICE_MAJOR,
    CHOICE_SHA,
    CHOICE_SKIP,
    ActionUpdate,
    Decision,
)


_console = Console()


STATUS_OUTDATED = "outdated"

_CHOICE_FROM_KEY = {
    "m": CHOICE_MAJOR,
    "e": CHOICE_EXACT,
    "s": CHOICE_SHA,
    "n": CHOICE_SKIP,
}


_HELP_TEXT = """
  (m) pin to major tag:  rewrite to use the moving major-version tag (e.g. v6).
                         Auto-tracks future v6.x.y releases.
  (e) pin to exact tag:  rewrite to use the latest exact release tag
                         (e.g. v6.0.2). Immutable for that release.
  (s) pin to SHA:        rewrite to use the immutable commit SHA. Most secure
                         (no supply-chain surprise from a moving tag).
  (n) leave as is:       keep the current pin unchanged.

  Uppercase variants (M/E/S/N) apply the same choice to this occurrence and
  every remaining WORKFLOW@TAG match in the queue.

  (q) quit:              stop prompting; keep choices made so far.
  (?) help:              this message.
"""


def _find_line(file_path: Path, uses_target: str) -> int | None:
    """Return the first 1-indexed line in file_path where uses_target appears with `uses:`."""
    if not file_path.is_file():
        return None
    for i, line in enumerate(file_path.read_text().splitlines(), start=1):
        if "uses:" in line and uses_target in line:
            return i
    return None


def _print_yaml_context(file_path: Path, target_line: int, padding: int = 5) -> None:
    """Render ±padding lines around target_line as a syntax-highlighted YAML block."""
    lines = file_path.read_text().splitlines()
    start = max(1, target_line - padding)
    end = min(len(lines), target_line + padding)
    snippet = "\n".join(lines[start - 1 : end])
    syntax = Syntax(
        snippet,
        "yaml",
        line_numbers=True,
        start_line=start,
        highlight_lines={target_line},
        theme="ansi_dark",
        background_color="default",
    )
    _console.print(syntax)


def _short_sha(sha: str) -> str:
    return sha[:8] if sha else ""


def _print_prompt(
    update: ActionUpdate,
    clone_path: Path,
    position: int,
    total: int,
    pending_matches: int,
) -> tuple[list[str], bool]:
    """Print the per-action prompt block.

    Returns (valid_keys, major_available) so the input loop can validate.
    """
    workflow_file = clone_path / update.workflow_path
    line_no = _find_line(workflow_file, update.uses_target)
    latest = update.latest_release

    print()
    _console.print(
        Rule(f"[{position}/{total}] {update.uses_target}", style="cyan", align="left")
    )
    file_label = (
        f"{workflow_file}:{line_no}" if line_no else str(workflow_file)
    )
    print(f"  File:   {file_label}")
    print(f"  Action: https://github.com/{update.uses_repo}")
    print()

    if line_no:
        _print_yaml_context(workflow_file, line_no)
        print()

    current_sha_short = _short_sha(update.current_sha) or "(unknown)"
    current_date = update.current_published_at[:10] or "(unknown)"
    latest_sha_short = (
        _short_sha(latest.latest_sha) if latest and latest.latest_sha else "(unknown)"
    )
    latest_date = (
        latest.published_at[:10] if latest and latest.published_at else "(unknown)"
    )
    latest_tag = latest.tag_name if latest else "(no release)"
    major_tag = latest.latest_major_tag if latest else None

    print(f"  Current: {update.current_ref:<15}  {current_sha_short}  {current_date}")
    print(f"  Latest:  {latest_tag:<15}  {latest_sha_short}  {latest_date}")
    if major_tag:
        print(f"  Major:   {major_tag}")
    print()

    print("  Options:")
    if major_tag:
        print(f"    (m) pin to major tag ({major_tag})")
    else:
        print(
            f"    (m) pin to major tag — not available for {update.uses_repo}"
        )
    print(f"    (e) pin to exact tag ({latest_tag})")
    print(f"    (s) pin to SHA ({latest_sha_short})")
    print("    (n) leave as is")
    if pending_matches > 0:
        plural = "" if pending_matches == 1 else "s"
        print()
        print(
            f"    Uppercase (M/E/S/N) applies to this and "
            f"{pending_matches} more occurrence{plural} of {update.uses_target}."
        )
        print()
    print("    (q) quit (keep changes made so far)")
    print("    (?) help")
    print()

    valid = ["m", "e", "s", "n", "q", "?"]
    if pending_matches > 0:
        valid.extend(["M", "E", "S", "N"])
    return valid, bool(major_tag)


def _ask_one(
    update: ActionUpdate,
    clone_path: Path,
    position: int,
    total: int,
    pending_matches: int,
) -> str:
    """Show the prompt and return a valid choice key.

    Return values: lowercase letter (single-target choice), uppercase letter
    (bulk-apply to current + remaining matching), or "q" to quit.
    """
    valid, major_available = _print_prompt(
        update, clone_path, position, total, pending_matches
    )
    while True:
        try:
            raw = input(f"  Choose [{'/'.join(valid)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "q"

        if raw == "?":
            print(_HELP_TEXT)
            continue
        if raw not in valid:
            print(f"  Not a valid choice. Pick one of [{'/'.join(valid)}].")
            continue
        if raw.lower() == "m" and not major_available:
            print(
                f"  ({raw}) is not available: {update.uses_repo} does not "
                "publish a moving major-version tag."
            )
            continue
        return raw


def prompt_for_decisions(
    clone_path: Path,
    updates: list[ActionUpdate],
) -> list[Decision]:
    """Run the interactive prompt loop over outdated updates.

    Skips up-to-date and other non-outdated rows entirely. Returns the
    accumulated decisions, including any partial set if the user quits early.
    """
    outdated = [u for u in updates if u.status == STATUS_OUTDATED]
    if not outdated:
        print("Nothing to update — every external action is already up to date.")
        return []

    plural = "" if len(outdated) == 1 else "s"
    file_count = len({u.workflow_path for u in outdated})
    file_plural = "" if file_count == 1 else "s"
    print(
        f"Found {len(outdated)} outdated action use{plural} across "
        f"{file_count} workflow file{file_plural}."
    )
    if not typer.confirm("Proceed with interactive review?", default=True):
        return []

    decisions: list[Decision] = []
    pending: list[ActionUpdate] = list(outdated)
    completed = 0
    total = len(outdated)

    while pending:
        update = pending.pop(0)
        completed += 1
        pending_matches = sum(
            1 for u in pending if u.uses_target == update.uses_target
        )

        key = _ask_one(
            update,
            clone_path,
            position=completed,
            total=total,
            pending_matches=pending_matches,
        )

        if key == "q":
            print("  quitting; keeping decisions made so far.")
            break

        choice_name = _CHOICE_FROM_KEY[key.lower()]
        decisions.append(
            Decision(
                workflow_path=update.workflow_path,
                uses_target=update.uses_target,
                choice=choice_name,
            )
        )

        if key.isupper():
            # Bulk-apply to all remaining matching uses_target
            applied_to = 1
            remaining: list[ActionUpdate] = []
            for u in pending:
                if u.uses_target == update.uses_target:
                    decisions.append(
                        Decision(
                            workflow_path=u.workflow_path,
                            uses_target=u.uses_target,
                            choice=choice_name,
                        )
                    )
                    applied_to += 1
                else:
                    remaining.append(u)
            pending = remaining
            plural = "" if applied_to == 1 else "s"
            print(
                f"  applied [{choice_name}] to {applied_to} occurrence{plural} "
                f"of {update.uses_target}."
            )

    return decisions
