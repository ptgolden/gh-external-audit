import json
import sys
from typing import Any, Iterable

import yaml
from loguru import logger
from tqdm import tqdm

from .github import GitHubClient
from .models import Repo, UseRecord, WorkflowFile


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
