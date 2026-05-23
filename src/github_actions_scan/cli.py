import sys
from pathlib import Path
from typing import Optional

import typer
from loguru import logger

from .actions import action_keys_from_records, inspect_actions, metadata_records_by_key
from .clone import ensure_clone, resolve_here, working_tree_is_dirty
from .editor import apply_decisions, diff, load_decisions
from .github import GitHubClient, require_gh
from .models import Repo
from .prompts import prompt_for_decisions
from .report import write_problem_report_from_records
from .scan import dedupe_scan_records, scan, scan_cloned_workflows, scan_repo_workflows
from .update import find_action_updates, write_update_report


app = typer.Typer(
    add_completion=False,
    help=(
        "Scan GitHub Actions workflow files. "
        "Use `org` to audit every repo in an organization, "
        "`repo` to find action updates for a single repository, "
        "or `update` to interactively update a repository's workflows."
    ),
)


def setup_logging(progress: bool, log_level: str) -> None:
    """Configure loguru for stderr progress logging."""
    logger.remove()
    if progress:
        logger.add(sys.stderr, level=log_level.upper(), colorize=True)
    else:
        logger.disable("github_actions_scan")


@app.command(name="org")
def org_command(
    org: str = typer.Argument(..., help="GitHub organization to scan."),
    repo_limit: int = typer.Option(
        1000,
        "--repo-limit",
        envvar="REPO_LIMIT",
        help="Maximum number of repositories to scan.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the planned audit configuration without calling GitHub.",
    ),
    progress: bool = typer.Option(
        True,
        "--progress/--no-progress",
        help="Write progress logs to stderr.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Log level for stderr progress logs.",
    ),
    header: bool = typer.Option(
        True,
        "--header/--no-header",
        help="Include a TSV header row.",
    ),
) -> None:
    """Audit every repo in an org and write a TSV problem report to stdout."""
    if dry_run:
        typer.echo(f"organization: {org}")
        typer.echo(f"repo_limit: {repo_limit}")
        typer.echo(f"progress: {progress}")
        typer.echo(f"log_level: {log_level.upper()}")
        typer.echo("planned steps:")
        typer.echo("  scan workflows")
        typer.echo("  deduplicate action refs")
        typer.echo("  inspect action metadata")
        typer.echo("  write problem report")
        return

    setup_logging(progress, log_level)
    require_gh()
    client = GitHubClient()
    try:
        scan_records = dedupe_scan_records(scan(client, org, repo_limit, progress))
        keys = action_keys_from_records(scan_records)
        if progress:
            logger.info(
                "found {} unique action refs across {} uses rows",
                len(keys),
                len(scan_records),
            )
        metadata_records = list(inspect_actions(client, keys, progress))
        metadata_by_key = metadata_records_by_key(metadata_records)
        write_problem_report_from_records(
            scan_records,
            metadata_by_key,
            include_header=header,
        )
    finally:
        if progress:
            logger.info("gh api calls: {}", client.api_call_count)


@app.command(name="repo")
def repo_command(
    repo: str = typer.Argument(
        ..., help="Target repository as OWNER/REPO (e.g. actions/checkout)."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the planned configuration without calling GitHub.",
    ),
    progress: bool = typer.Option(
        True,
        "--progress/--no-progress",
        help="Write progress logs to stderr.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Log level for stderr progress logs.",
    ),
    header: bool = typer.Option(
        True,
        "--header/--no-header",
        help="Include a TSV header row.",
    ),
) -> None:
    """Find available updates for every external action used in REPO's workflows."""
    if "/" not in repo or repo.count("/") != 1 or not all(repo.split("/")):
        raise typer.BadParameter("repo must be in OWNER/REPO format")

    if dry_run:
        typer.echo(f"repository: {repo}")
        typer.echo(f"progress: {progress}")
        typer.echo(f"log_level: {log_level.upper()}")
        typer.echo("planned steps:")
        typer.echo("  scan workflows")
        typer.echo("  deduplicate action refs")
        typer.echo("  fetch latest release per action repo")
        typer.echo("  resolve current ref commit info per (repo, ref)")
        typer.echo("  write update report")
        return

    setup_logging(progress, log_level)
    require_gh()
    client = GitHubClient()
    try:
        repo_obj = Repo(name_with_owner=repo, updated_at="", pushed_at="")
        records = scan_repo_workflows(client, repo_obj)
        updates = find_action_updates(client, records, progress)
        if progress:
            logger.info("computed {} action update rows", len(updates))
        write_update_report(updates, include_header=header)
    finally:
        if progress:
            logger.info("gh api calls: {}", client.api_call_count)


def _validate_owner_repo(value: str) -> str:
    if "/" not in value or value.count("/") != 1 or not all(value.split("/")):
        raise typer.BadParameter("repo must be in OWNER/REPO format")
    return value


@app.command(name="update")
def update_command(
    repo: Optional[str] = typer.Argument(
        None,
        help=(
            "Target repository as OWNER/REPO. Required unless --here is set; "
            "with --here, inferred from cwd's `origin` remote if omitted."
        ),
    ),
    here: bool = typer.Option(
        False,
        "--here",
        help="Operate on cwd instead of sparse-cloning. Infers OWNER/REPO from `origin`.",
    ),
    work_dir: Path = typer.Option(
        Path("working"),
        "--work-dir",
        help="Directory under which sparse clones live. Ignored with --here.",
    ),
    force_reclone: bool = typer.Option(
        False,
        "--force-reclone",
        help="Delete any existing clone before fetching. Ignored with --here.",
    ),
    emit: bool = typer.Option(
        False,
        "--emit",
        help=(
            "Print the action-update TSV (same shape as `repo`) and exit. "
            "Use this to feed an agent that will produce a decisions file."
        ),
    ),
    decisions_file: Optional[Path] = typer.Option(
        None,
        "--decisions",
        help=(
            "Path to a JSON file of pre-computed Decision records. "
            "When set, applies them non-interactively and shows the diff."
        ),
    ),
    header: bool = typer.Option(
        True,
        "--header/--no-header",
        help="Include a TSV header row in --emit output.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the planned configuration without calling GitHub or editing files.",
    ),
    progress: bool = typer.Option(
        True,
        "--progress/--no-progress",
        help="Write progress logs to stderr.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Log level for stderr progress logs.",
    ),
) -> None:
    """Update GitHub Actions used in REPO's workflows.

    Three modes:
    - default: walk every outdated action with `git add -p`-style prompts,
      apply choices to the workflow files, print the diff.
    - --emit: print the action-update TSV (same shape as `repo`) and exit;
      meant to feed an agent that produces a decisions file.
    - --decisions FILE: apply pre-computed decisions from a JSON file
      non-interactively, then print the diff.

    Sparse-clones the target into `./working/OWNER/REPO/`, or uses cwd with
    --here.
    """
    if emit and decisions_file is not None:
        raise typer.BadParameter("--emit and --decisions are mutually exclusive")

    setup_logging(progress, log_level)

    cwd = Path.cwd()
    if here:
        resolved_repo = repo if repo else resolve_here(cwd)
        _validate_owner_repo(resolved_repo)
        clone_path = cwd
        if working_tree_is_dirty(clone_path):
            logger.warning(
                "{} has uncommitted changes; proceeding anyway (--here)", cwd
            )
    else:
        if repo is None:
            raise typer.BadParameter("OWNER/REPO required (or pass --here)")
        resolved_repo = _validate_owner_repo(repo)
        if dry_run:
            clone_path = work_dir / resolved_repo
        else:
            clone_path = ensure_clone(resolved_repo, work_dir, force_reclone)

    if emit:
        mode = "emit"
    elif decisions_file is not None:
        mode = "apply-decisions"
    else:
        mode = "interactive"

    if dry_run:
        typer.echo(f"repo: {resolved_repo}")
        typer.echo(f"clone_path: {clone_path}")
        typer.echo(f"here: {here}")
        typer.echo(f"force_reclone: {force_reclone}")
        typer.echo(f"mode: {mode}")
        if mode == "apply-decisions":
            typer.echo(f"decisions_file: {decisions_file}")
        typer.echo("planned steps:")
        typer.echo("  ensure clone (or use cwd)")
        typer.echo("  scan local workflows")
        typer.echo("  fetch latest release per action repo")
        typer.echo("  resolve current ref commit info per (repo, ref)")
        if mode == "emit":
            typer.echo("  write TSV to stdout")
        elif mode == "apply-decisions":
            typer.echo("  load decisions from file")
            typer.echo("  apply decisions to workflow files")
            typer.echo("  show diff")
        else:
            typer.echo("  prompt interactively for each outdated action")
            typer.echo("  apply decisions to workflow files")
            typer.echo("  show diff")
        return

    require_gh()
    client = GitHubClient()
    try:
        records = scan_cloned_workflows(clone_path, resolved_repo)
        updates = find_action_updates(client, records, progress)
        if progress:
            logger.info("computed {} action update rows", len(updates))

        if mode == "emit":
            write_update_report(updates, include_header=header)
            return

        if mode == "apply-decisions":
            assert decisions_file is not None
            decisions = load_decisions(decisions_file)
            logger.info("loaded {} decisions from {}", len(decisions), decisions_file)
        else:
            decisions = prompt_for_decisions(clone_path, updates)
            if not decisions:
                logger.info("no decisions made; nothing to apply")
                return

        edits = apply_decisions(clone_path, decisions, updates)
        if edits == 0:
            logger.info("no edits applied")
            return

        print()
        print(diff(clone_path))
    finally:
        if progress:
            logger.info("gh api calls: {}", client.api_call_count)
