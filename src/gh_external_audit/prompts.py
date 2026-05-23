import re
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


def _indent_of(line: str) -> int:
    """Return the number of leading spaces on a line (tab is treated as 0)."""
    return len(line) - len(line.lstrip(" "))


def _dash_col(line: str) -> int | None:
    """Return the column of `- ` if the line is a YAML list-item header, else None."""
    stripped = line.lstrip(" ")
    if not stripped.startswith("- "):
        return None
    return len(line) - len(stripped)


def _find_step_bounds(
    lines: list[str], target_line: int
) -> tuple[int, int, int] | None:
    """Return 1-indexed (start, end, dash_col) for the YAML step containing target_line.

    Uses pure indent heuristics: walk upward to find the most recent `- `
    ancestor whose dash sits at less indent than the target, then walk
    downward until the next sibling `- ` at the same dash position or any
    line at less-or-equal indent ends the block. Returns None if the
    structure can't be inferred (e.g. tab-indented YAML).
    """
    if not lines:
        return None
    target_idx = target_line - 1
    if not (0 <= target_idx < len(lines)):
        return None

    target_indent = _indent_of(lines[target_idx])

    target_dash = _dash_col(lines[target_idx])
    if target_dash is not None:
        step_start_idx = target_idx
        step_dash_col = target_dash
    else:
        step_start_idx = -1
        step_dash_col = -1
        for i in range(target_idx - 1, -1, -1):
            line = lines[i]
            if not line.strip() or line.lstrip(" ").startswith("#"):
                continue
            line_indent = _indent_of(line)
            dash = _dash_col(line)
            if dash is not None and dash < target_indent:
                step_start_idx = i
                step_dash_col = dash
                break
            if line_indent < target_indent:
                return None
        if step_start_idx < 0:
            return None

    step_end_idx = len(lines) - 1
    for i in range(step_start_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip(" ").startswith("#"):
            continue
        line_indent = _indent_of(line)
        dash = _dash_col(line)
        if dash is not None and dash == step_dash_col:
            step_end_idx = i - 1
            break
        if line_indent <= step_dash_col:
            step_end_idx = i - 1
            break

    return step_start_idx + 1, step_end_idx + 1, step_dash_col


_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _step_name(
    lines: list[str], step_start: int, step_end: int, dash_col: int
) -> str | None:
    """Return the value of the step's `name:` key if present."""
    content_indent = dash_col + 2
    for i in range(step_start - 1, step_end):
        line = lines[i]
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        # The step header line, e.g. `- name: Step name`
        if i == step_start - 1 and stripped.startswith("- "):
            after_dash = stripped[2:]
            match = _NAME_RE.match(after_dash)
            if match:
                return _unquote(match.group(1))
            continue
        # A body line of the step at content_indent
        if _indent_of(line) == content_indent:
            match = _NAME_RE.match(stripped)
            if match:
                return _unquote(match.group(1))
    return None


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

    if line_no:
        text = workflow_file.read_text()
        lines = text.splitlines()
        bounds = _find_step_bounds(lines, line_no)
        if bounds is None:
            start = max(1, line_no - 5)
            end = min(len(lines), line_no + 5)
            step_name = None
        else:
            start, end, dash_col = bounds
            step_name = _step_name(lines, start, end, dash_col)

        if step_name:
            print(f"  Step:   {step_name}")
        print()

        snippet = "\n".join(lines[start - 1 : end])
        syntax = Syntax(
            snippet,
            "yaml",
            line_numbers=True,
            start_line=start,
            highlight_lines={line_no},
            theme="ansi_dark",
            background_color="default",
        )
        _console.print(syntax)
        print()
    else:
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
