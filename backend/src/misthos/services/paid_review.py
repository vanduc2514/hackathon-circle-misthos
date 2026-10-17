"""The paid pre-flight review (#204): the review agent, for anyone's diff, before it is
submitted.

An agent pays to hear what the review agent would say about a diff against a set of
acceptance criteria. It is the same reviewer and the same rules the lifecycle review
uses (`store.reviewer`, then `domain.review.decide`), so a diff that passes here passes
there on the same criteria. A pull request it names is read only from a public
repository: the App's token reaches private ones, and an anonymous buyer must not.

It is not that review: nothing here reads or writes an issue, publishes a verdict or
moves money. The lifecycle review stays the signed-in, per-issue action it was.

A review can take minutes with a model, so it runs as a job. The edge verifies the
payment and posts the review; the core answers 202 with a job id and runs it in the
background. The edge polls, and settles the payment when it collects a finished
review, once. A job that fails, or that never finishes (`STALE`), is failed and never
charged.

Gateway settles the `exact` scheme only, so the price is fixed when the 402 names it.
Where #204 asked for `upto` with a cap, the review is sold at the cap, and the size
limits below keep the work inside what the cap pays for.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from misthos.domain.paid import PaidReview
from misthos.domain.repo import normalize_repo
from misthos.domain.review import ChangedFile, Submitted, decide
from misthos.services.github import GitHubError
from misthos.services.paid import PaidCaller
from misthos.services.paid_views import Unanswerable
from misthos.store import Store

log = logging.getLogger("misthos.paid.review")

MAX_CRITERIA = 20
MAX_CRITERION_CHARS = 500
MAX_DIFF_CHARS = 200_000
MAX_FILES = 300
STALE = timedelta(minutes=15)
"""A job not finished by then is failed: its process died, or its reviewer hung."""

_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="paid-review")


def _in_background(work: Callable[[], None]) -> None:
    _WORKERS.submit(work)


run_later: Callable[[Callable[[], None]], None] = _in_background
"""How a job is run. The tests run it in place."""


class PullRequestRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(min_length=3, max_length=200)
    number: int = Field(gt=0)


class ReviewAsked(BaseModel):
    """A diff, or a pull request to read one from, and the criteria to hold it to."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[str] = Field(min_length=1, max_length=MAX_CRITERIA)
    diff: str | None = Field(default=None, max_length=MAX_DIFF_CHARS)
    pull_request: PullRequestRef | None = None
    title: str = Field(default="", max_length=300)
    repo: str | None = Field(default=None, max_length=200)
    checks_passed: bool | None = None
    """Whether the project's own checks pass. Read from GitHub for a pull request."""

    @model_validator(mode="after")
    def _one_source(self) -> ReviewAsked:
        if (self.diff is None) == (self.pull_request is None):
            raise ValueError("give a diff or a pull_request, not both and not neither")
        for criterion in self.criteria:
            if not criterion.strip() or len(criterion) > MAX_CRITERION_CHARS:
                raise ValueError(f"a criterion is 1 to {MAX_CRITERION_CHARS} characters")
        return self


def parse_review_asked(body: Any) -> ReviewAsked:
    try:
        return ReviewAsked.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "body"
        message = str(first.get("msg", "invalid")).removeprefix("Value error, ")
        raise Unanswerable(400, "bad_request", f"{where}: {message}") from exc


_GIT_HEADER = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)")


def _blocks(lines: list[str]) -> list[list[str]]:
    """The diff cut into one block per file."""
    if any(line.startswith("diff --git ") for line in lines):
        starts = [i for i, line in enumerate(lines) if line.startswith("diff --git ")]
    else:
        starts = [
            i
            for i, line in enumerate(lines[:-1])
            if line.startswith("--- ") and lines[i + 1].startswith("+++ ")
        ]
    ends = [*starts[1:], len(lines)]
    return [lines[a:b] for a, b in zip(starts, ends, strict=True)] if starts else []


def _path(block: list[str]) -> str | None:
    def named(line: str, prefix: str) -> str | None:
        raw = line[len(prefix) :].split("\t")[0].strip()
        if raw == "/dev/null" or not raw:
            return None
        return raw[2:] if raw[:2] in {"a/", "b/"} else raw

    for line in block:
        if line.startswith("+++ "):
            found = named(line, "+++ ")
            if found:
                return found
    for line in block:
        if line.startswith("--- "):
            found = named(line, "--- ")
            if found:
                return found
    header = _GIT_HEADER.match(block[0]) if block else None
    return header.group("b") if header else None


def parse_diff(diff: str) -> list[ChangedFile]:
    """A unified diff, `git diff` style or plain, as the files it changes."""
    files: list[ChangedFile] = []
    for block in _blocks(diff.splitlines()):
        path = _path(block)
        if path is None:
            continue
        adds = dels = 0
        in_hunk = False
        for line in block:
            if line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line.startswith("+") and not line.startswith("+++ "):
                adds += 1
            elif in_hunk and line.startswith("-") and not line.startswith("--- "):
                dels += 1
        files.append(ChangedFile(path, adds, dels, "\n".join(block)))
    return files


def _now() -> datetime:
    return datetime.now(UTC)


def submit(
    store: Store, asked: ReviewAsked, caller: PaidCaller, request_digest: str | None
) -> PaidReview:
    """Check what was asked, read the pull request if one was named, and queue the
    review. Anything that cannot be reviewed is refused here, before the job exists."""
    repo = normalize_repo(asked.repo) if asked.repo else None
    if asked.repo and not repo:
        raise Unanswerable(400, "bad_repo", "repo is owner/name or a github.com link")
    title = asked.title
    checks_passed = asked.checks_passed
    head_sha = "0" * 40
    pr_number = 0
    if asked.pull_request is not None:
        repo = normalize_repo(asked.pull_request.repo)
        if not repo:
            raise Unanswerable(400, "bad_repo", "pull_request.repo is owner/name")
        pr_number = asked.pull_request.number
        unreadable = Unanswerable(
            404, "pull_request_unreadable", f"{repo}#{pr_number} could not be read"
        )
        try:
            pr = store.github.read_pull_request(repo, pr_number)
            # The App can read a private repository it is installed on; an anonymous
            # buyer must not, and must not learn that it exists: the same answer as
            # a pull request that is not there.
            if pr.private:
                raise unreadable
            files = store.github.read_files(repo, pr_number)
            if checks_passed is None:
                checks_passed = store.github.checks_passed(repo, pr.head_sha)
        except (GitHubError, KeyError, LookupError) as exc:
            raise unreadable from exc
        head_sha = pr.head_sha
        title = title or (pr.body.strip().splitlines() or [f"pull request #{pr_number}"])[0]
    else:
        files = parse_diff(asked.diff or "")
        if not files:
            raise Unanswerable(400, "bad_diff", "the diff names no file it changes")
    if len(files) > MAX_FILES:
        raise Unanswerable(
            413, "too_large", f"{len(files)} files; a paid review takes at most {MAX_FILES}"
        )

    job = PaidReview(
        id=uuid.uuid4().hex,
        status="queued",
        request_digest=request_digest,
        payer=caller.payer.lower() if caller.payer else None,
        free_caller=caller.free_caller,
        repo=repo,
        title=title[:300] or "a diff",
        criteria=len(asked.criteria),
        files=len(files),
        additions=sum(f.additions for f in files),
        deletions=sum(f.deletions for f in files),
        created_at=_now(),
    )
    store.repo.save_paid_review(job)
    submitted = Submitted(
        repo=repo or "unknown/unknown",
        pr_number=pr_number,
        head_sha=head_sha,
        title=job.title,
        criteria=tuple(c.strip() for c in asked.criteria),
        files=tuple(files),
        checks_passed=checks_passed,
    )
    run_later(lambda: run(store, job.id, submitted))
    return job


def run(store: Store, job_id: str, submitted: Submitted) -> None:
    """Review the diff and keep the result on the job. Never raises."""
    job = store.repo.get_paid_review(job_id)
    if job is None or job.status != "queued":
        return
    started = _now()
    store.repo.save_paid_review(replace(job, status="running", started_at=started))
    try:
        judgement, seconds = store._judge(store.reviewer, submitted)
        decided = decide(
            judgement,
            checks_passed=submitted.checks_passed,
            files_changed=len(submitted.files),
            rework_rounds=0,
        )
    except Exception:
        # Whatever went wrong stays in the log: the buyer is told only that the
        # review failed and that nothing was charged.
        log.exception("paid review %s failed", job_id)
        store.repo.save_paid_review(
            replace(
                job,
                status="failed",
                started_at=started,
                finished_at=_now(),
                error="the reviewer could not finish this review",
            )
        )
        return
    result = {
        "verdict": decided.verdict.value,
        "rule": decided.rule,
        "findings": decided.findings,
        "criteria": [
            {"index": c.index, "criterion": c.criterion, "met": c.met, "evidence": c.evidence}
            for c in judgement.checks
        ],
        "summary": judgement.summary,
        "reviewer": judgement.reviewer,
        "checks_passed": submitted.checks_passed,
        "files_reviewed": len(submitted.files),
        "seconds": seconds,
    }
    store.repo.save_paid_review(
        replace(
            job,
            status="done",
            started_at=started,
            finished_at=_now(),
            verdict=decided.verdict.value,
            rule=decided.rule,
            reviewer=judgement.reviewer,
            seconds=seconds,
            cost=judgement.cost,
            result=result,
        )
    )


def poll(store: Store, job_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Where a job is. A job left unfinished past `STALE` is failed now."""
    job = store.repo.get_paid_review(job_id)
    if job is None:
        raise Unanswerable(404, "no_such_job", "no such review")
    at = now or _now()
    if job.status in {"queued", "running"} and at - job.created_at > STALE:
        job = replace(
            job, status="failed", finished_at=at, error="the review did not finish in time"
        )
        store.repo.save_paid_review(job)
    answer: dict[str, Any] = {
        "job_id": job.id,
        "status": job.status,
        "queued_at": job.created_at.isoformat(),
    }
    if job.status == "done":
        answer.update(job.result)
    if job.status == "failed":
        answer["error"] = job.error
    return answer
