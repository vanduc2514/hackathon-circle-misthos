"""The nightly pull-request review, judged where it can be judged without GitHub.

The model call and the `gh` calls are not tested here; what is tested is the part that
decides, because that is the part where a mistake merges something nobody approved. A
verdict that cannot be parsed is a refusal rather than a guess, and every reason not to
merge is a fact about the pull request rather than a judgement.
"""

from __future__ import annotations

import pytest

from misthos.services.pr_review import (
    PullRequest,
    ReviewFailed,
    Verdict,
    parse_verdict,
    prompt_for,
    refusal,
    reviewed_head,
    verdict_from,
)


def pr(**overrides) -> PullRequest:
    """A pull request that may be merged, so each test can spoil one thing."""
    fields = {
        "number": 42,
        "title": "feat: something",
        "author": "someone",
        "base": "main",
        "head": "a" * 40,
        "draft": False,
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
        "decision": "APPROVED",
        "checks": ("SUCCESS", "SUCCESS"),
    }
    fields.update(overrides)
    return PullRequest(**fields)


class TestTheVerdict:
    def test_it_reads_the_object_the_model_was_asked_for(self) -> None:
        verdict = parse_verdict('{"decision": "approve", "body": "Checked the money path."}')
        assert verdict.decision == "approve"
        assert verdict.body == "Checked the money path."

    def test_it_reads_through_a_fence_and_around_prose(self) -> None:
        """Models wrap an answer in a fence or a sentence; both are still the answer."""
        fenced = 'Here is my review:\n```json\n{"decision": "request_changes", "body": "No."}\n```'
        assert parse_verdict(fenced).decision == "request_changes"

    def test_the_spellings_of_a_decision_are_narrowed_to_two(self) -> None:
        assert verdict_from({"decision": "CHANGES-REQUESTED", "body": "x"}).decision == (
            "request_changes"
        )
        assert verdict_from({"verdict": " reject ", "review": "x"}).decision == "request_changes"

    def test_an_answer_that_is_not_json_is_a_failure(self) -> None:
        with pytest.raises(ReviewFailed, match="no object"):
            parse_verdict("I think this looks fine!")

    def test_a_decision_that_is_not_one_of_the_two_is_refused(self) -> None:
        """The whole point: something that is neither approve nor request changes must
        not be read as an approval by default."""
        with pytest.raises(ReviewFailed, match="not a decision"):
            parse_verdict('{"decision": "looks fine to me", "body": "x"}')

    def test_a_verdict_with_no_review_to_post_is_refused(self) -> None:
        with pytest.raises(ReviewFailed, match="no review to post"):
            parse_verdict('{"decision": "approve", "body": "   "}')

    def test_a_stated_verdict_names_itself(self) -> None:
        stated = Verdict("request_changes", "The fee is never written.").comment()
        assert stated.startswith("**Verdict: REQUEST CHANGES**")


class TestTheChecks:
    def test_all_successful_is_green(self) -> None:
        assert pr(checks=("SUCCESS", "NEUTRAL", "SKIPPED")).checks_state() == "green"

    def test_a_failure_is_red(self) -> None:
        assert pr(checks=("SUCCESS", "FAILURE")).checks_state() == "red"

    def test_a_run_that_has_not_finished_is_pending_not_failed(self) -> None:
        """`IN_PROGRESS` is not a failure, and pending must never merge — reading it as
        red would stop the merge for the right reason and the wrong note."""
        assert pr(checks=("SUCCESS", "IN_PROGRESS")).checks_state() == "pending"
        assert pr(checks=()).checks_state() == "pending"

    def test_no_checks_at_all_is_pending(self) -> None:
        assert pr(checks=()).checks_state() == "pending"


class TestWhatMustNotMerge:
    def test_an_approved_pull_request_that_is_green_may_merge(self) -> None:
        assert refusal(pr(), Verdict("approve", "ok")) is None

    def test_a_request_changes_verdict_never_merges(self) -> None:
        blocked = refusal(pr(), Verdict("request_changes", "not yet"))
        assert blocked == "the verdict is request changes"

    def test_a_draft_never_merges(self) -> None:
        assert refusal(pr(draft=True), Verdict("approve", "ok")) == "it is a draft"

    def test_a_conflict_never_merges(self) -> None:
        blocked = refusal(pr(mergeable="CONFLICTING"), Verdict("approve", "ok"))
        assert blocked == "it does not merge (CONFLICTING)"

    def test_a_pull_request_another_reviewer_held_back_never_merges(self) -> None:
        blocked = refusal(pr(decision="CHANGES_REQUESTED"), Verdict("approve", "ok"))
        assert blocked == "a review is asking for changes"

    def test_a_red_check_never_merges(self) -> None:
        blocked = refusal(pr(checks=("SUCCESS", "FAILURE")), Verdict("approve", "ok"))
        assert blocked == "a check failed"

    def test_a_running_check_never_merges(self) -> None:
        blocked = refusal(pr(checks=("SUCCESS", "QUEUED")), Verdict("approve", "ok"))
        assert blocked == "checks have not finished"

    def test_a_pull_request_with_no_head_never_merges(self) -> None:
        blocked = refusal(pr(head=""), Verdict("approve", "ok"))
        assert blocked == "it has no head commit"


class TestWhatHasBeenReviewed:
    def test_the_last_review_by_this_account_carries_its_commit(self) -> None:
        reviews = [
            {"author": {"login": "someone-else"}, "commitId": "b" * 40},
            {"author": {"login": "me"}, "commitId": "a" * 40},
        ]
        assert reviewed_head(reviews, "me") == "a" * 40

    def test_a_review_by_anyone_else_is_not_this_accounts_review(self) -> None:
        reviews = [{"author": {"login": "someone-else"}, "commitId": "b" * 40}]
        assert reviewed_head(reviews, "me") is None

    def test_nothing_reviewed_is_no_head(self) -> None:
        assert reviewed_head([], "me") is None


class TestTheRowFromGitHub:
    def test_it_reads_the_list_row(self) -> None:
        row = {
            "number": 130,
            "title": "fix(arc): pay before the deadline",
            "author": {"login": "khacthebkhn"},
            "baseRefName": "main",
            "headRefOid": "c" * 40,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"state": "IN_PROGRESS"}],
        }
        parsed = PullRequest.from_json(row)
        assert parsed.number == 130 and parsed.author == "khacthebkhn"
        assert parsed.checks == ("SUCCESS", "IN_PROGRESS")
        assert parsed.checks_state() == "pending"

    def test_a_row_without_a_rollup_is_pending_rather_than_green(self) -> None:
        row = {"number": 1, "title": "t", "author": {"login": "a"}, "baseRefName": "main"}
        assert PullRequest.from_json(row).checks_state() == "pending"


def test_the_prompt_carries_the_standards_and_the_diff() -> None:
    """The prompt is where the repository's standards reach the model, so the parts that
    are specific to this project have to be in it."""
    text = prompt_for(pr(number=7, title="feat: a thing"), "diff --git a/x b/x\n+one line")

    assert "single JSON object" in text and '"decision"' in text
    assert "api-schema.d.ts" in text and "MisthosEscrow.abi.json" in text
    assert "money path" in text
    assert "#7: feat: a thing" in text and "+one line" in text


def test_the_prompt_asks_for_the_shape_the_parser_reads() -> None:
    """Every decision the parser accepts has to be one the prompt offered, or the model
    is being asked for an answer that would be refused."""
    asked = prompt_for(pr(), "diff")

    assert '"decision": "approve" or "request_changes"' in asked
    assert '"body"' in asked
