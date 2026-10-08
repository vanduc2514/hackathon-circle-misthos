"""The GitHub App client against a recorded GitHub: authentication, the read path
and the write path, with every request checked. Nothing leaves the process."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from misthos.services.github import (
    STATUS_CONTEXT,
    GitHubError,
    ReviewEvent,
    SimulatedGitHub,
    StatusState,
    build_github,
)
from misthos.services.github.app import GitHubApp

REPO = "globex/parse-locale"
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
IN_AN_HOUR = "2027-01-15T09:00:00Z"


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


class FakeGitHub:
    """Answers the routes the client uses and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.tokens_issued = 0
        self.token_expiry = IN_AN_HOUR
        self.on("GET", f"/repos/{REPO}/installation", {"id": 42})
        self.routes[("POST", "/app/installations/42/access_tokens")] = self._token

    def on(self, method: str, path: str, body: Any, status: int = 200) -> None:
        self.routes[(method, path)] = lambda _: httpx.Response(status, json=body)

    def _token(self, _: httpx.Request) -> httpx.Response:
        self.tokens_issued += 1
        return httpx.Response(
            201, json={"token": f"ghs_{self.tokens_issued}", "expires_at": self.token_expiry}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return handler(request)

    def last(self, method: str, path: str) -> httpx.Request:
        return next(r for r in reversed(self.requests) if (r.method, r.url.path) == (method, path))


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def app(github: FakeGitHub, pem: str) -> GitHubApp:
    return GitHubApp("123456", pem, transport=httpx.MockTransport(github), clock=lambda: NOW)


class TestAuthentication:
    def test_the_app_signs_a_short_lived_jwt(self, app: GitHubApp, key: rsa.RSAPrivateKey) -> None:
        claims = jwt.decode(
            app.app_jwt(),
            key.public_key(),
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["iss"] == "123456"
        assert claims["exp"] - claims["iat"] <= 600

    def test_calls_go_out_as_the_installation_not_as_a_person(
        self, app: GitHubApp, github: FakeGitHub
    ) -> None:
        github.on("POST", f"/repos/{REPO}/issues/87/comments", {"id": 1}, status=201)
        app.comment(REPO, 87, "hello")

        lookup = github.last("GET", f"/repos/{REPO}/installation")
        assert lookup.headers["Authorization"].startswith("Bearer ")
        posted = github.last("POST", f"/repos/{REPO}/issues/87/comments")
        assert posted.headers["Authorization"] == "token ghs_1"
        assert posted.headers["X-GitHub-Api-Version"] == "2022-11-28"

    def test_a_token_is_reused_until_it_nears_expiry(self, github: FakeGitHub, pem: str) -> None:
        clock = {"now": NOW}
        expiry = NOW + 3600
        app = GitHubApp("1", pem, transport=httpx.MockTransport(github), clock=lambda: clock["now"])
        github.on("POST", f"/repos/{REPO}/issues/87/comments", {"id": 1}, status=201)

        app.comment(REPO, 87, "one")
        app.comment(REPO, 87, "two")
        assert github.tokens_issued == 1

        clock["now"] = expiry - 60  # inside the renewal margin
        app.comment(REPO, 87, "three")
        assert github.tokens_issued == 2
        assert sum(1 for r in github.requests if r.url.path.endswith("/installation")) == 1

    def test_a_refusal_is_an_error_with_githubs_reason(
        self, app: GitHubApp, github: FakeGitHub
    ) -> None:
        github.on("POST", f"/repos/{REPO}/issues/87/comments", {"message": "Forbidden"}, status=403)
        with pytest.raises(GitHubError, match="403 Forbidden"):
            app.comment(REPO, 87, "hello")


class TestReadPath:
    def test_an_issue_is_read_with_its_repository_and_history(
        self, app: GitHubApp, github: FakeGitHub
    ) -> None:
        github.on(
            "GET",
            f"/repos/{REPO}/issues/87",
            {"title": "Locales", "body": "Fix `src/a.py`", "labels": [{"name": "bug"}]},
        )
        github.on("GET", f"/repos/{REPO}", {"default_branch": "trunk"})
        github.on(
            "GET",
            f"/repos/{REPO}/git/trees/trunk",
            {
                "tree": [
                    {"path": "src", "type": "tree"},
                    {"path": "src/a.py", "type": "blob"},
                    {"path": "src/b.py", "type": "blob"},
                    {"path": "tests/test_a.py", "type": "blob"},
                    {"path": "pyproject.toml", "type": "blob"},
                    {"path": ".github/workflows/ci.yml", "type": "blob"},
                ]
            },
        )
        abandoned = {"number": 90, "state": "closed", "pull_request": {"merged_at": None}}
        merged = {"number": 91, "state": "closed", "pull_request": {"merged_at": "2026-01-01"}}
        github.on(
            "GET",
            f"/repos/{REPO}/issues/87/timeline",
            [
                {"event": "cross-referenced", "source": {"issue": abandoned}},
                {"event": "cross-referenced", "source": {"issue": abandoned}},
                {"event": "cross-referenced", "source": {"issue": merged}},
                {"event": "cross-referenced", "source": {"issue": {"number": 5}}},
                {"event": "labeled"},
            ],
        )

        facts = app.read_issue(REPO, 87)

        assert facts.title == "Locales" and facts.labels == ("bug",)
        assert (facts.tree.source_files, facts.tree.test_files) == (2, 1)
        assert facts.tree.has_ci and facts.tree.dependency_manifests == 1
        assert facts.prior_attempts == 1
        tree = github.last("GET", f"/repos/{REPO}/git/trees/trunk")
        assert tree.url.params["recursive"] == "1"

    def test_a_pull_request_is_not_an_issue(self, app: GitHubApp, github: FakeGitHub) -> None:
        github.on("GET", f"/repos/{REPO}/issues/88", {"title": "x", "pull_request": {}})
        with pytest.raises(GitHubError, match="is a pull request"):
            app.read_issue(REPO, 88)

    def test_a_pull_request_is_read(self, app: GitHubApp, github: FakeGitHub) -> None:
        github.on(
            "GET",
            f"/repos/{REPO}/pulls/501",
            {
                "number": 501,
                "user": {"login": "amara.dev"},
                "head": {"sha": "abc123"},
                "body": "Fixes #87",
                "merged": False,
                "changed_files": 3,
                "additions": 40,
                "deletions": 2,
            },
        )
        pr = app.read_pull_request(REPO, 501)
        assert (pr.author, pr.head_sha, pr.files_changed) == ("amara.dev", "abc123", 3)

    @pytest.mark.parametrize(
        ("runs", "passed"),
        [
            ([], None),
            ([{"status": "in_progress", "conclusion": None}], None),
            ([{"status": "completed", "conclusion": "success"},
              {"status": "completed", "conclusion": "skipped"}], True),
            ([{"status": "completed", "conclusion": "success"},
              {"status": "completed", "conclusion": "failure"}], False),
        ],
    )  # fmt: skip
    def test_checks_pass_only_when_every_run_finished_well(
        self, app: GitHubApp, github: FakeGitHub, runs: list[dict], passed: bool | None
    ) -> None:
        github.on("GET", f"/repos/{REPO}/commits/abc/check-runs", {"check_runs": runs})
        assert app.checks_passed(REPO, "abc") is passed

    def test_a_contributors_merged_history_in_the_repository(
        self, app: GitHubApp, github: FakeGitHub
    ) -> None:
        github.on("GET", "/search/issues", {"total_count": 4})
        assert app.merged_pull_requests(REPO, "amara.dev") == 4
        query = github.last("GET", "/search/issues").url.params["q"]
        assert query == f"repo:{REPO} is:pr is:merged author:amara.dev"


class TestWritePath:
    def test_a_review_is_pinned_to_the_reviewed_commit(
        self, app: GitHubApp, github: FakeGitHub
    ) -> None:
        github.on("POST", f"/repos/{REPO}/pulls/501/reviews", {"id": 9})
        app.review(REPO, 501, "abc123", ReviewEvent.REQUEST_CHANGES, "Needs a test.")
        sent = json.loads(github.last("POST", f"/repos/{REPO}/pulls/501/reviews").content)
        assert sent == {"commit_id": "abc123", "event": "REQUEST_CHANGES", "body": "Needs a test."}

    def test_a_status_carries_our_context_and_a_short_description(
        self, app: GitHubApp, github: FakeGitHub
    ) -> None:
        github.on("POST", f"/repos/{REPO}/statuses/abc123", {"id": 3}, status=201)
        app.status(REPO, "abc123", StatusState.PENDING, "x" * 200)
        sent = json.loads(github.last("POST", f"/repos/{REPO}/statuses/abc123").content)
        assert sent["context"] == STATUS_CONTEXT
        assert sent["state"] == "pending"
        assert len(sent["description"]) == 140


def test_the_app_is_used_only_when_configured(pem: str) -> None:
    assert isinstance(build_github("", "", "https://api.github.com"), SimulatedGitHub)
    escaped = pem.replace("\n", "\\n")
    assert isinstance(build_github("1", escaped, "https://api.github.com"), GitHubApp)
