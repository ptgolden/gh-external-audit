from __future__ import annotations

import sys
from typing import Any, Iterable
from urllib.parse import quote

import yaml
from loguru import logger
from tqdm import tqdm

from .checks import audit_action
from .github import GitHubClient
from .models import ActionKey, ActionMetadataRecord, ProblemRecord, UseRecord


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
