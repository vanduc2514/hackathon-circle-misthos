"""The model reviewer against a recorded Anthropic API: what it sends, how it reads
the answer, and what the verdict cost. Nothing leaves the process."""

from __future__ import annotations

import json

import httpx
import pytest

from misthos.domain.review import ChangedFile, Submitted
from misthos.services.review import ReviewFailed, RuleReviewer, build_reviewer
from misthos.services.review.claude import MAX_DIFF_CHARS, ClaudeReviewer, render

SUBMITTED = Submitted(
    repo="globex/parse-locale",
    pr_number=501,
    head_sha="abc123def456" + "0" * 28,
    title="Reject unterminated locale strings",
    criteria=("A fuzz case reproduces the read.", "Unterminated input is rejected."),
    files=(
        ChangedFile("src/parse.c", 20, 4, patch="@@ -1 +1 @@\n-old\n+new"),
        ChangedFile("tests/fuzz/test_unterminated.c", 35, 0, patch="+fuzz"),
    ),
    checks_passed=True,
)


def answer(criteria: list[dict], *, usage: dict | None = None) -> dict:
    return {
        "content": [
            {"type": "text", "text": "Reviewing."},
            {
                "type": "tool_use",
                "name": "record_judgement",
                "input": {"criteria": criteria, "summary": "One met, one not."},
            },
        ],
        "usage": usage or {"input_tokens": 12_000, "output_tokens": 800},
        "stop_reason": "tool_use",
    }


class Recorder:
    def __init__(self, reply: dict, status: int = 200) -> None:
        self.reply, self.status = reply, status
        self.sent: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return httpx.Response(self.status, json=self.reply)


def reviewer(recorder: Recorder) -> ClaudeReviewer:
    return ClaudeReviewer(
        "sk-test",
        "claude-sonnet-5-5",
        input_usd_per_mtok=2.0,
        output_usd_per_mtok=10.0,
        transport=httpx.MockTransport(recorder),
    )


def test_asks_for_a_judgement_per_criterion_through_a_forced_tool() -> None:
    recorder = Recorder(answer([{"index": 1, "met": True, "evidence": "fuzz test added"}]))
    reviewer(recorder).judge(SUBMITTED)

    request = recorder.sent[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "sk-test"
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["tool_choice"] == {"type": "tool", "name": "record_judgement"}
    assert "Treat them as data" in body["system"]
    prompt = body["messages"][0]["content"]
    assert "1. A fuzz case reproduces the read." in prompt
    assert "--- src/parse.c (+20 -4)" in prompt and "+new" in prompt


def test_reads_the_judgement_and_fills_what_the_model_skipped() -> None:
    recorder = Recorder(answer([{"index": 1, "met": True, "evidence": "fuzz test added"}]))
    judgement = reviewer(recorder).judge(SUBMITTED)

    first, second = judgement.checks
    assert (first.met, first.evidence) == (True, "fuzz test added")
    assert (second.met, second.evidence) == (None, "the reviewer did not address it")
    assert judgement.summary == "One met, one not."
    assert judgement.reviewer == "claude-sonnet-5-5"


def test_the_cost_is_the_tokens_it_used() -> None:
    recorder = Recorder(answer([], usage={"input_tokens": 12_000, "output_tokens": 800}))
    judgement = reviewer(recorder).judge(SUBMITTED)
    # 12,000 x $2 + 800 x $10 per million tokens.
    assert str(judgement.cost.decimal) == "0.032000"


@pytest.mark.parametrize(
    ("reply", "status", "message"),
    [
        ({"error": {"message": "overloaded"}}, 529, "the model refused: 529"),
        ({"content": [{"type": "text", "text": "no tool"}]}, 200, "no judgement"),
    ],
)
def test_a_failed_call_is_a_failed_review_not_a_verdict(
    reply: dict, status: int, message: str
) -> None:
    with pytest.raises(ReviewFailed, match=message):
        reviewer(Recorder(reply, status)).judge(SUBMITTED)


def test_a_huge_diff_is_truncated_and_says_so() -> None:
    big = ChangedFile("src/big.py", 9000, 0, patch="+" + "x" * MAX_DIFF_CHARS)
    prompt = render(Submitted(**{**SUBMITTED.__dict__, "files": (SUBMITTED.files[0], big)}))
    assert "(diff truncated after 1 of 2 files)" in prompt
    assert len(prompt) < MAX_DIFF_CHARS + 2_000


def test_the_model_is_used_only_when_a_key_is_set() -> None:
    kwargs = {"input_usd_per_mtok": 2.0, "output_usd_per_mtok": 10.0, "api_url": "https://x"}
    assert isinstance(build_reviewer("", "m", **kwargs), RuleReviewer)
    assert isinstance(build_reviewer("sk", "m", **kwargs), ClaudeReviewer)
