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


class GitHubNotConfigured(RuntimeError):
    """A live App was configured without what its deliveries need to be trusted."""


@dataclass(frozen=True)
class Installation:
    """One installation of the App, as GitHub reports it.

    `login` is the account the App is installed on, which is what attributes the
    installation's repositories to a publisher. `repositories` is every repository it
    covers, so an installation with the whole account selected lists them all.
    """

    id: int
    login: str
    repositories: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenIssue:
    """One open issue, as the picker shows it (#199)."""

    repo: str
    number: int
    title: str
    labels: tuple[str, ...] = ()
    url: str = ""


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
    private: bool = False
    """The repository is private. Read by the App's installation token all the same."""


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

    # --------------------------------------------------------- installations

    def list_installations(self) -> list[Installation]:
        """Every installation of the App, with the repositories each one covers.

        Empty when the gateway cannot know.
        """

    def installation_for(self, repo: str) -> Installation | None:
        """The installation that covers one repository, or None when the App is not on
        it. One call, for the paths that ask about a single repository."""

    # ------------------------------------------------------------- read path

    def read_issue(self, repo: str, number: int) -> IssueFacts | None:
        """The issue and what its repository says about the work, or None when this
        gateway has nothing to read."""

    def open_issues(self, repo: str, limit: int = 100) -> list[OpenIssue]:
        """The repository's open issues, without its pull requests, newest first.

        GitHub's issues endpoint returns both, so the picker is built from the same
        place a publisher would look. A cap keeps one request bounded.
        """

    def read_pull_request(self, repo: str, number: int) -> PullRequest: ...

    def read_files(self, repo: str, number: int) -> list[ChangedFile]:
        """The files a pull request changes, with their patches."""

    def checks_passed(self, repo: str, sha: str) -> bool | None:
        """Whether every check on the commit passed. None while any is still running."""

    def merged_pull_requests(self, repo: str, author: str) -> int:
        """How many of `author`'s pull requests this repository has merged."""

    # ------------------------------------------------------------ write path

    creates_issues: bool
    """Whether publishing may open an issue. Only a gateway acting as the App can;
    a fake used by a test opens none there."""

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        """Open an issue and return the number GitHub gave it.

        A publisher who is describing work GitHub does not hold yet gets an issue
        rather than a made-up number, so what is priced, reviewed and paid is an issue
        that exists and can be linked to.
        """

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        """Give an issue these labels, creating one that does not exist yet.

        Linking an existing issue is what puts the platform label on it, so the
        label-driven flow prices the same issue the web app listed (#199).
        """

    def comment(self, repo: str, number: int, body: str) -> None: ...

    def review(
        self, repo: str, number: int, head_sha: str, event: ReviewEvent, body: str
    ) -> None: ...

    def status(self, repo: str, sha: str, state: StatusState, description: str) -> None: ...
