from __future__ import annotations

import json
import subprocess
from typing import Any, Iterable

import typer


class GitHubClient:
    """Small wrapper around `gh api` that counts API subprocess calls."""

    def __init__(self) -> None:
        """Create a client with a zeroed call counter."""
        self.api_call_count = 0

    def api(
        self,
        endpoint: str,
        *args: str,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run `gh api` and return the completed process."""
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
        """Run `gh api --jq ... @json` and yield one decoded JSON object per line."""
        result = self.api(endpoint, *args)
        for line in result.stdout.splitlines():
            if line:
                yield json.loads(line)

    def api_text(self, endpoint: str, *args: str) -> str:
        """Run `gh api` and return stdout as text."""
        return self.api(endpoint, *args).stdout


def require_gh() -> None:
    """Fail early if the GitHub CLI is not available."""
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
