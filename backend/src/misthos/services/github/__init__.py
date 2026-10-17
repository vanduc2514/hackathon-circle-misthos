"""GitHub, behind the one interface the store reads and writes through.

`GitHubApp` is the only implementation: every read and write goes to GitHub as the
configured App's installation. A deployment without the App credentials refuses to
start rather than pricing work with no repository behind it.
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


def build_github(app_id: str, private_key: str, api_url: str) -> GitHubGateway:
    if not (app_id and private_key):
        raise GitHubNotConfigured(
            "MISTHOS_GITHUB_APP_ID and MISTHOS_GITHUB_APP_PRIVATE_KEY are required: "
            "the platform reads and writes GitHub as its App"
        )
    # Imported here so a test that injects a gateway never loads the HTTP or JWT
    # libraries.
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
    "build_github",
]
