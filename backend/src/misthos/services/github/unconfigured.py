"""Fallback GitHub gateway when no GitHub App is configured."""

from __future__ import annotations

from misthos.domain.review import ChangedFile
from misthos.domain.signals import IssueFacts
from misthos.services.github.base import (
    GitHubNotConfigured,
    Installation,
    PullRequest,
    ReviewEvent,
    StatusState,
)


class UnconfiguredGitHub:
    name = "unconfigured"
    creates_issues = False

    def list_installations(self) -> list[Installation]:
        return []

    def installation_for(self, repo: str) -> Installation | None:
        return None

    def read_issue(self, repo: str, number: int) -> IssueFacts | None:
        return None

    def read_pull_request(self, repo: str, number: int) -> PullRequest:
        raise GitHubNotConfigured("GitHub App credentials are not configured")

    def read_files(self, repo: str, number: int) -> list[ChangedFile]:
        raise GitHubNotConfigured("GitHub App credentials are not configured")

    def checks_passed(self, repo: str, sha: str) -> bool | None:
        raise GitHubNotConfigured("GitHub App credentials are not configured")

    def merged_pull_requests(self, repo: str, author: str) -> int:
        return 0

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        raise GitHubNotConfigured("GitHub App credentials are not configured")

    def comment(self, repo: str, number: int, body: str) -> None:
        pass

    def review(self, repo: str, number: int, head_sha: str, event: ReviewEvent, body: str) -> None:
        pass

    def status(self, repo: str, sha: str, state: StatusState, description: str) -> None:
        pass
