import sys

import typer
from loguru import logger

from .actions import action_keys_from_records, inspect_actions, metadata_records_by_key
from .github import GitHubClient, require_gh
from .models import Repo
from .report import write_problem_report_from_records
from .scan import dedupe_scan_records, scan
from .update import find_action_updates, write_update_report


app = typer.Typer(
    add_completion=False,
    help=(
        "Scan GitHub Actions workflow files. "
        "Use `org` to audit every repo in an organization, "
        "or `repo` to find action updates for a single repository."
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
        updates = find_action_updates(client, repo_obj, progress)
        if progress:
            logger.info("computed {} action update rows", len(updates))
        write_update_report(updates, include_header=header)
    finally:
        if progress:
            logger.info("gh api calls: {}", client.api_call_count)
