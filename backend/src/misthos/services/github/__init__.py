"""GitHub, behind the one interface the store reads and writes through.

`GitHubApp` is the real thing: set `MISTHOS_GITHUB_APP_ID` and
`MISTHOS_GITHUB_APP_PRIVATE_KEY` and every read and write goes to GitHub as
that App's installation.
"""

from __future__ import annotations

from misthos.services.github.base import (
    STATUS_CONTEXT,
    GitHubError,
    GitHubGateway,
    GitHubNotConfigured,
    Installation,
    PullRequest,
    ReviewEvent,
    StatusState,
)
from misthos.services.github.unconfigured import UnconfiguredGitHub


def build_github(app_id: str, private_key: str, api_url: str) -> GitHubGateway:
    """The App when it is configured, an unconfigured placeholder otherwise."""
    if not (app_id and private_key):
        return UnconfiguredGitHub()
    # Imported here so the zero-config path never loads the HTTP or JWT libraries.
    from misthos.services.github.app import GitHubApp

    # Environment variables cannot hold newlines everywhere, so accept escaped ones.
    return GitHubApp(app_id, private_key.replace("\\n", "\n"), api_url=api_url)


__all__ = [
    "STATUS_CONTEXT",
    "GitHubError",
    "GitHubGateway",
    "GitHubNotConfigured",
    "Installation",
    "PullRequest",
    "ReviewEvent",
    "StatusState",
    "UnconfiguredGitHub",
    "build_github",
]
