import json
import re

from .github import GitHubClient
from .models import LatestRelease


_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_MAJOR_ONLY_RE = re.compile(r"^v?(\d+)$")


def fetch_tag_exists(client: GitHubClient, uses_repo: str, tag: str) -> bool:
    """Return True if a tag of the given name exists in the repo."""
    result = client.api(
        f"/repos/{uses_repo}/git/ref/tags/{tag}",
        "-H",
        "Accept: application/vnd.github+json",
        check=False,
    )
    return result.returncode == 0


def fetch_commit_info(client: GitHubClient, uses_repo: str, ref: str) -> tuple[str, str]:
    """Resolve a ref (tag/branch/sha) to its (commit_sha, committer_date).

    Returns ("", "") on 404 or malformed response. The commits endpoint
    transparently follows annotated tags.
    """
    result = client.api(
        f"/repos/{uses_repo}/commits/{ref}",
        "-H",
        "Accept: application/vnd.github+json",
        "--jq",
        "{sha, date: .commit.committer.date}",
        check=False,
    )
    if result.returncode != 0:
        return "", ""

    try:
        data = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return "", ""

    if not isinstance(data, dict):
        return "", ""

    return data.get("sha") or "", data.get("date") or ""


def fetch_version_tags(client: GitHubClient, uses_repo: str) -> list[tuple[str, str]]:
    """Return every tag in the repo as (name, commit_sha) pairs.

    Uses `--paginate` so all tags come back in one call from the caller's
    perspective. For repos with hundreds of tags this is a few API calls;
    for typical action repos it's one.
    """
    result = client.api(
        f"/repos/{uses_repo}/tags",
        "--paginate",
        "--jq",
        ".[] | {name, sha: .commit.sha}",
        check=False,
    )
    if result.returncode != 0:
        return []

    tags: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = obj.get("name") or ""
        sha = obj.get("sha") or ""
        if name and sha:
            tags.append((name, sha))
    return tags


def find_pinnable_tags(
    tags: list[tuple[str, str]],
) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """Find the highest semver tag and its matching major-only tag.

    Returns (exact_tag, major_tag), each as (name, sha) or None.

    The major_tag is only returned when there's a `vMAJOR` tag whose major
    matches the highest semver tag's major — so repos that don't publish
    moving major-version tags (e.g. `astral-sh/setup-uv`, which stopped on
    purpose) correctly return major=None instead of pointing at an older
    major like `v7`.
    """
    if not tags:
        return None, None

    semvers: list[tuple[tuple[int, int, int], str, str]] = []
    majors_by_int: dict[int, tuple[str, str]] = {}

    for name, sha in tags:
        semver_match = _SEMVER_RE.match(name)
        if semver_match:
            major, minor, patch = (
                int(semver_match.group(1)),
                int(semver_match.group(2)),
                int(semver_match.group(3)),
            )
            semvers.append(((major, minor, patch), name, sha))
            continue
        major_only_match = _MAJOR_ONLY_RE.match(name)
        if major_only_match:
            majors_by_int[int(major_only_match.group(1))] = (name, sha)

    if not semvers:
        return None, None

    semvers.sort(reverse=True)
    _, exact_name, exact_sha = semvers[0]
    top_major = semvers[0][0][0]

    major_pair = majors_by_int.get(top_major)
    return (exact_name, exact_sha), major_pair


def fetch_latest_release(client: GitHubClient, uses_repo: str) -> LatestRelease | None:
    """Determine the latest pinnable version for a GitHub Action repo.

    The pinnable `tag_name` comes from a scan of `/tags`, picking the
    highest `v?MAJOR.MINOR.PATCH`-shaped tag. This handles repos whose
    GitHub Releases use a prefixed scheme (e.g. `github/codeql-action`'s
    `codeql-bundle-vX.Y.Z`) where the release's `tag_name` isn't what you
    actually want to pin to.

    `/releases/latest` is still consulted for human-readable metadata
    (release name, html_url, published_at when the release's tag IS the
    pinnable tag). Returns None if there's neither a usable release nor a
    semver tag.
    """
    release_result = client.api(
        f"/repos/{uses_repo}/releases/latest",
        "-H",
        "Accept: application/vnd.github+json",
        check=False,
    )
    release_data: dict | None = None
    if release_result.returncode == 0:
        try:
            parsed = json.loads(release_result.stdout)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("tag_name"):
            release_data = parsed

    version_tags = fetch_version_tags(client, uses_repo)
    exact_pair, major_pair = find_pinnable_tags(version_tags)

    if exact_pair is not None:
        tag_name, latest_sha = exact_pair
    elif release_data is not None:
        tag_name = release_data["tag_name"]
        latest_sha, _ = fetch_commit_info(client, uses_repo, tag_name)
    else:
        return None

    release_tag = release_data["tag_name"] if release_data else None
    if release_data and tag_name == release_tag:
        published_at = release_data.get("published_at") or ""
    else:
        _, published_at = fetch_commit_info(client, uses_repo, tag_name)

    name = (release_data.get("name") if release_data else "") or ""
    html_url = (release_data.get("html_url") if release_data else "") or ""
    latest_major_tag = major_pair[0] if major_pair else None

    return LatestRelease(
        tag_name=tag_name,
        name=name,
        published_at=published_at,
        html_url=html_url,
        latest_major_tag=latest_major_tag,
        latest_sha=latest_sha,
    )
