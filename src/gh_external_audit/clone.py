import re
import shutil
import subprocess
from pathlib import Path

import typer
from loguru import logger


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


def sparse_clone(owner_repo: str, target: Path) -> None:
    """Sparse-clone OWNER/REPO into `target`, materializing only `.github/`."""
    url = f"https://github.com/{owner_repo}.git"
    target.parent.mkdir(parents=True, exist_ok=True)
    logger.info("cloning {} into {}", owner_repo, target)
    _run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            "--sparse",
            url,
            str(target),
        ]
    )
    _run(["git", "sparse-checkout", "set", ".github"], cwd=target)
    _run(["git", "checkout"], cwd=target)


def ensure_clone(
    owner_repo: str,
    working_dir: Path,
    force_reclone: bool,
) -> Path:
    """Return the path to a local sparse clone of OWNER/REPO, creating one if needed.

    Reuses any existing clone at `working_dir/OWNER/REPO/`. Pass
    `force_reclone=True` to delete and re-clone from scratch.
    """
    target = working_dir / owner_repo
    if target.exists():
        if force_reclone:
            logger.info("removing existing clone at {} (--force-reclone)", target)
            shutil.rmtree(target)
        else:
            logger.info("reusing existing clone at {}", target)
            return target
    sparse_clone(owner_repo, target)
    return target


_GITHUB_URL_RE = re.compile(
    r"""
    ^(?:
        (?:https?://github\.com/)
      | (?:git@github\.com:)
    )
    (?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$
    """,
    re.VERBOSE,
)


def resolve_here(cwd: Path) -> str:
    """Return the OWNER/REPO inferred from cwd's `origin` remote.

    Raises typer.BadParameter when cwd has no `origin` remote, isn't a git
    repo, or `origin` points to a non-GitHub URL we can't parse.
    """
    result = _run(
        ["git", "-C", str(cwd), "remote", "get-url", "origin"],
        check=False,
    )
    if result.returncode != 0:
        raise typer.BadParameter(
            f"{cwd} has no `origin` remote (or is not a git repo); "
            "cannot infer OWNER/REPO from --here mode"
        )

    url = result.stdout.strip()
    match = _GITHUB_URL_RE.match(url)
    if not match:
        raise typer.BadParameter(
            f"could not parse OWNER/REPO from origin URL {url!r}"
        )
    return f"{match['owner']}/{match['repo']}"


def working_tree_is_dirty(cwd: Path) -> bool:
    """Return True if `cwd`'s working tree has uncommitted changes."""
    result = _run(
        ["git", "-C", str(cwd), "status", "--porcelain"],
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())
