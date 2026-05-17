from __future__ import annotations

import csv
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

from loguru import logger
from tqdm import tqdm
import typer
import yaml


app = typer.Typer(
    add_completion=False,
    help="Scan GitHub Actions workflow files and report actions using old Node runtimes.",
)

SCAN_COLUMNS = [
    "repo",
    "repo_updated_at",
    "repo_pushed_at",
    "workflow_path",
    "uses_target",
    "uses_repo",
    "uses_path",
    "ref",
]

PROBLEM_REPORT_COLUMNS = [
    *SCAN_COLUMNS,
    "metadata_path",
    "problem",
    "detail",
]


@dataclass(frozen=True)
class Repo:
    name_with_owner: str
    updated_at: str
    pushed_at: str


@dataclass(frozen=True)
class WorkflowFile:
    path: str


@dataclass(frozen=True)
class UseRecord:
    repo: str
    repo_updated_at: str
    repo_pushed_at: str
    workflow_path: str
    uses_target: str
    uses_repo: str
    uses_path: str
    ref: str


@dataclass(frozen=True)
class ActionKey:
    uses_repo: str
    uses_path: str
    ref: str


@dataclass(frozen=True)
class ProblemRecord:
    code: str
    detail: str = ""


@dataclass(frozen=True)
class ActionMetadataRecord:
    uses_repo: str
    uses_path: str
    ref: str
    metadata_path: str
    metadata_found: bool
    problems: tuple[ProblemRecord, ...]


class GitHubClient:
    """Small wrapper around `gh api` that counts API subprocess calls."""

    def __init__(self) -> None:
        """Create a client with a zeroed call counter."""
        self.api_call_count = 0

    def api(
        self,
        endpoint: str,
        *args: str,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run `gh api` and return the completed process."""
        self.api_call_count += 1
        command = ["gh", "api", endpoint, *args]
        return subprocess.run(
            command,
            check=check,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=60,
        )

    def api_json_lines(self, endpoint: str, *args: str) -> Iterable[Any]:
        """Run `gh api --jq ... @json` and yield one decoded JSON object per line."""
        result = self.api(endpoint, *args)
        for line in result.stdout.splitlines():
            if line:
                yield json.loads(line)

    def api_text(self, endpoint: str, *args: str) -> str:
        """Run `gh api` and return stdout as text."""
        return self.api(endpoint, *args).stdout


def require_gh() -> None:
    """Fail early if the GitHub CLI is not available."""
    try:
        subprocess.run(
            ["gh", "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        raise typer.BadParameter("missing required command: gh") from None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise typer.BadParameter(f"could not run gh: {exc}") from exc


def setup_logging(progress: bool, log_level: str) -> None:
    """Configure loguru for stderr progress logging."""
    logger.remove()
    if progress:
        logger.add(sys.stderr, level=log_level.upper(), colorize=True)
    else:
        logger.disable(__name__)


def list_repos(client: GitHubClient, org: str, limit: int) -> list[Repo]:
    """List repositories in an organization with basic update timestamps."""
    repos: list[Repo] = []
    page = 1

    while len(repos) < limit:
        per_page = min(100, limit - len(repos))
        page_items = list(
            client.api_json_lines(
                f"/orgs/{org}/repos",
                "--method",
                "GET",
                "-f",
                "type=all",
                "-f",
                "sort=full_name",
                "-f",
                "direction=asc",
                "-f",
                f"per_page={per_page}",
                "-f",
                f"page={page}",
                "--jq",
                ".[] | {name, owner: .owner.login, updated_at, pushed_at} | @json",
            )
        )

        if not page_items:
            break

        for item in page_items:
            repos.append(
                Repo(
                    name_with_owner=f"{item['owner']}/{item['name']}",
                    updated_at=item.get("updated_at", ""),
                    pushed_at=item.get("pushed_at", ""),
                )
            )
        page += 1

    return repos


def list_workflow_files(client: GitHubClient, repo: Repo) -> list[WorkflowFile]:
    """Return YAML workflow files under `.github/workflows` for a repository."""
    result = client.api(
        f"/repos/{repo.name_with_owner}/contents/.github/workflows",
        "--jq",
        (
            '.[] | select(.type == "file") '
            '| select(.name | test("\\\\.ya?ml$")) '
            "| {path} | @json"
        ),
        check=False,
    )
    if result.returncode != 0:
        return []

    return [
        WorkflowFile(path=item["path"])
        for item in (json.loads(line) for line in result.stdout.splitlines() if line)
    ]


def get_workflow_text(
    client: GitHubClient, repo: Repo, workflow_file: WorkflowFile
) -> str:
    """Fetch raw workflow YAML text from GitHub."""
    return client.api_text(
        f"/repos/{repo.name_with_owner}/contents/{workflow_file.path}",
        "-H",
        "Accept: application/vnd.github.raw",
    )


def iter_uses(value: Any) -> Iterable[str]:
    """Yield every string value found under a `uses` key in nested YAML data."""
    if isinstance(value, dict):
        uses = value.get("uses")
        if isinstance(uses, str):
            yield uses
        for child in value.values():
            yield from iter_uses(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_uses(child)


def ref_from_uses(uses_target: str) -> str:
    """Extract the ref after the final `@` in a GitHub Actions uses target."""
    if "@" not in uses_target:
        return ""
    return uses_target.rsplit("@", 1)[1]


def action_repo_and_path_from_uses(uses_target: str) -> tuple[str, str]:
    """Split a uses target into `owner/repo` and optional action subdirectory."""
    target_without_ref = uses_target.rsplit("@", 1)[0]
    parts = target_without_ref.split("/")
    if len(parts) < 2:
        return target_without_ref, ""

    action_repo = "/".join(parts[:2])
    action_path = "/".join(parts[2:])
    return action_repo, action_path


def should_skip(uses_target: str, org: str) -> bool:
    """Return true for local actions and same-org actions that are out of scope."""
    if uses_target.startswith(("./", "../")):
        return True

    owner = uses_target.split("/", 1)[0].lower()
    return owner == org.lower()


def parse_workflow(
    text: str, repo: Repo, workflow_file: WorkflowFile, org: str
) -> Iterable[UseRecord]:
    """Parse one workflow file and yield external action uses records."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning(
            "could not parse {}:{}: {}",
            repo.name_with_owner,
            workflow_file.path,
            exc,
        )
        return

    for uses_target in iter_uses(parsed):
        if should_skip(uses_target, org):
            continue
        uses_repo, uses_path = action_repo_and_path_from_uses(uses_target)
        yield UseRecord(
            repo=repo.name_with_owner,
            repo_updated_at=repo.updated_at,
            repo_pushed_at=repo.pushed_at,
            workflow_path=workflow_file.path,
            uses_target=uses_target,
            uses_repo=uses_repo,
            uses_path=uses_path,
            ref=ref_from_uses(uses_target),
        )


def scan(
    client: GitHubClient,
    org: str,
    repo_limit: int,
    progress: bool,
) -> Iterable[UseRecord]:
    """Scan repositories and workflow files for external action uses."""
    if progress:
        logger.info("listing repositories for {}", org)
    repos = list_repos(client, org, repo_limit)
    if progress:
        logger.info("found {} repositories", len(repos))

    progress_bar: tqdm[Repo] | None = None
    repo_iterable: Iterable[Repo]
    if progress:
        progress_bar = tqdm(
            repos,
            desc="repositories",
            unit="repo",
            file=sys.stderr,
            dynamic_ncols=True,
        )
        progress_bar.set_postfix_str(f"gh api calls={client.api_call_count}")
        repo_iterable = progress_bar
    else:
        repo_iterable = repos

    for repo in repo_iterable:
        if progress_bar:
            progress_bar.set_postfix_str(f"gh api calls={client.api_call_count}")

        if progress:
            logger.debug("listing workflow files for {}", repo.name_with_owner)
        workflow_files = list_workflow_files(client, repo)
        if progress_bar:
            progress_bar.set_postfix_str(f"gh api calls={client.api_call_count}")

        if progress and workflow_files:
            logger.debug(
                "found {} workflow files in {}",
                len(workflow_files),
                repo.name_with_owner,
            )

        for workflow_file in workflow_files:
            if progress:
                logger.debug(
                    "downloading {}:{}",
                    repo.name_with_owner,
                    workflow_file.path,
                )

            text = get_workflow_text(client, repo, workflow_file)
            if progress_bar:
                progress_bar.set_postfix_str(f"gh api calls={client.api_call_count}")
            yield from parse_workflow(text, repo, workflow_file, org)


def dedupe_scan_records(records: Iterable[UseRecord]) -> list[UseRecord]:
    """Remove exact duplicate workflow-use rows while preserving first-seen order."""
    seen: set[tuple[str, str, str, str, str, str, str, str]] = set()
    deduped: list[UseRecord] = []
    for record in records:
        row = (
            record.repo,
            record.repo_updated_at,
            record.repo_pushed_at,
            record.workflow_path,
            record.uses_target,
            record.uses_repo,
            record.uses_path,
            record.ref,
        )
        if row in seen:
            continue
        seen.add(row)
        deduped.append(record)
    return deduped


def action_keys_from_records(records: Iterable[UseRecord]) -> list[ActionKey]:
    """Return sorted unique action metadata lookup keys from use records."""
    keys = {
        ActionKey(
            uses_repo=record.uses_repo,
            uses_path=record.uses_path,
            ref=record.ref,
        )
        for record in records
        if record.uses_repo and record.ref
    }
    return sorted(keys, key=lambda key: (key.uses_repo.lower(), key.uses_path, key.ref))


def scan_record_to_row(record: UseRecord) -> dict[str, str]:
    """Convert a workflow use record into a TSV row mapping."""
    return {
        "repo": record.repo,
        "repo_updated_at": record.repo_updated_at,
        "repo_pushed_at": record.repo_pushed_at,
        "workflow_path": record.workflow_path,
        "uses_target": record.uses_target,
        "uses_repo": record.uses_repo,
        "uses_path": record.uses_path,
        "ref": record.ref,
    }


def metadata_records_by_key(
    records: Iterable[ActionMetadataRecord],
) -> dict[ActionKey, ActionMetadataRecord]:
    """Index action metadata records by their action repo/path/ref key."""
    return {
        ActionKey(
            uses_repo=record.uses_repo,
            uses_path=record.uses_path,
            ref=record.ref,
        ): record
        for record in records
    }


def write_problem_report_from_records(
    scan_records: Iterable[UseRecord],
    metadata_by_key: dict[ActionKey, ActionMetadataRecord],
    include_header: bool,
) -> None:
    """Write one TSV row per (workflow use × action problem)."""
    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=PROBLEM_REPORT_COLUMNS,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="ignore",
    )
    if include_header:
        writer.writeheader()

    for record in scan_records:
        key = ActionKey(
            uses_repo=record.uses_repo,
            uses_path=record.uses_path,
            ref=record.ref,
        )
        metadata = metadata_by_key.get(key)
        if not metadata or not metadata.problems:
            continue

        for problem in metadata.problems:
            writer.writerow(
                {
                    **scan_record_to_row(record),
                    "metadata_path": metadata.metadata_path,
                    "problem": problem.code,
                    "detail": problem.detail,
                }
            )


def action_metadata_paths(key: ActionKey) -> list[str]:
    """Return possible action metadata paths for a root or subdirectory action."""
    base = key.uses_path.rstrip("/")
    if base:
        return [f"{base}/action.yml", f"{base}/action.yaml"]
    return ["action.yml", "action.yaml"]


def fetch_action_metadata(
    client: GitHubClient, key: ActionKey
) -> tuple[str, dict[str, Any] | None, str]:
    """Fetch and parse action.yml/action.yaml at a specific action ref."""
    quoted_ref = quote(key.ref, safe="")
    for metadata_path in action_metadata_paths(key):
        result = client.api(
            f"/repos/{key.uses_repo}/contents/{metadata_path}?ref={quoted_ref}",
            "-H",
            "Accept: application/vnd.github.raw",
            check=False,
        )
        if result.returncode != 0:
            continue

        try:
            parsed = yaml.safe_load(result.stdout)
        except yaml.YAMLError as exc:
            return metadata_path, None, f"metadata_parse_error:{exc}"

        if isinstance(parsed, dict):
            return metadata_path, parsed, ""
        return metadata_path, None, "metadata_not_mapping"

    return "", None, "metadata_missing"


def check_node_runtime(metadata: dict[str, Any]) -> Iterable[ProblemRecord]:
    """Flag JavaScript actions running on Node older than version 24."""
    runs = metadata.get("runs")
    if not isinstance(runs, dict):
        return
    using = runs.get("using")
    if not isinstance(using, str):
        return
    runtime = using.lower()
    if runtime.startswith("node"):
        version = runtime.removeprefix("node")
        if version.isdigit() and int(version) < 24:
            yield ProblemRecord(code="node_lt_24", detail=using)


ACTION_CHECKS = [check_node_runtime]


def audit_action(metadata: dict[str, Any]) -> Iterable[ProblemRecord]:
    """Run every action-level check on parsed action metadata."""
    for check in ACTION_CHECKS:
        yield from check(metadata)


def inspect_action(
    client: GitHubClient,
    key: ActionKey,
) -> ActionMetadataRecord:
    """Inspect one unique action ref and report any problems found."""
    metadata_path, metadata, fetch_problem = fetch_action_metadata(client, key)
    problems: list[ProblemRecord] = []
    if fetch_problem:
        problems.append(ProblemRecord(code=fetch_problem))
    if metadata:
        problems.extend(audit_action(metadata))

    return ActionMetadataRecord(
        uses_repo=key.uses_repo,
        uses_path=key.uses_path,
        ref=key.ref,
        metadata_path=metadata_path,
        metadata_found=metadata is not None,
        problems=tuple(problems),
    )


def inspect_actions(
    client: GitHubClient,
    keys: list[ActionKey],
    progress: bool,
) -> Iterable[ActionMetadataRecord]:
    """Inspect many unique action refs, optionally with a progress bar."""
    if progress:
        logger.info("inspecting {} unique action refs", len(keys))

    progress_bar: tqdm[ActionKey] | None = None
    key_iterable: Iterable[ActionKey]
    if progress:
        progress_bar = tqdm(
            keys,
            desc="actions",
            unit="action",
            file=sys.stderr,
            dynamic_ncols=True,
        )
        progress_bar.set_postfix_str(f"gh api calls={client.api_call_count}")
        key_iterable = progress_bar
    else:
        key_iterable = keys

    for key in key_iterable:
        if progress_bar:
            progress_bar.set_postfix_str(f"gh api calls={client.api_call_count}")
        logger.debug(
            "inspecting {}{}@{}",
            key.uses_repo,
            f"/{key.uses_path}" if key.uses_path else "",
            key.ref,
        )
        record = inspect_action(client, key)
        if progress_bar:
            progress_bar.set_postfix_str(f"gh api calls={client.api_call_count}")
        yield record


@app.command()
def main(
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
    """Run the complete audit and write a TSV problem report to stdout."""
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


if __name__ == "__main__":
    app()
