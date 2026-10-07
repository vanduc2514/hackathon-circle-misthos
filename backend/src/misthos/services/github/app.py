"""The real GitHub, as an App installation.

The platform never acts as a person. It signs a short-lived JWT with the App's
private key, exchanges it for a token scoped to the installation on the repository
it is working in, and makes every read and write with that token. Tokens last an
hour; one is reused until five minutes before it expires.

The permissions the App asks for are the ones this module uses and no more: issues
write (criteria and settlement comments), pull requests write (the review), checks
read (the project's own tests), commit statuses write (our status), contents read
(the repository tree) and metadata read. `backend/github-app-manifest.json` registers
an App with exactly that set.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import jwt

from misthos.domain.review import ChangedFile
from misthos.domain.signals import IssueFacts, classify_tree
from misthos.services.github.base import (
    STATUS_CONTEXT,
    GitHubError,
    PullRequest,
    ReviewEvent,
    StatusState,
)

API = "https://api.github.com"
API_VERSION = "2022-11-28"
# Renew a token this long before GitHub would expire it, so a slow call never
# starts with a token that dies halfway.
TOKEN_MARGIN_SECONDS = 300
# GitHub accepts an App JWT for ten minutes at most, and clocks drift.
JWT_LIFETIME_SECONDS = 540
JWT_BACKDATE_SECONDS = 60

_PASSING = frozenset({"success", "neutral", "skipped"})
# GitHub lists at most 3,000 files of a pull request, 100 to a page.
MAX_FILE_PAGES = 30


@dataclass
class _Token:
    value: str
    expires_at: float


class GitHubApp:
    name = "github-app"

    def __init__(
        self,
        app_id: str,
        private_key: str,
        *,
        api_url: str = API,
        transport: httpx.BaseTransport | None = None,
        clock: Any = time.time,
    ) -> None:
        self._app_id = app_id
        self._key = private_key
        self._clock = clock
        self._http = httpx.Client(
            base_url=api_url,
            timeout=10.0,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "misthos",
            },
        )
        self._guard = threading.Lock()
        self._installations: dict[str, int] = {}
        self._tokens: dict[int, _Token] = {}

    # ------------------------------------------------------------ auth

    def app_jwt(self) -> str:
        now = int(self._clock())
        claims = {
            "iat": now - JWT_BACKDATE_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": self._app_id,
        }
        return jwt.encode(claims, self._key, algorithm="RS256")

    def installation_token(self, repo: str) -> str:
        with self._guard:
            installation = self._installations.get(repo.lower())
        if installation is None:
            found = self._call("GET", f"/repos/{repo}/installation", auth=self._as_app())
            installation = int(found["id"])
            with self._guard:
                self._installations[repo.lower()] = installation

        with self._guard:
            token = self._tokens.get(installation)
            if token and token.expires_at - TOKEN_MARGIN_SECONDS > self._clock():
                return token.value

        issued = self._call(
            "POST", f"/app/installations/{installation}/access_tokens", auth=self._as_app()
        )
        expires = datetime.fromisoformat(issued["expires_at"].replace("Z", "+00:00"))
        token = _Token(value=issued["token"], expires_at=expires.timestamp())
        with self._guard:
            self._tokens[installation] = token
        return token.value

    def _as_app(self) -> str:
        return f"Bearer {self.app_jwt()}"

    def _as_installation(self, repo: str) -> str:
        return f"token {self.installation_token(repo)}"

    def _call(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._http.request(
                method, path, json=json, params=params, headers={"Authorization": auth}
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"{method} {path}: {exc}") from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("message", "")
            except ValueError:
                message = response.text[:200]
            raise GitHubError(f"{method} {path}: {response.status_code} {message}")
        return response.json() if response.content else None

    def _get(self, repo: str, path: str, **params: Any) -> Any:
        return self._call("GET", path, auth=self._as_installation(repo), params=params or None)

    def _post(self, repo: str, path: str, body: dict[str, Any]) -> Any:
        return self._call("POST", path, auth=self._as_installation(repo), json=body)

    # ------------------------------------------------------------- read path

    def read_issue(self, repo: str, number: int) -> IssueFacts:
        issue = self._get(repo, f"/repos/{repo}/issues/{number}")
        if "pull_request" in issue:
            raise GitHubError(f"{repo}#{number} is a pull request, not an issue")
        meta = self._get(repo, f"/repos/{repo}")
        tree = self._get(repo, f"/repos/{repo}/git/trees/{meta['default_branch']}", recursive="1")
        paths = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
        timeline = self._get(repo, f"/repos/{repo}/issues/{number}/timeline", per_page="100")
        return IssueFacts(
            repo=repo,
            number=number,
            title=issue["title"],
            body=issue.get("body") or "",
            labels=tuple(label["name"] for label in issue.get("labels", [])),
            tree=classify_tree(paths),
            prior_attempts=_abandoned_attempts(timeline or []),
        )

    def read_pull_request(self, repo: str, number: int) -> PullRequest:
        return pull_request_from(repo, self._get(repo, f"/repos/{repo}/pulls/{number}"))

    def read_files(self, repo: str, number: int) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            batch = self._get(
                repo, f"/repos/{repo}/pulls/{number}/files", per_page="100", page=str(page)
            )
            files.extend(
                ChangedFile(
                    path=f["filename"],
                    additions=int(f.get("additions", 0)),
                    deletions=int(f.get("deletions", 0)),
                    patch=f.get("patch") or "",
                )
                for f in batch
            )
            if len(batch) < 100:
                break
        return files

    def checks_passed(self, repo: str, sha: str) -> bool | None:
        found = self._get(repo, f"/repos/{repo}/commits/{sha}/check-runs", per_page="100")
        runs = found.get("check_runs", [])
        if not runs or any(r.get("status") != "completed" for r in runs):
            return None
        return all(r.get("conclusion") in _PASSING for r in runs)

    def merged_pull_requests(self, repo: str, author: str) -> int:
        found = self._get(repo, "/search/issues", q=f"repo:{repo} is:pr is:merged author:{author}")
        return int(found.get("total_count", 0))

    # ------------------------------------------------------------ write path

    def comment(self, repo: str, number: int, body: str) -> None:
        self._post(repo, f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def review(self, repo: str, number: int, head_sha: str, event: ReviewEvent, body: str) -> None:
        self._post(
            repo,
            f"/repos/{repo}/pulls/{number}/reviews",
            {"commit_id": head_sha, "event": event.value, "body": body},
        )

    def status(self, repo: str, sha: str, state: StatusState, description: str) -> None:
        self._post(
            repo,
            f"/repos/{repo}/statuses/{sha}",
            # GitHub truncates nothing for us: a description over 140 characters fails.
            {"state": state.value, "context": STATUS_CONTEXT, "description": description[:140]},
        )


def pull_request_from(repo: str, pr: dict[str, Any]) -> PullRequest:
    """A pull request from GitHub's own representation, in a webhook or the API."""
    return PullRequest(
        repo=repo,
        number=int(pr["number"]),
        author=pr["user"]["login"],
        head_sha=pr["head"]["sha"],
        body=pr.get("body") or "",
        merged=bool(pr.get("merged")),
        files_changed=int(pr.get("changed_files", 0)),
        additions=int(pr.get("additions", 0)),
        deletions=int(pr.get("deletions", 0)),
        draft=bool(pr.get("draft")),
    )


def _abandoned_attempts(timeline: list[dict[str, Any]]) -> int:
    """Pull requests that referenced the issue and were closed without merging."""
    seen: set[int] = set()
    for event in timeline:
        if event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        pr = source.get("pull_request")
        if not pr or source.get("state") != "closed" or pr.get("merged_at"):
            continue
        seen.add(int(source["number"]))
    return len(seen)
