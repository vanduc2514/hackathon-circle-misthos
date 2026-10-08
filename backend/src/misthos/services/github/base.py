"""The GitHub protocol the store reads and writes through."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from misthos.domain.review import ChangedFile
from misthos.domain.signals import IssueFacts

# The context our commit status appears under on a pull request.
STATUS_CONTEXT = "misthos/review"


class GitHubError(Exception):
    """GitHub refused or failed a call."""


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    author: str
    head_sha: str
    body: str
    merged: bool
    files_changed: int
    additions: int
    deletions: int
    draft: bool = False


class ReviewEvent(StrEnum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


class StatusState(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"


class GitHubGateway(Protocol):
    name: str

    # ------------------------------------------------------------- read path

    def read_issue(self, repo: str, number: int) -> IssueFacts | None:
        """The issue and what its repository says about the work, or None when this
        gateway has nothing to read (the simulation without a fixture)."""

    def read_pull_request(self, repo: str, number: int) -> PullRequest: ...

    def read_files(self, repo: str, number: int) -> list[ChangedFile]:
        """The files a pull request changes, with their patches."""

    def checks_passed(self, repo: str, sha: str) -> bool | None:
        """Whether every check on the commit passed. None while any is still running."""

    def merged_pull_requests(self, repo: str, author: str) -> int:
        """How many of `author`'s pull requests this repository has merged."""

    # ------------------------------------------------------------ write path

    def comment(self, repo: str, number: int, body: str) -> None: ...

    def review(
        self, repo: str, number: int, head_sha: str, event: ReviewEvent, body: str
    ) -> None: ...

    def status(self, repo: str, sha: str, state: StatusState, description: str) -> None: ...
