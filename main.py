from __future__ import annotations

import csv
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable

from loguru import logger
from tqdm import tqdm
import typer
import yaml


app = typer.Typer(
    add_completion=False,
    help="Scan GitHub Actions workflow files for external uses targets.",
)


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


class GitHubClient:
    def __init__(self) -> None:
        self.api_call_count = 0

    def api(
        self,
        endpoint: str,
        *args: str,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
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
        result = self.api(endpoint, *args)
        for line in result.stdout.splitlines():
            if line:
                yield json.loads(line)

    def api_text(self, endpoint: str, *args: str) -> str:
        return self.api(endpoint, *args).stdout


def require_gh() -> None:
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


def list_repos(client: GitHubClient, org: str, limit: int) -> list[Repo]:
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
                '.[] | {name, owner: .owner.login, updated_at, pushed_at} | @json',
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


def get_workflow_text(client: GitHubClient, repo: Repo, workflow_file: WorkflowFile) -> str:
    return client.api_text(
        f"/repos/{repo.name_with_owner}/contents/{workflow_file.path}",
        "-H",
        "Accept: application/vnd.github.raw",
    )


def iter_uses(value: Any) -> Iterable[str]:
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
    if "@" not in uses_target:
        return ""
    return uses_target.rsplit("@", 1)[1]


def action_repo_and_path_from_uses(uses_target: str) -> tuple[str, str]:
    target_without_ref = uses_target.rsplit("@", 1)[0]
    parts = target_without_ref.split("/")
    if len(parts) < 2:
        return target_without_ref, ""

    action_repo = "/".join(parts[:2])
    action_path = "/".join(parts[2:])
    return action_repo, action_path


def should_skip(uses_target: str, org: str) -> bool:
    if uses_target.startswith(("./", "../")):
        return True

    owner = uses_target.split("/", 1)[0].lower()
    return owner == org.lower()


def parse_workflow(
    text: str, repo: Repo, workflow_file: WorkflowFile, org: str
) -> Iterable[UseRecord]:
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
    if progress:
        logger.info("listing repositories for {}", org)
    repos = list_repos(client, org, repo_limit)
    if progress:
        logger.info("found {} repositories", len(repos))

    repo_iterable: Iterable[Repo]
    if progress:
        repo_iterable = tqdm(
            repos,
            desc="repositories",
            unit="repo",
            file=sys.stderr,
            dynamic_ncols=True,
        )
    else:
        repo_iterable = repos

    for repo in repo_iterable:
        if progress:
            logger.debug("listing workflow files for {}", repo.name_with_owner)
        workflow_files = list_workflow_files(client, repo)
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
            yield from parse_workflow(text, repo, workflow_file, org)


def write_tsv(records: Iterable[UseRecord], include_header: bool) -> None:
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    if include_header:
        writer.writerow(
            [
                "repo",
                "repo_updated_at",
                "repo_pushed_at",
                "workflow_path",
                "uses_target",
                "uses_repo",
                "uses_path",
                "ref",
            ]
        )

    seen: set[tuple[str, str, str, str, str, str, str, str]] = set()
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
        writer.writerow(row)


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
        help="Print the planned scan configuration without calling GitHub.",
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
        False,
        "--header/--no-header",
        help="Include a TSV header row.",
    ),
) -> None:
    if dry_run:
        typer.echo(f"organization: {org}")
        typer.echo(f"repo_limit: {repo_limit}")
        typer.echo(f"progress: {progress}")
        typer.echo(f"log_level: {log_level.upper()}")
        typer.echo("planned endpoints:")
        typer.echo(f"  GET /orgs/{org}/repos")
        typer.echo("  GET /repos/{owner}/{repo}/contents/.github/workflows")
        typer.echo("  GET /repos/{owner}/{repo}/contents/{workflow_path}")
        return

    logger.remove()
    if progress:
        logger.add(sys.stderr, level=log_level.upper(), colorize=True)
    else:
        logger.disable(__name__)

    require_gh()
    client = GitHubClient()
    try:
        records = scan(client, org, repo_limit, progress)
        write_tsv(records, include_header=header)
    finally:
        if progress:
            logger.info("gh api calls: {}", client.api_call_count)


if __name__ == "__main__":
    app()
