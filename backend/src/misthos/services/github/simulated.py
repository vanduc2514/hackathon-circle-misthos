"""A GitHub that lives in memory.

It answers reads from fixtures a test or a demo installs, and keeps every write in
order instead of sending it, so what the platform would have posted can be read
back. With no fixture for an issue it returns nothing, and the store falls back to
the signals it was given, which is how the seeded demo stays as it was.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from misthos.domain.signals import IssueFacts
from misthos.services.github.base import GitHubError, PullRequest, ReviewEvent, StatusState


@dataclass(frozen=True)
class Sent:
    """One write the platform made to GitHub."""

    kind: str
    """comment, review or status."""
    repo: str
    target: str
    """The issue or pull request number, or the commit for a status."""
    body: str
    state: str = ""


class SimulatedGitHub:
    name = "simulated"

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.issues: dict[tuple[str, int], IssueFacts] = {}
        self.pulls: dict[tuple[str, int], PullRequest] = {}
        self.checks: dict[tuple[str, str], bool | None] = {}
        self.history: dict[tuple[str, str], int] = {}
        self.sent: list[Sent] = []
        self.fail_writes = False
        """Make every write raise, to prove a failed post never fails the action."""

    def reset(self) -> None:
        with self._guard:
            self.issues, self.pulls, self.checks, self.history = {}, {}, {}, {}
            self.sent = []
            self.fail_writes = False

    # ------------------------------------------------------------- read path

    def read_issue(self, repo: str, number: int) -> IssueFacts | None:
        return self.issues.get((repo.lower(), number))

    def read_pull_request(self, repo: str, number: int) -> PullRequest:
        found = self.pulls.get((repo.lower(), number))
        if found is None:
            raise GitHubError(f"no pull request {repo}#{number}")
        return found

    def checks_passed(self, repo: str, sha: str) -> bool | None:
        return self.checks.get((repo.lower(), sha))

    def merged_pull_requests(self, repo: str, author: str) -> int:
        return self.history.get((repo.lower(), author.lower()), 0)

    # ------------------------------------------------------------ write path

    def comment(self, repo: str, number: int, body: str) -> None:
        self._send(Sent("comment", repo, str(number), body))

    def review(self, repo: str, number: int, head_sha: str, event: ReviewEvent, body: str) -> None:
        self._send(Sent("review", repo, str(number), body, state=event.value))

    def status(self, repo: str, sha: str, state: StatusState, description: str) -> None:
        self._send(Sent("status", repo, sha, description, state=state.value))

    def _send(self, sent: Sent) -> None:
        if self.fail_writes:
            raise GitHubError("GitHub is unavailable")
        with self._guard:
            self.sent.append(sent)

    # --------------------------------------------------------------- fixtures

    def put_issue(self, facts: IssueFacts) -> None:
        self.issues[(facts.repo.lower(), facts.number)] = facts

    def put_pull_request(self, pr: PullRequest) -> None:
        self.pulls[(pr.repo.lower(), pr.number)] = pr

    def put_checks(self, repo: str, sha: str, passed: bool | None) -> None:
        self.checks[(repo.lower(), sha)] = passed
