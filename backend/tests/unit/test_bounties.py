"""The calibration corpus of paid bounties (#41): what counts as paid, and the rows."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx

from misthos.domain.pricing import ComplexitySignals
from misthos.domain.signals import read
from misthos.services.bounties import SOURCES, GitHub, bounty_in, facts_for
from misthos.services.calibration import CORPUS, load_corpus

BOT = {"login": "algora-pbc[bot]"}


def comment(body: str, user: dict | None = None, n: int = 1) -> dict:
    return {"user": user or BOT, "body": body, "html_url": f"https://github.com/o/r/issues/1#c{n}"}


class TestWhatCountsAsPaid:
    def test_a_bounty_priced_up_front_and_awarded_is_paid(self) -> None:
        paid = bounty_in(
            [
                comment("## 💎 $100 bounty [• Cap](https://algora.io/Cap)", n=1),
                comment("🎉🎈 @someone has been awarded **$100**! 🎈🎊", n=2),
            ]
        )
        assert paid is not None
        assert (paid.priced, paid.awarded) == (Decimal(100), Decimal(100))
        assert paid.award_url.endswith("#c2")

    def test_the_older_priced_format_is_read_too(self) -> None:
        paid = bounty_in(
            [
                comment("💎 **$144.00** bounty created by someone"),
                comment("🎉🎈 @x has been awarded **$144**! 🎈🎊"),
            ]
        )
        assert paid is not None and paid.priced == Decimal("144.00")

    def test_a_split_bounty_sums_its_awards(self) -> None:
        paid = bounty_in(
            [
                comment("💎 $1,000 bounty"),
                comment("🎉🎈 @a has been awarded **$600**! 🎈🎊"),
                comment("🎉🎈 @b has been awarded **$400** by **Org**! 🎈🎊"),
            ]
        )
        assert paid is not None and paid.awarded == Decimal(1000)

    def test_a_tip_after_the_fact_is_not_a_bounty(self) -> None:
        # Paid, but never priced up front: not the kind of price the engine sets.
        assert bounty_in([comment("🎉🎈 @x has been awarded **$500** by **Org**! 🎈🎊")]) is None

    def test_a_bounty_never_paid_is_not_a_worth(self) -> None:
        priced_only = [comment("💎 $100 bounty"), comment("💡 @x submitted a pull request")]
        assert bounty_in(priced_only) is None

    def test_only_the_bots_own_comments_count(self) -> None:
        anyone = {"login": "someone"}
        forged = [
            comment("💎 $100 bounty", anyone),
            comment("🎉🎈 has been awarded **$9**", anyone),
        ]
        assert bounty_in(forged) is None

    def test_a_promise_to_pay_is_not_the_payment(self) -> None:
        promise = comment("@x: You've been awarded a **$500** by **Org**! Complete onboarding")
        assert bounty_in([comment("💎 $500 bounty"), promise]) is None


class TestTheCorpus:
    def test_it_is_exactly_the_listed_issues(self) -> None:
        listed = json.loads(SOURCES.read_text(encoding="utf-8"))["issues"]
        rows = json.loads(CORPUS.read_text(encoding="utf-8"))["issues"]
        as_refs = {
            r["id"].removeprefix("https://github.com/").replace("/issues/", "#") for r in rows
        }
        assert as_refs == set(listed)
        assert len(rows) >= 20  # enough for calibration to fit the weights, not just the rate

    def test_every_worth_points_at_the_payment_on_that_issue(self) -> None:
        for obs in load_corpus():
            assert obs.source.startswith(obs.id + "#issuecomment-"), obs.id
            assert obs.worth.base_units > 0

    def test_every_score_is_the_engines_own_reading(self) -> None:
        for row in json.loads(CORPUS.read_text(encoding="utf-8"))["issues"]:
            ComplexitySignals(**row["signals"]).validate()
            assert set(row["reasons"]) == set(row["signals"]), row["id"]

    def test_no_organisation_dominates(self) -> None:
        owners: dict[str, int] = {}
        for row in json.loads(CORPUS.read_text(encoding="utf-8"))["issues"]:
            owner = row["id"].removeprefix("https://github.com/").split("/")[0]
            owners[owner] = owners.get(owner, 0) + 1
        assert max(owners.values()) <= 3


def test_an_issue_is_read_the_way_the_platform_reads_one() -> None:
    routes = {
        "/repos/o/r/issues/7": {
            "title": "Crash on start",
            "body": "Steps to reproduce: run it. Expected: it starts.",
            "labels": [{"name": "bug"}],
        },
        "/repos/o/r": {"default_branch": "main"},
        "/repos/o/r/git/trees/main": {
            "tree": [
                {"path": "src/app.py", "type": "blob"},
                {"path": "tests/test_app.py", "type": "blob"},
                {"path": "pyproject.toml", "type": "blob"},
            ]
        },
        "/repos/o/r/issues/7/timeline": [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {"number": 9, "state": "closed", "pull_request": {"merged_at": None}}
                },
            }
        ],
    }

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=routes[request.url.path])

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(answer))
    facts = facts_for(GitHub(None, client), "o/r", 7)

    assert facts.labels == ("bug",)
    assert facts.prior_attempts == 1  # the closed, unmerged pull request
    assert (facts.tree.source_files, facts.tree.test_files) == (1, 1)
    assert read(facts).signals.blast_radius == 3.0  # labelled bug
