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

UPDATE_REPORT_COLUMNS = [
    "workflow_path",
    "uses_target",
    "uses_repo",
    "uses_path",
    "current_ref",
    "current_sha",
    "current_published_at",
    "latest_tag",
    "latest_major_tag",
    "latest_sha",
    "latest_published_at",
    "latest_url",
    "status",
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


@dataclass(frozen=True)
class LatestRelease:
    tag_name: str
    name: str
    published_at: str
    html_url: str
    latest_major_tag: str | None = None
    latest_sha: str = ""


@dataclass(frozen=True)
class ActionUpdate:
    workflow_path: str
    uses_target: str
    uses_repo: str
    uses_path: str
    current_ref: str
    current_sha: str
    current_published_at: str
    latest_release: LatestRelease | None
    status: str


CHOICE_MAJOR = "major"
CHOICE_EXACT = "exact"
CHOICE_SHA = "sha"
CHOICE_SKIP = "skip"

CHOICES = (CHOICE_MAJOR, CHOICE_EXACT, CHOICE_SHA, CHOICE_SKIP)


@dataclass(frozen=True)
class Decision:
    workflow_path: str
    uses_target: str
    choice: str
    note: str = ""
