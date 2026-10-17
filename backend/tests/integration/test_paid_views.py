"""The paid views over existing data (#202): the decision log, a reputation, and a
signed escrow attestation, as the core answers them to the edge.

The edge's half (402, payment, replay, settlement) is tested in edge/src/paid.test.ts
against these routes' shapes. Here: every view answers what it says, refuses what it
cannot answer with an error the edge does not charge for, and carries nothing that
links a wallet to a person.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from misthos.auth import sessions
from misthos.config import settings
from misthos.domain.ledger import EscrowStatus
from misthos.main import app
from misthos.services import certificates, paid_views, reputation
from misthos.store import Store, store
from tests.support.accounts import account_for

TOKEN = "edge-core-token-for-tests"
EDGE = {"X-Misthos-Core-Token": TOKEN}
PAID = "/api/v1/internal/paid"
CERTIFICATE_KEY = "0x" + "4c" * 32


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def edge_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "edge_core_token", TOKEN)


@pytest.fixture
def certificate_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    secret = tmp_path / "certificates-2026-10"
    secret.write_text(CERTIFICATE_KEY)
    os.chmod(secret, 0o600)
    monkeypatch.setattr(settings, "secret_store_dir", str(tmp_path))
    monkeypatch.setattr(settings, "certificate_secret_ref", "certificates-2026-10")
    return CERTIFICATE_KEY


def private_strings() -> list[str]:
    """Everything no paid response may carry: contributor wallets and payout transfers."""
    found = [c.wallet.address for c in store.repo.list_contributors()]
    found += [r.payout_tx_hash for r in store.repo.list_issues() if r.payout_tx_hash]
    return [s.lower() for s in found if s]


def assert_private(text: str) -> None:
    lowered = text.lower()
    for secret in private_strings():
        assert secret not in lowered, secret


def every_decision() -> list[str]:
    return [d.id for r in store.repo.list_issues() for d in r.decisions]


class TestTheDecisionLog:
    def test_pages_through_the_whole_log_once_oldest_first(self, client):
        seen: list[dict] = []
        cursor = None
        for _ in range(100):
            query = {"limit": 7, **({"cursor": cursor} if cursor else {})}
            page = client.get(f"{PAID}/decisions", params=query, headers=EDGE).json()
            assert page["limit"] == 7 and page["max_page_size"] == paid_views.DECISIONS_PAGE_MAX
            assert len(page["decisions"]) <= 7
            seen += page["decisions"]
            cursor = page["next"]
            if cursor is None:
                break
        assert sorted(d["id"] for d in seen) == sorted(every_decision())
        assert len({d["id"] for d in seen}) == len(seen), "no decision twice"
        keys = [(d["created_at"], d["id"]) for d in seen]
        assert keys == sorted(keys)

    def test_the_whole_log_goes_further_than_the_free_read(self, client):
        free = client.get("/api/v1/decisions", params={"limit": 3}).json()
        paid = client.get(f"{PAID}/decisions", params={"limit": 200}, headers=EDGE).json()
        assert len(free) == 3
        assert len(paid["decisions"]) == len(every_decision()) > 3

    @pytest.mark.parametrize("limit", [0, -1, paid_views.DECISIONS_PAGE_MAX + 1])
    def test_a_page_is_one_to_the_documented_maximum(self, client, limit):
        res = client.get(f"{PAID}/decisions", params={"limit": limit}, headers=EDGE)
        assert res.status_code == 400
        assert res.json()["error"] == "bad_limit"

    def test_filters_by_issue_actor_action_and_time(self, client):
        rec = store.repo.list_issues()[3]
        mine = client.get(f"{PAID}/decisions", params={"issue_id": rec.id}, headers=EDGE).json()
        in_order = sorted(rec.decisions, key=lambda d: (d.created_at, d.id))
        assert [d["id"] for d in mine["decisions"]] == [d.id for d in in_order]

        agent = client.get(f"{PAID}/decisions", params={"actor": "agent"}, headers=EDGE).json()
        assert agent["decisions"] and {d["actor"] for d in agent["decisions"]} == {"agent"}

        claimed = client.get(f"{PAID}/decisions", params={"action": "claimed"}, headers=EDGE)
        assert {d["action"] for d in claimed.json()["decisions"]} == {"claimed"}

        newest = max(d.created_at for r in store.repo.list_issues() for d in r.decisions)
        after = client.get(
            f"{PAID}/decisions",
            params={"since": (newest + timedelta(seconds=1)).isoformat()},
            headers=EDGE,
        ).json()
        assert after == {**after, "decisions": [], "next": None}
        before = client.get(
            f"{PAID}/decisions",
            params={"until": (newest + timedelta(seconds=1)).replace(tzinfo=None).isoformat()},
            headers=EDGE,
        ).json()
        assert len(before["decisions"]) == min(len(every_decision()), 50)

    @pytest.mark.parametrize(
        ("query", "error"),
        [
            ({"wallet": "0xabc"}, "unknown_filter"),
            ({"page": "2"}, "unknown_filter"),
            ({"cursor": "not-a-cursor"}, "bad_cursor"),
            ({"actor": "robot"}, "bad_filter"),
        ],
    )
    def test_refuses_what_it_does_not_know(self, client, query, error):
        res = client.get(f"{PAID}/decisions", params=query, headers=EDGE)
        assert res.status_code == 400
        assert res.json()["error"] == error

    def test_names_contributors_by_handle_never_by_wallet(self, client):
        res = client.get(f"{PAID}/decisions", params={"limit": 200}, headers=EDGE)
        assert_private(res.text)


def test_every_repository_pages_the_log_the_same_way(every_store: Store):
    everything = every_store.repo.page_decisions(limit=10_000)
    assert [d.id for d in everything] == [
        d.id for d in sorted(everything, key=lambda d: (d.created_at, d.id))
    ]
    assert len(everything) == sum(len(r.decisions) for r in every_store.repo.list_issues())
    first = every_store.repo.page_decisions(limit=3)
    rest = every_store.repo.page_decisions(after=(first[-1].created_at, first[-1].id), limit=10_000)
    assert [d.id for d in first + rest] == [d.id for d in everything]
    issue = everything[0].issue_id
    assert {d.issue_id for d in every_store.repo.page_decisions(issue_id=issue)} == {issue}
    cut = everything[len(everything) // 2].created_at
    assert all(d.created_at >= cut for d in every_store.repo.page_decisions(since=cut))
    assert all(d.created_at < cut for d in every_store.repo.page_decisions(until=cut))


class TestAReputation:
    def test_matches_the_ledger_derived_standing(self, client):
        ledger = reputation.standing(store.repo.list_issues())
        assert ledger, "the seed settles someone"
        for contributor_id, standing in ledger.items():
            body = client.get(f"{PAID}/reputation/{contributor_id}", headers=EDGE).json()
            assert body["reputation"] == standing.reputation
            assert body["settled_issues"] == standing.settled_issues
            assert body["earned"]["base_units"] == standing.earned.base_units
            assert body["score"] == reputation.score_of(standing.reputation)
            assert 0 < body["score"] < 100
            assert body["ranked_contributors"] == len(ledger)

    def test_ranks_against_everyone_with_standing(self, client):
        ledger = reputation.standing(store.repo.list_issues())
        top = max(ledger, key=lambda c: ledger[c].reputation)
        assert client.get(f"{PAID}/reputation/{top}", headers=EDGE).json()["percentile"] == 100

    def test_names_where_the_work_was_done(self, client):
        settled = [
            r for r in store.repo.list_issues() if r.contributor_id and r.state.value == "PAID"
        ]
        rec = settled[0]
        body = client.get(f"{PAID}/reputation/{rec.contributor_id}", headers=EDGE).json()
        assert body["repos"][0]["repo"] == rec.repo
        assert body["repos"][0]["settled_issues"] == 1
        assert {item["label"] for item in body["labels"]} == set(rec.labels)
        assert body["first_settled_at"] and body["last_settled_at"]
        assert body["signals"]["issues_held"] >= 1

    def test_counts_the_verdicts_on_work_the_contributor_holds(self, client):
        rework = next(r for r in store.repo.list_issues() if r.state.value == "REWORK")
        body = client.get(f"{PAID}/reputation/{rework.contributor_id}", headers=EDGE).json()
        assert body["signals"]["verdicts"]["rework"] >= 1
        assert body["signals"]["rework_rounds"] >= 1
        assert body["signals"]["acceptance_rate"] is not None

    def test_a_contributor_with_no_settled_work_has_no_standing(self, client):
        ledger = reputation.standing(store.repo.list_issues())
        idle = next(c.id for c in store.repo.list_contributors() if c.id not in ledger)
        body = client.get(f"{PAID}/reputation/{idle}", headers=EDGE).json()
        assert (body["reputation"], body["score"], body["percentile"]) == (0, 0, None)
        assert body["repos"] == [] and body["first_settled_at"] is None

    def test_an_unknown_contributor_is_not_found(self, client):
        res = client.get(f"{PAID}/reputation/CON-999", headers=EDGE)
        assert res.status_code == 404
        assert res.json()["error"] == "no_such_contributor"

    def test_carries_no_wallet_and_no_handle(self, client):
        for contributor in store.repo.list_contributors():
            res = client.get(f"{PAID}/reputation/{contributor.id}", headers=EDGE)
            assert_private(res.text)
            assert contributor.handle.lower() not in res.text.lower()
            assert "wallet" not in res.text.lower()

    def test_refuses_a_filter(self, client):
        res = client.get(f"{PAID}/reputation/CON-1", params={"since": "2026"}, headers=EDGE)
        assert res.status_code == 400


class TestAnEscrowAttestation:
    def funded(self):
        return next(r for r in store.repo.list_issues() if r.state.value == "FUNDED")

    def test_is_signed_by_the_published_signer_and_says_what_the_chain_holds(
        self, client, certificate_key
    ):
        rec = self.funded()
        body = client.get(f"{PAID}/escrow/{rec.id}/attestation", headers=EDGE).json()
        signer = client.get(f"{PAID}/signer", headers=EDGE).json()["signer"]

        assert certificates.recover(body["canonical"], body["signature"]) == body["signer"]
        assert body["signer"] == signer
        assert json.loads(body["canonical"]) == body["certificate"]

        held = store.chain.commitment(rec.id)
        assert held is not None
        cert = body["certificate"]
        assert cert["issue"]["id"] == rec.id
        assert cert["issue"]["key"].startswith("0x") and len(cert["issue"]["key"]) == 66
        assert cert["amount"]["base_units"] == held.amount.base_units
        assert cert["status"] == "held"
        assert cert["fee_bps"] == held.fee_bps
        assert cert["publisher"]["wallet"].lower() == held.publisher.lower()
        assert cert["escrow"]["commit_tx"] == rec.escrow.tx_hash
        assert cert["escrow"]["network"] == f"eip155:{cert['escrow']['chain_id']}"
        assert cert["deadline"] is not None
        assert body["scheme"] == "eip191-personal-sign"

    def test_a_settled_commitment_is_attested_as_released(self, client, certificate_key):
        rec = next(r for r in store.repo.list_issues() if r.state.value == "PAID")
        res = client.get(f"{PAID}/escrow/{rec.id}/attestation", headers=EDGE)
        assert res.status_code == 200
        assert res.json()["certificate"]["status"] == "released"
        assert_private(res.text)

    def test_a_tampered_signature_does_not_recover_the_signer(self, client, certificate_key):
        body = client.get(f"{PAID}/escrow/{self.funded().id}/attestation", headers=EDGE).json()
        forged = body["canonical"].replace('"status":"held"', '"status":"released"')
        assert certificates.recover(forged, body["signature"]) != body["signer"]

    def test_a_divergence_is_not_certified(self, client, certificate_key):
        rec = self.funded()
        store.chain.tamper(rec.id, EscrowStatus.REFUNDED)
        res = client.get(f"{PAID}/escrow/{rec.id}/attestation", headers=EDGE)
        assert res.status_code == 409
        assert res.json() == {
            "error": "escrow_diverges",
            "detail": "the escrow and the record disagree on status",
        }

    def test_an_issue_with_no_commitment_has_nothing_to_attest(self, client, certificate_key):
        unfunded = next(r for r in store.repo.list_issues() if r.escrow is None)
        res = client.get(f"{PAID}/escrow/{unfunded.id}/attestation", headers=EDGE)
        assert (res.status_code, res.json()["error"]) == (404, "no_commitment")
        res = client.get(f"{PAID}/escrow/ISS-9999/attestation", headers=EDGE)
        assert (res.status_code, res.json()["error"]) == (404, "no_such_issue")

    def test_a_chain_that_cannot_be_read_is_not_certified(
        self, client, certificate_key, monkeypatch
    ):
        from misthos.services.chain import ChainUnavailable

        def down(*_: object) -> None:
            raise ChainUnavailable("the Arc RPC at http://10.0.0.5:8545 timed out")

        monkeypatch.setattr(store.chain, "commitment", down)
        res = client.get(f"{PAID}/escrow/{self.funded().id}/attestation", headers=EDGE)
        assert res.status_code == 502
        assert res.json()["error"] == "chain_unavailable"
        assert "10.0.0.5" not in res.text, "the RPC's address stays inside"

    def test_without_a_certificate_key_nothing_is_signed(self, client):
        res = client.get(f"{PAID}/escrow/{self.funded().id}/attestation", headers=EDGE)
        assert (res.status_code, res.json()["error"]) == (503, "certificates_unavailable")
        assert client.get(f"{PAID}/signer", headers=EDGE).status_code == 503

    def test_the_certificate_key_is_not_the_attestor_key(self, certificate_key):
        """The escrow knows the attestor; a certificate key must be a different one, or a
        certificate could double as a release."""
        from eth_account import Account

        assert certificates.signer() == Account.from_key(certificate_key).address


class TestWhoIsFree:
    def ask(self, client, route: str, params: dict, party_id: str | None):
        headers = dict(EDGE)
        if party_id:
            account = account_for(party_id)
            store.repo.save_account(account)
            headers["X-Misthos-Caller-Authorization"] = f"Bearer {sessions.issue(account.address)}"
        return client.post(
            f"{PAID}/access", json={"route": route, "params": params}, headers=headers
        ).json()

    def test_the_log_is_free_to_anyone_signed_in(self, client):
        assert self.ask(client, "decisions", {}, "CON-1")["free"] is True
        assert self.ask(client, "decisions", {}, None)["free"] is False

    def test_a_reputation_is_free_to_its_own_contributor_only(self, client):
        assert self.ask(client, "reputation", {"contributor_id": "CON-3"}, "CON-3") == {
            "free": True,
            "who": "contributor:CON-3",
        }
        assert self.ask(client, "reputation", {"contributor_id": "CON-3"}, "CON-4")["free"] is False
        assert self.ask(client, "reputation", {"contributor_id": "CON-3"}, "PUB-1")["free"] is False

    def test_an_attestation_is_free_to_the_issue_publisher_only(self, client):
        rec = store.repo.list_issues()[0]
        other = next(p.id for p in store.repo.list_publishers() if p.id != rec.publisher_id)
        params = {"issue_id": rec.id}
        assert self.ask(client, "escrow-attestation", params, rec.publisher_id)["free"] is True
        assert self.ask(client, "escrow-attestation", params, other)["free"] is False
        assert self.ask(client, "escrow-attestation", {"issue_id": "ISS-9"}, other)["free"] is False


def test_reputation_scores_saturate():
    assert reputation.score_of(0) == 0
    assert reputation.score_of(50) == 50
    assert reputation.score_of(150) == 75
    assert reputation.score_of(10_000) == 100


def test_a_cursor_round_trips():
    from misthos.schemas import Decision

    decision = Decision(
        id="DEC-0042",
        issue_id="ISS-1",
        actor="agent",
        action="x",
        rule="y",
        outcome="z",
        created_at=datetime(2026, 10, 1, 12, tzinfo=UTC),
    )
    assert paid_views.decode_cursor(paid_views.encode_cursor(decision)) == (
        decision.created_at,
        "DEC-0042",
    )
