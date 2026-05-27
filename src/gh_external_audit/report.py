import csv
import sys
from typing import Iterable

from .models import (
    PROBLEM_REPORT_COLUMNS,
    ActionKey,
    ActionMetadataRecord,
    UseRecord,
)


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
