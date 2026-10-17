"""Opportunities, settlement receipts and the screen that stays off (#206), as the core
answers them to the edge."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from misthos.auth import sessions
from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.ledger import EscrowStatus
from misthos.domain.money import Usdc
from misthos.main import app
from misthos.services import certificates, paid_deals
from misthos.store import store
from tests.support.accounts import account_for

TOKEN = "edge-core-token-for-tests"
EDGE = {"X-Misthos-Core-Token": TOKEN}
PAID = "/api/v1/internal/paid"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def edge_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "edge_core_token", TOKEN)


@pytest.fixture
def certificate_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "certificates"
    secret.write_text("0x" + "5d" * 32)
    os.chmod(secret, 0o600)
    monkeypatch.setattr(settings, "secret_store_dir", str(tmp_path))
    monkeypatch.setattr(settings, "certificate_secret_ref", "certificates")


def private_strings() -> list[str]:
    found = [c.wallet.address for c in store.repo.list_contributors()]
    found += [r.payout_tx_hash for r in store.repo.list_issues() if r.payout_tx_hash]
    return [s.lower() for s in found if s]


def assert_private(text: str) -> None:
    for secret in private_strings():
        assert secret not in text.lower(), secret


def open_funded() -> list:
    return [
        r for r in store.repo.list_issues() if r.state in paid_deals.OPEN and r.escrow is not None
    ]


class TestOpportunities:
    def get(self, client: TestClient, **query: str):
        return client.get(f"{PAID}/opportunities", params=query, headers=EDGE)

    def test_are_the_funded_issues_still_open_and_nothing_else(self, client):
        body = self.get(client).json()
        assert {o["issue_id"] for o in body["opportunities"]} == {r.id for r in open_funded()}
        assert body["total"] == len(open_funded()) and body["next"] is None
        closed = {r.id for r in store.repo.list_issues() if r.state not in paid_deals.OPEN}
        assert closed and not closed & {o["issue_id"] for o in body["opportunities"]}

    def test_say_what_the_work_pays_and_when_it_is_due(self, client):
        rec = next(r for r in open_funded() if r.state is IssueState.FUNDED)
        found = next(o for o in self.get(client).json()["opportunities"] if o["issue_id"] == rec.id)
        gross = int(rec.escrow.amount["base_units"])
        assert found["price"]["base_units"] == gross
        assert (
            found["contributor_receives"]["base_units"]
            == gross - gross * rec.escrow.fee_bps // 10_000
        )
        assert found["claimable"] is True and found["claim"] == {
            "claimed": False,
            "expires_at": None,
        }
        assert found["deadline"] and found["github_url"] == rec.github_url
        assert found["acceptance_criteria"] == rec.acceptance_criteria

    def test_a_claimed_issue_says_when_its_claim_runs_out(self, client):
        rec = next(r for r in open_funded() if r.state is IssueState.CLAIMED)
        found = next(o for o in self.get(client).json()["opportunities"] if o["issue_id"] == rec.id)
        assert found["claimable"] is False
        assert found["claim"]["claimed"] is True and found["claim"]["expires_at"]

    def test_filters(self, client):
        claimable = self.get(client, claimable="true").json()["opportunities"]
        assert claimable and all(o["state"] == "FUNDED" for o in claimable)
        taken = self.get(client, claimable="false").json()["opportunities"]
        assert taken and all(o["state"] != "FUNDED" for o in taken)

        rec = open_funded()[0]
        by_repo = self.get(client, repo=rec.repo.upper()).json()["opportunities"]
        assert {o["repo"] for o in by_repo} == {rec.repo}

        label = rec.labels[0]
        by_label = self.get(client, labels=label.upper()).json()["opportunities"]
        assert by_label and all(label in o["labels"] for o in by_label)

        prices = sorted(int(r.escrow.amount["base_units"]) for r in open_funded())
        cut = Usdc(prices[len(prices) // 2]).decimal
        dear = self.get(client, min_price=str(cut)).json()["opportunities"]
        cheap = self.get(client, max_price=str(cut)).json()["opportunities"]
        assert dear and cheap
        assert all(o["price"]["base_units"] >= Usdc.from_decimal(cut).base_units for o in dear)
        assert all(o["price"]["base_units"] <= Usdc.from_decimal(cut).base_units for o in cheap)

    def test_sorts_by_price_and_deadline(self, client):
        by_price = self.get(client, sort="price").json()["opportunities"]
        amounts = [o["price"]["base_units"] for o in by_price]
        assert amounts == sorted(amounts, reverse=True)
        by_deadline = self.get(client, sort="deadline").json()["opportunities"]
        deadlines = [o["deadline"] for o in by_deadline]
        assert deadlines == sorted(deadlines)

    def test_pages(self, client):
        first = self.get(client, limit="2").json()
        assert len(first["opportunities"]) == 2 and first["next"]
        rest = self.get(client, limit="100", cursor=first["next"]).json()
        ids = [o["issue_id"] for o in first["opportunities"] + rest["opportunities"]]
        assert sorted(ids) == sorted(r.id for r in open_funded())

    def test_an_empty_answer_is_still_an_answer(self, client):
        body = self.get(client, repo="nobody/nothing").json()
        assert (body["opportunities"], body["total"], body["next"]) == ([], 0, None)

    @pytest.mark.parametrize(
        ("query", "error"),
        [
            ({"state": "PAID"}, "unknown_filter"),
            ({"publisher_id": "PUB-1"}, "unknown_filter"),
            ({"claimable": "maybe"}, "bad_filter"),
            ({"sort": "random"}, "bad_filter"),
            ({"min_price": "lots"}, "bad_filter"),
            ({"min_price": "-1"}, "bad_filter"),
            ({"limit": "0"}, "bad_limit"),
            ({"limit": "101"}, "bad_limit"),
            ({"cursor": "nope"}, "bad_cursor"),
        ],
    )
    def test_refuses_what_it_does_not_know(self, client, query, error):
        res = self.get(client, **query)
        assert (res.status_code, res.json()["error"]) == (400, error)

    def test_carry_no_wallet_and_no_contributor(self, client):
        res = self.get(client)
        assert_private(res.text)
        for contributor in store.repo.list_contributors():
            assert contributor.id not in res.text


class TestAReceipt:
    def paid(self):
        return next(r for r in store.repo.list_issues() if r.state is IssueState.PAID)

    def get(self, client: TestClient, settlement_id: str, free_caller: str | None = None):
        headers = dict(EDGE)
        if free_caller:
            headers["X-Misthos-Free-Caller"] = free_caller
        return client.get(f"{PAID}/receipt/{settlement_id}", headers=headers)

    def test_is_signed_and_adds_up(self, client, certificate_key):
        rec = self.paid()
        body = self.get(client, rec.id).json()
        assert certificates.recover(body["canonical"], body["signature"]) == body["signer"]
        assert json.loads(body["canonical"]) == body["receipt"]
        receipt = body["receipt"]
        assert receipt["outcome"] == "released" and receipt["to"] == "contributor"
        gross = int(rec.escrow.amount["base_units"])
        assert receipt["gross"]["base_units"] == gross
        assert receipt["fee"]["base_units"] == rec.platform_fee.base_units
        assert receipt["net"]["base_units"] == rec.paid.base_units
        assert receipt["fee"]["base_units"] + receipt["net"]["base_units"] == gross
        assert receipt["escrow"]["commit_tx"] == rec.escrow.tx_hash
        assert receipt["settled_at"]

    def test_the_release_transfer_is_the_contributors_alone(self, client, certificate_key):
        rec = self.paid()
        anyone = self.get(client, rec.id)
        publisher = self.get(client, rec.id, f"publisher:{rec.publisher_id}")
        mine = self.get(client, rec.id, f"contributor:{rec.contributor_id}")
        assert anyone.json()["receipt"]["escrow"]["settlement_tx"] is None
        assert publisher.json()["receipt"]["escrow"]["settlement_tx"] is None
        assert mine.json()["receipt"]["escrow"]["settlement_tx"] == rec.payout_tx_hash
        assert_private(anyone.text)
        assert_private(publisher.text)
        assert rec.contributor_id not in anyone.text

    def test_a_refund_is_receipted_to_the_publisher(self, client, certificate_key):
        rec = next(r for r in store.repo.list_issues() if r.state is IssueState.REFUNDED)
        receipt = self.get(client, rec.id).json()["receipt"]
        assert receipt["outcome"] == "refunded" and receipt["to"] == "publisher"
        assert receipt["fee"]["base_units"] == 0
        assert receipt["net"] == receipt["gross"]
        assert receipt["escrow"]["settlement_tx"]

    def test_an_issue_that_has_not_settled_has_no_receipt(self, client, certificate_key):
        open_one = open_funded()[0]
        assert (
            self.get(client, open_one.id).status_code,
            self.get(client, open_one.id).json()["error"],
        ) == (404, "not_settled")
        assert self.get(client, "ISS-9999").json()["error"] == "no_such_settlement"

    def test_a_ledger_that_disagrees_with_the_escrow_is_not_receipted(
        self, client, certificate_key
    ):
        rec = self.paid()
        store.chain.tamper(rec.id, EscrowStatus.REFUNDED)
        res = self.get(client, rec.id)
        assert (res.status_code, res.json()["error"]) == (409, "ledger_diverges")
        assert "the escrow" in res.json()["detail"]

    def test_without_a_certificate_key_nothing_is_signed(self, client):
        res = self.get(client, self.paid().id)
        assert (res.status_code, res.json()["error"]) == (503, "certificates_unavailable")


class TestTheScreen:
    def test_is_off_by_default_and_says_why(self, client):
        res = client.post(f"{PAID}/screen", json={"address": "0x" + "1" * 40}, headers=EDGE)
        assert res.status_code == 503
        assert res.json()["error"] == "screening_unavailable"
        assert "legal review" in res.json()["detail"]

    def test_checks_a_wallet_once_switched_on(self, client, monkeypatch):
        monkeypatch.setattr(settings, "paid_screening_enabled", True)
        res = client.post(f"{PAID}/screen", json={"address": "0x" + "1" * 40}, headers=EDGE)
        assert res.status_code == 200
        assert res.json()["outcome"] in {"clear", "hit"}

    @pytest.mark.parametrize(
        "body", [{"address": "nope"}, {}, {"address": "0x" + "1" * 40, "x": 1}]
    )
    def test_refuses_what_is_not_a_wallet(self, client, monkeypatch, body):
        monkeypatch.setattr(settings, "paid_screening_enabled", True)
        res = client.post(f"{PAID}/screen", json=body, headers=EDGE)
        assert res.status_code == 400


class TestWhoIsFree:
    def free(self, client, route: str, params: dict, party_id: str | None) -> bool:
        headers = dict(EDGE)
        if party_id:
            account = account_for(party_id)
            store.repo.save_account(account)
            headers["X-Misthos-Caller-Authorization"] = f"Bearer {sessions.issue(account.address)}"
        res = client.post(
            f"{PAID}/access", json={"route": route, "params": params}, headers=headers
        )
        return res.json()["free"]

    def test_opportunities_are_free_to_anyone_signed_in(self, client):
        assert self.free(client, "opportunities", {}, "CON-1") is True
        assert self.free(client, "opportunities", {}, None) is False

    def test_a_receipt_is_free_to_the_parties_to_it(self, client):
        rec = next(r for r in store.repo.list_issues() if r.state is IssueState.PAID)
        params = {"settlement_id": rec.id}
        other_contributor = next(
            c.id for c in store.repo.list_contributors() if c.id != rec.contributor_id
        )
        other_publisher = next(
            p.id for p in store.repo.list_publishers() if p.id != rec.publisher_id
        )
        assert self.free(client, "receipt", params, rec.contributor_id) is True
        assert self.free(client, "receipt", params, rec.publisher_id) is True
        assert self.free(client, "receipt", params, other_contributor) is False
        assert self.free(client, "receipt", params, other_publisher) is False
        assert self.free(client, "screen", {}, rec.publisher_id) is False
