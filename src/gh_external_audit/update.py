import csv
import sys
from typing import Iterable

from loguru import logger
from tqdm import tqdm

from .github import GitHubClient
from .models import (
    UPDATE_REPORT_COLUMNS,
    ActionUpdate,
    LatestRelease,
    UseRecord,
)
from .releases import fetch_commit_info, fetch_latest_release
from .scan import dedupe_scan_records


STATUS_UP_TO_DATE = "up_to_date"
STATUS_OUTDATED = "outdated"
STATUS_NO_RELEASE = "no_release"
STATUS_REF_UNKNOWN = "ref_unknown"


def is_hex_ref(ref: str) -> bool:
    """Return True for a ref that looks like a (possibly abbreviated) commit SHA."""
    return len(ref) >= 7 and all(c in "0123456789abcdef" for c in ref.lower())


def compute_status(
    current_ref: str, current_sha: str, latest: LatestRelease | None
) -> str:
    """Classify an action use as up-to-date, outdated, missing-release, or unknown.

    A current ref counts as up-to-date if it matches any of:
    - the exact release tag (e.g. `v6.0.2`)
    - the moving major-version tag (e.g. `v6`)
    - the commit SHA the latest release points to (catches moving minor tags
      like `v6.0` and any other ref that resolves to the same commit)
    - a hex prefix of the latest commit SHA (handles abbreviated SHA pins)
    """
    if not current_ref:
        return STATUS_REF_UNKNOWN
    if latest is None:
        return STATUS_NO_RELEASE
    if current_ref == latest.tag_name:
        return STATUS_UP_TO_DATE
    if latest.latest_major_tag and current_ref == latest.latest_major_tag:
        return STATUS_UP_TO_DATE
    if current_sha and latest.latest_sha and current_sha == latest.latest_sha:
        return STATUS_UP_TO_DATE
    if (
        latest.latest_sha
        and is_hex_ref(current_ref)
        and latest.latest_sha.lower().startswith(current_ref.lower())
    ):
        return STATUS_UP_TO_DATE
    return STATUS_OUTDATED


def find_action_updates(
    client: GitHubClient,
    records: Iterable[UseRecord],
    progress: bool,
) -> list[ActionUpdate]:
    """Look up release + current-pin info per action for a stream of use records.

    Caches the latest-release lookup per unique action repo and the current
    pin's commit info per unique (repo, ref) pair. The caller is responsible
    for producing the records iterable from whatever source (remote scan,
    local clone, etc.).
    """
    deduped = dedupe_scan_records(records)

    unique_repos = sorted({record.uses_repo for record in deduped if record.uses_repo})
    unique_refs = sorted(
        {(record.uses_repo, record.ref) for record in deduped if record.uses_repo and record.ref}
    )
    if progress:
        logger.info(
            "found {} workflow uses; {} unique action repos; {} unique pinned refs",
            len(deduped),
            len(unique_repos),
            len(unique_refs),
        )

    latest_by_repo: dict[str, LatestRelease | None] = {}
    if progress:
        latest_bar: tqdm[str] | None = tqdm(
            unique_repos,
            desc="releases",
            unit="repo",
            file=sys.stderr,
            dynamic_ncols=True,
        )
        latest_bar.set_postfix_str(f"gh api calls={client.api_call_count}")
        latest_iterable: Iterable[str] = latest_bar
    else:
        latest_bar = None
        latest_iterable = unique_repos

    for uses_repo in latest_iterable:
        logger.debug("fetching latest release for {}", uses_repo)
        latest_by_repo[uses_repo] = fetch_latest_release(client, uses_repo)
        if latest_bar:
            latest_bar.set_postfix_str(f"gh api calls={client.api_call_count}")

    current_by_ref: dict[tuple[str, str], tuple[str, str]] = {}
    if progress:
        current_bar: tqdm[tuple[str, str]] | None = tqdm(
            unique_refs,
            desc="current refs",
            unit="ref",
            file=sys.stderr,
            dynamic_ncols=True,
        )
        current_bar.set_postfix_str(f"gh api calls={client.api_call_count}")
        current_iterable: Iterable[tuple[str, str]] = current_bar
    else:
        current_bar = None
        current_iterable = unique_refs

    for uses_repo, ref in current_iterable:
        logger.debug("resolving current ref {}@{}", uses_repo, ref)
        current_by_ref[(uses_repo, ref)] = fetch_commit_info(client, uses_repo, ref)
        if current_bar:
            current_bar.set_postfix_str(f"gh api calls={client.api_call_count}")

    updates: list[ActionUpdate] = []
    for record in deduped:
        current_sha, current_date = current_by_ref.get(
            (record.uses_repo, record.ref), ("", "")
        )
        latest = latest_by_repo.get(record.uses_repo)
        updates.append(
            ActionUpdate(
                workflow_path=record.workflow_path,
                uses_target=record.uses_target,
                uses_repo=record.uses_repo,
                uses_path=record.uses_path,
                current_ref=record.ref,
                current_sha=current_sha,
                current_published_at=current_date,
                latest_release=latest,
                status=compute_status(record.ref, current_sha, latest),
            )
        )
    return updates


def write_update_report(updates: Iterable[ActionUpdate], include_header: bool) -> None:
    """Write one TSV row per workflow action use, including latest-release info."""
    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=UPDATE_REPORT_COLUMNS,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="ignore",
    )
    if include_header:
        writer.writeheader()

    for update in updates:
        latest = update.latest_release
        writer.writerow(
            {
                "workflow_path": update.workflow_path,
                "uses_target": update.uses_target,
                "uses_repo": update.uses_repo,
                "uses_path": update.uses_path,
                "current_ref": update.current_ref,
                "current_sha": update.current_sha,
                "current_published_at": update.current_published_at,
                "latest_tag": latest.tag_name if latest else "",
                "latest_major_tag": (latest.latest_major_tag if latest else "") or "",
                "latest_sha": latest.latest_sha if latest else "",
                "latest_published_at": latest.published_at if latest else "",
                "latest_url": latest.html_url if latest else "",
                "status": update.status,
            }
        )
