import json
import re

from .github import GitHubClient
from .models import LatestRelease


def major_tag(tag_name: str) -> str | None:
    """Extract the moving major-version tag implied by a release tag.

    e.g. `v6.0.2` -> `v6`, `6.0.2` -> `6`, `release-1.0` -> None.
    Returns None when no leading numeric component is present.
    """
    match = re.match(r"^(v?\d+)", tag_name)
    return match.group(1) if match else None


def fetch_tag_exists(client: GitHubClient, uses_repo: str, tag: str) -> bool:
    """Return True if a tag of the given name exists in the repo."""
    result = client.api(
        f"/repos/{uses_repo}/git/ref/tags/{tag}",
        "-H",
        "Accept: application/vnd.github+json",
        check=False,
    )
    return result.returncode == 0


def fetch_commit_sha(client: GitHubClient, uses_repo: str, ref: str) -> str:
    """Resolve a ref (tag/branch/sha) to its underlying commit SHA, or "" on failure.

    Uses the commits endpoint, which transparently follows annotated tags.
    """
    result = client.api(
        f"/repos/{uses_repo}/commits/{ref}",
        "-H",
        "Accept: application/vnd.github+json",
        "--jq",
        ".sha",
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def fetch_latest_release(client: GitHubClient, uses_repo: str) -> LatestRelease | None:
    """Fetch the latest non-prerelease, non-draft release for `owner/repo`.

    Also probes for the moving major-version tag (e.g. `v6` alongside `v6.0.2`)
    since pinning to a major-version tag is the GitHub Actions convention.

    Returns None if the repository has no releases (404) or the response is unparseable.
    """
    result = client.api(
        f"/repos/{uses_repo}/releases/latest",
        "-H",
        "Accept: application/vnd.github+json",
        check=False,
    )
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or not data.get("tag_name"):
        return None

    tag_name = data.get("tag_name") or ""
    major = major_tag(tag_name)
    if major and major != tag_name and fetch_tag_exists(client, uses_repo, major):
        latest_major_tag: str | None = major
    else:
        latest_major_tag = None

    latest_sha = fetch_commit_sha(client, uses_repo, tag_name)

    return LatestRelease(
        tag_name=tag_name,
        name=data.get("name") or "",
        published_at=data.get("published_at") or "",
        html_url=data.get("html_url") or "",
        latest_major_tag=latest_major_tag,
        latest_sha=latest_sha,
    )
