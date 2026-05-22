from dataclasses import dataclass


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
