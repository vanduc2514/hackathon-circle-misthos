"""The paid pre-flight review (#204), as the core runs it for the edge.

The same reviewer and rules as the lifecycle review, on a diff anyone sends, as a
job: queued, run, then done or failed. Nothing here may touch an issue, and the
lifecycle review is not changed by any of it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from misthos.config import settings
from misthos.domain.money import Usdc
from misthos.domain.review import ChangedFile, Judgement
from misthos.main import app
from misthos.services import paid_review
from misthos.services.github.base import PullRequest
from misthos.services.review.base import ReviewFailed
from misthos.store import Store, store

TOKEN = "edge-core-token-for-tests"
PAID = "/api/v1/internal/paid"
PAYER = "0x2222222222222222222222222222222222222222"
EDGE = {
    "X-Misthos-Core-Token": TOKEN,
    "X-Misthos-Paid-Route": "review",
    "X-Misthos-Payer": PAYER,
    "X-Misthos-Request-Digest": "e" * 64,
}

DIFF = """diff --git a/src/hooks/deliver.py b/src/hooks/deliver.py
index 1111111..2222222 100644
--- a/src/hooks/deliver.py
+++ b/src/hooks/deliver.py
@@ -10,3 +10,6 @@ def deliver(hook):
-    send(hook)
+    for attempt in backoff(5):
+        if send(hook, timeout=attempt):
+            return
diff --git a/tests/test_deliver.py b/tests/test_deliver.py
new file mode 100644
--- /dev/null
+++ b/tests/test_deliver.py
@@ -0,0 +1,2 @@
+def test_retries():
+    assert retried()
diff --git a/CHANGELOG.md b/CHANGELOG.md
--- a/CHANGELOG.md
+++ b/CHANGELOG.md
@@ -1 +1,2 @@
+- Retry deliveries that time out.
"""

CRITERIA = [
    "Covered by a test",
    "Documented in the changelog",
    "Deliveries that time out are retried with backoff",
]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def edge_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "edge_core_token", TOKEN)


@pytest.fixture(autouse=True)
def in_place(monkeypatch: pytest.MonkeyPatch) -> list:
    """Run each job as soon as it is queued, unless a test holds them."""
    held: list = []
    monkeypatch.setattr(paid_review, "run_later", lambda work: work())
    return held


def post(client: TestClient, body: object):
    return client.post(f"{PAID}/review", json=body, headers=EDGE)


def poll(client: TestClient, job_id: str):
    return client.get(f"{PAID}/review/jobs/{job_id}", headers={"X-Misthos-Core-Token": TOKEN})


class TestAReview:
    def test_queues_a_job_and_finishes_it_with_the_verdict(self, client):
        queued = post(client, {"criteria": CRITERIA, "diff": DIFF, "checks_passed": True})
        assert queued.status_code == 202
        job = queued.json()
        assert job["status"] == "queued" and job["files"] == 3

        done = poll(client, job["job_id"]).json()
        assert done["status"] == "done"
        assert done["verdict"] == "accept"
        assert done["rule"]
        assert [c["met"] for c in done["criteria"]] == [True, True, None]
        assert all(c["evidence"] for c in done["criteria"])
        assert done["criteria"][0]["evidence"].startswith("changes `tests/test_deliver.py`")
        assert done["findings"] and done["summary"]
        assert done["reviewer"] == store.reviewer.name
        assert done["files_reviewed"] == 3

    def test_asks_for_rework_against_an_unmet_criterion(self, client):
        no_tests = DIFF.split("diff --git a/tests")[0]
        job = post(client, {"criteria": CRITERIA, "diff": no_tests, "checks_passed": True})
        done = poll(client, job.json()["job_id"]).json()
        assert done["verdict"] == "rework"
        assert done["criteria"][0]["met"] is False

    def test_failing_checks_are_rework(self, client):
        job = post(client, {"criteria": CRITERIA, "diff": DIFF, "checks_passed": False})
        assert poll(client, job.json()["job_id"]).json()["verdict"] == "rework"

    def test_is_the_lifecycle_reviewers_verdict_for_the_same_diff(self, client):
        from misthos.domain.review import Submitted, decide

        files = tuple(paid_review.parse_diff(DIFF))
        judgement = store.reviewer.judge(
            Submitted("a/b", 0, "0" * 40, "a diff", tuple(CRITERIA), files, True)
        )
        expected = decide(judgement, checks_passed=True, files_changed=3, rework_rounds=0)
        job = post(client, {"criteria": CRITERIA, "diff": DIFF, "checks_passed": True})
        done = poll(client, job.json()["job_id"]).json()
        assert (done["verdict"], done["rule"]) == (expected.verdict.value, expected.rule)
        assert done["findings"] == expected.findings

    def test_reads_a_pull_request_it_is_pointed_at(self, client):
        github = store.github
        github.pulls[("acme/hooks", 7)] = PullRequest(
            repo="acme/hooks",
            number=7,
            author="someone",
            head_sha="f" * 40,
            body="Retry timed-out deliveries\n\nWith backoff.",
            merged=False,
            files_changed=2,
            additions=10,
            deletions=1,
        )
        github.files[("acme/hooks", 7)] = [
            ChangedFile("src/deliver.py", 8, 1),
            ChangedFile("tests/test_deliver.py", 2, 0),
        ]
        github.checks[("acme/hooks", "f" * 40)] = True
        job = post(
            client, {"criteria": CRITERIA[:1], "pull_request": {"repo": "acme/hooks", "number": 7}}
        )
        assert job.status_code == 202
        done = poll(client, job.json()["job_id"]).json()
        assert done["verdict"] == "accept" and done["checks_passed"] is True
        kept = store.repo.get_paid_review(job.json()["job_id"])
        assert kept is not None and kept.title == "Retry timed-out deliveries"
        assert kept.repo == "acme/hooks"

    def test_a_private_pull_request_is_as_unreadable_as_a_missing_one(self, client):
        """The App can read a private repository it is installed on. An anonymous buyer
        must get neither its code nor the fact that it exists."""
        github = store.github
        github.pulls[("acme/secret", 3)] = PullRequest(
            repo="acme/secret",
            number=3,
            author="someone",
            head_sha="a" * 40,
            body="Private work",
            merged=False,
            files_changed=1,
            additions=1,
            deletions=0,
            private=True,
        )
        github.files[("acme/secret", 3)] = [ChangedFile("src/secret.py", 1, 0, "+key = 1")]
        private = post(
            client, {"criteria": CRITERIA, "pull_request": {"repo": "acme/secret", "number": 3}}
        )
        missing = post(
            client, {"criteria": CRITERIA, "pull_request": {"repo": "acme/secret", "number": 4}}
        )
        assert private.status_code == missing.status_code == 404
        assert private.json()["error"] == missing.json()["error"] == "pull_request_unreadable"
        assert "secret.py" not in private.text

    def test_a_pull_request_it_cannot_read_is_refused_before_any_job(self, client):
        res = post(
            client, {"criteria": CRITERIA, "pull_request": {"repo": "acme/none", "number": 1}}
        )
        assert (res.status_code, res.json()["error"]) == (404, "pull_request_unreadable")

    def test_changes_no_issue_and_publishes_nothing(self, client):
        before = {r.id: (r.version, r.state, r.review) for r in store.repo.list_issues()}
        sent = len(store.github.sent)
        job = post(client, {"criteria": CRITERIA, "diff": DIFF})
        poll(client, job.json()["job_id"])
        assert {r.id: (r.version, r.state, r.review) for r in store.repo.list_issues()} == before
        assert len(store.github.sent) == sent, "no comment, review or status was posted"

    def test_keeps_latency_cost_and_verdict_for_calibration(self, client):
        job_id = post(client, {"criteria": CRITERIA, "diff": DIFF, "checks_passed": True}).json()[
            "job_id"
        ]
        kept = store.repo.get_paid_review(job_id)
        assert kept is not None
        assert (kept.status, kept.verdict) == ("done", "accept")
        assert kept.seconds is not None and kept.cost == Usdc(0)
        assert kept.payer == PAYER and kept.request_digest == "e" * 64
        assert (kept.files, kept.additions, kept.deletions) == (3, 6, 1)
        assert kept.started_at and kept.finished_at
        assert "diff" not in kept.result, "the diff itself is not kept"


class TestTheJob:
    def test_is_queued_until_it_runs(self, client, monkeypatch):
        held: list = []
        monkeypatch.setattr(paid_review, "run_later", held.append)
        job_id = post(client, {"criteria": CRITERIA, "diff": DIFF}).json()["job_id"]
        assert poll(client, job_id).json()["status"] == "queued"
        held[0]()
        assert poll(client, job_id).json()["status"] == "done"

    def test_a_reviewer_that_fails_fails_the_job_and_says_nothing_inside(self, client, monkeypatch):
        class Broken:
            name = "broken"

            def judge(self, submitted) -> Judgement:
                raise ReviewFailed("the model at http://10.0.0.9/v1 answered 500")

        monkeypatch.setattr(store, "reviewer", Broken())
        job_id = post(client, {"criteria": CRITERIA, "diff": DIFF}).json()["job_id"]
        res = poll(client, job_id)
        assert res.json() == {
            "job_id": job_id,
            "status": "failed",
            "queued_at": res.json()["queued_at"],
            "error": "the reviewer could not finish this review",
        }
        assert "10.0.0.9" not in res.text

    def test_a_job_that_never_finishes_is_failed(self, client, monkeypatch):
        monkeypatch.setattr(paid_review, "run_later", lambda work: None)
        job_id = post(client, {"criteria": CRITERIA, "diff": DIFF}).json()["job_id"]
        job = store.repo.get_paid_review(job_id)
        assert job is not None
        later = job.created_at + paid_review.STALE + timedelta(seconds=1)
        assert paid_review.poll(store, job_id, now=later)["status"] == "failed"
        assert poll(client, job_id).json()["status"] == "failed"

    def test_runs_once(self, client, monkeypatch):
        held: list = []
        monkeypatch.setattr(paid_review, "run_later", held.append)
        job_id = post(client, {"criteria": CRITERIA, "diff": DIFF}).json()["job_id"]
        held[0]()
        first = store.repo.get_paid_review(job_id)
        held[0]()
        assert store.repo.get_paid_review(job_id) == first

    def test_an_unknown_job_is_not_found(self, client):
        res = poll(client, "nope")
        assert (res.status_code, res.json()["error"]) == (404, "no_such_job")


class TestWhatItRefuses:
    @pytest.mark.parametrize(
        ("body", "error"),
        [
            ({"criteria": CRITERIA}, "bad_request"),
            (
                {"criteria": CRITERIA, "diff": DIFF, "pull_request": {"repo": "a/b", "number": 1}},
                "bad_request",
            ),
            ({"criteria": [], "diff": DIFF}, "bad_request"),
            ({"criteria": [""], "diff": DIFF}, "bad_request"),
            ({"criteria": ["x" * 501], "diff": DIFF}, "bad_request"),
            ({"criteria": ["c"] * 21, "diff": DIFF}, "bad_request"),
            ({"criteria": CRITERIA, "diff": "x" * (paid_review.MAX_DIFF_CHARS + 1)}, "bad_request"),
            ({"criteria": CRITERIA, "diff": "not a diff"}, "bad_diff"),
            ({"criteria": CRITERIA, "diff": DIFF, "issue_id": "ISS-1001"}, "bad_request"),
            ({"criteria": CRITERIA, "diff": DIFF, "repo": "not a repo"}, "bad_repo"),
        ],
    )
    def test_refuses_what_it_cannot_review(self, client, body, error):
        res = post(client, body)
        assert res.status_code == 400
        assert res.json()["error"] == error
        assert "pydantic" not in res.text

    def test_refuses_a_diff_too_wide_for_its_price(self, client):
        many = "".join(
            f"--- a/f{i}.py\n+++ b/f{i}.py\n@@ -0,0 +1 @@\n+x\n"
            for i in range(paid_review.MAX_FILES + 1)
        )
        res = post(client, {"criteria": CRITERIA, "diff": many})
        assert (res.status_code, res.json()["error"]) == (413, "too_large")


def test_reads_plain_and_git_diffs():
    files = paid_review.parse_diff(DIFF)
    assert [(f.path, f.additions, f.deletions) for f in files] == [
        ("src/hooks/deliver.py", 3, 1),
        ("tests/test_deliver.py", 2, 0),
        ("CHANGELOG.md", 1, 0),
    ]
    plain = "--- README.md\n+++ README.md\n@@ -1 +1 @@\n-x\n+y\n"
    assert [(f.path, f.additions, f.deletions) for f in paid_review.parse_diff(plain)] == [
        ("README.md", 1, 1)
    ]
    deleted = "diff --git a/old.txt b/old.txt\n--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    assert [(f.path, f.deletions) for f in paid_review.parse_diff(deleted)] == [("old.txt", 1)]
    assert paid_review.parse_diff("") == []


def test_every_repository_keeps_paid_reviews(every_store: Store):
    from dataclasses import replace
    from datetime import UTC, datetime

    from misthos.domain.paid import PaidReview

    job = PaidReview(
        id="a" * 32,
        status="queued",
        request_digest=None,
        payer=PAYER,
        free_caller=None,
        repo="acme/hooks",
        title="t",
        criteria=2,
        files=3,
        additions=4,
        deletions=5,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    every_store.repo.save_paid_review(job)
    assert every_store.repo.get_paid_review(job.id) == job
    done = replace(
        job,
        status="done",
        verdict="accept",
        seconds=1.5,
        cost=Usdc(12),
        result={"verdict": "accept"},
        finished_at=datetime(2026, 10, 1, 0, 1, tzinfo=UTC),
    )
    every_store.repo.save_paid_review(done)
    assert every_store.repo.get_paid_review(job.id) == done
    assert every_store.repo.get_paid_review("b" * 32) is None
