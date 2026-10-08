"""#53: a Team plan is bought without a conversation, and the plans gate what they say."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.config import settings
from misthos.domain import plans
from misthos.domain.money import Usdc
from misthos.main import app
from misthos.schemas import SubscriptionPayment
from misthos.services.billing import TRANSFER_TOPIC, ArcRail, SimulatedRail
from misthos.store import _now, store

API = "/api/v1"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield
    store.rail = SimulatedRail()


def sign_in(client: TestClient, wallet: Wallet) -> str:
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    r = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert r.status_code == 200, r.text
    account = client.post(f"{API}/auth/role", json={"role": "publisher", "name": "Initech"})
    client.post(f"{API}/auth/github/simulate", json={"login": f"initech-{wallet.address[-4:]}"})
    return account.json()["party_id"]


@pytest.fixture
def publisher() -> tuple[TestClient, str]:
    client = TestClient(app)
    return client, sign_in(client, Wallet(71))


def buy_team(client: TestClient, pid: str) -> dict:
    chosen = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
    assert chosen.status_code == 200, chosen.text
    paid = client.post(f"{API}/demo/publishers/{pid}/subscription/pay")
    assert paid.status_code == 200, paid.text
    return paid.json()


class TestThePlans:
    def test_the_plans_are_public(self) -> None:
        rows = {p["id"]: p for p in TestClient(app).get(f"{API}/plans").json()}
        assert rows["team"]["monthly_usdc"] == "249.00" and rows["team"]["self_serve"]
        assert rows["enterprise"]["price_from"] and not rows["enterprise"]["self_serve"]
        assert rows["open"]["minimum_fix_usdc"] == "55.00"
        assert "Single sign-on" in rows["enterprise"]["features"]

    def test_open_does_not_include_the_organisation_tools(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        spend = client.get(f"{API}/publishers/{pid}/spend")
        assert spend.status_code == 402 and "Team plan" in spend.json()["detail"]
        assert client.put(f"{API}/publishers/{pid}/policy", json={}).status_code == 402
        audit = client.get(f"{API}/publishers/{pid}/audit")
        assert audit.status_code == 402 and "Enterprise plan" in audit.json()["detail"]


class TestBuyingTeam:
    def test_choose_pay_and_the_plan_is_on(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        chosen = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"}).json()
        request = chosen["pending"]
        assert chosen["status"] == "pending" and chosen["plan"] == "open"
        assert request["amount_usdc"] == "249.00"
        assert request["payer"] == store.get_publisher(pid).wallet.address  # type: ignore[union-attr]
        assert request["chain_id"] == settings.chain_id

        paid = client.post(f"{API}/demo/publishers/{pid}/subscription/pay").json()
        assert paid["plan"] == "team" and paid["status"] == "active"
        assert paid["pending"] is None and len(paid["payments"]) == 1
        assert "Spend reporting" in paid["features"]
        assert client.get(f"{API}/publishers/{pid}/spend").status_code == 200
        # Team's take rate and floor now price this publisher's issues.
        assert store.get_publisher(pid).tier == "team"  # type: ignore[union-attr]

    def test_a_transaction_that_pays_nothing_is_refused(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
        r = client.post(
            f"{API}/publishers/{pid}/subscription/payment", json={"tx_hash": "0x" + "1" * 64}
        )
        assert r.status_code == 422 and "moved no USDC" in r.json()["detail"]
        assert store.get_publisher(pid).tier == "open"  # type: ignore[union-attr]

    def test_too_little_is_refused(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        request = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"}).json()[
            "pending"
        ]
        assert isinstance(store.rail, SimulatedRail)
        tx = store.rail.send(request["payer"], request["pay_to"], Usdc.from_decimal("24.90"))
        r = client.post(f"{API}/publishers/{pid}/subscription/payment", json={"tx_hash": tx})
        assert r.status_code == 422 and "is due" in r.json()["detail"]

    def test_one_transaction_pays_for_one_period(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        first = buy_team(client, pid)
        tx = first["payments"][0]["tx_hash"]
        client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
        again = client.post(f"{API}/publishers/{pid}/subscription/payment", json={"tx_hash": tx})
        assert again.status_code == 409 and "already paid" in again.json()["detail"]

    def test_renewing_early_adds_a_period_to_the_end(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        first = buy_team(client, pid)
        second = buy_team(client, pid)
        assert len(second["payments"]) == 2
        assert second["payments"][1]["period_start"] == first["period_end"]

    def test_enterprise_is_agreed_not_bought(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        r = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "enterprise"})
        assert r.status_code == 409 and "agreed with us" in r.json()["detail"]
        # And a contract Enterprise organisation cannot downgrade itself by accident.
        assert (
            TestClient(app)
            .post(f"{API}/publishers/PUB-1/subscription", json={"plan": "team"})
            .status_code
            == 409
        )


class TestPeriodsEnd:
    def test_unpaid_is_past_due_then_back_on_open(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        end = store.subscription(pid).period_end if buy_team(client, pid) else None
        assert end is not None
        assert store.sweep_subscriptions(end + timedelta(days=1)) == [pid]
        due = store.subscription(pid, end + timedelta(days=1))
        assert due.status == "past_due" and due.plan == "team" and due.grace_ends is not None
        store.sweep_subscriptions(end + plans.GRACE + timedelta(days=1))
        lapsed = store.subscription(pid)
        assert lapsed.status == "lapsed" and lapsed.plan == "open"

    def test_choosing_open_ends_the_plan_at_its_period_end(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        buy_team(client, pid)
        chosen = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "open"}).json()
        assert chosen["cancel_at_period_end"] and chosen["plan"] == "team"
        end = store.subscription(pid).period_end
        assert end is not None
        store.sweep_subscriptions(end + timedelta(minutes=1))
        assert store.subscription(pid).status == "cancelled"
        assert store.get_publisher(pid).tier == "open"  # type: ignore[union-attr]

    def test_a_contract_plan_is_recorded_by_an_operator(self) -> None:
        out = store.set_contract_plan("PUB-3", "enterprise", _now() + timedelta(days=365))
        assert out.plan == "enterprise" and out.status == "active"


class TestOnArc:
    def test_the_payment_is_read_from_the_receipt(self, publisher) -> None:  # type: ignore[no-untyped-def]
        client, pid = publisher
        request = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"}).json()[
            "pending"
        ]

        def topic(address: str) -> str:
            return "0x" + "0" * 24 + address[2:].lower()

        def answer(_: httpx.Request) -> httpx.Response:
            log = {"address": settings.usdc_address,
                   "topics": [TRANSFER_TOPIC, topic(request["payer"]), topic(request["pay_to"])],
                   "data": hex(249_000_000)}  # fmt: skip
            return httpx.Response(200, json={"result": {"status": "0x1", "logs": [log]}})

        store.rail = ArcRail("https://rpc.example", settings.usdc_address,
                             transport=httpx.MockTransport(answer))  # fmt: skip
        r = client.post(
            f"{API}/publishers/{pid}/subscription/payment", json={"tx_hash": "0x" + "c" * 64}
        )
        assert r.status_code == 200 and r.json()["plan"] == "team"

    def test_outside_the_simulation_there_is_no_pay_button(
        self,
        publisher,
        monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
    ) -> None:
        client, pid = publisher
        monkeypatch.setattr(settings, "simulated", False)
        assert client.post(f"{API}/demo/publishers/{pid}/subscription/pay").status_code == 403
        # Nor can anything be bought until the platform wallet is configured.
        r = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
        assert r.status_code == 503
        assert TestClient(app).get(f"{API}/publishers/{pid}/subscription").status_code == 401


class TestAStaleHashCannotRewindThePeriod:
    """A transaction pays for one period, and a replay is refused however it arrives.

    The old code tried to tell a replay from an activation that never finished by
    comparing period ends, and finished the payment either way. A hash copied out of
    wallet history, replayed against a fresh purchase, therefore overwrote an
    activated period with an older one and reported 200 while doing it.
    """

    def test_replaying_the_first_hash_after_renewing_does_not_move_the_period(
        self, publisher: tuple[TestClient, str]
    ) -> None:
        client, pid = publisher
        first = buy_team(client, pid)
        stale = first["payments"][0]["tx_hash"]

        second = buy_team(client, pid)
        assert len(second["payments"]) == 2
        period_end = second["period_end"]

        # A third purchase, then the stale hash from the first one.
        client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
        replay = client.post(
            f"{API}/publishers/{pid}/subscription/payment", json={"tx_hash": stale}
        )
        assert replay.status_code == 409, replay.text
        assert "already paid" in replay.json()["detail"]

        after = client.get(f"{API}/publishers/{pid}/subscription").json()
        assert after["period_end"] == period_end, "the paid period moved"
        assert len(after["payments"]) == 2, "a payment was written by a replay"

    def test_a_replay_is_refused_even_with_nothing_pending(
        self, publisher: tuple[TestClient, str]
    ) -> None:
        client, pid = publisher
        stale = buy_team(client, pid)["payments"][0]["tx_hash"]
        r = client.post(f"{API}/publishers/{pid}/subscription/payment", json={"tx_hash": stale})
        # Nothing is pending, so it is refused before a replay is even considered.
        assert r.status_code == 409, r.text
        assert "nothing is waiting" in r.json()["detail"]


class TestAContractPlanIsChangedWithUs:
    def test_an_enterprise_organisation_cannot_end_its_own_plan(
        self, publisher: tuple[TestClient, str]
    ) -> None:
        """Open is self-serve too, and it used to return before the guard ran."""
        client, pid = publisher
        store.set_contract_plan(pid, "enterprise", _now() + timedelta(days=365))

        r = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "open"})
        assert r.status_code == 409, r.text
        assert "agreed with us" in r.json()["detail"]

        sub = client.get(f"{API}/publishers/{pid}/subscription").json()
        assert sub["plan"] == "enterprise"
        assert sub["cancel_at_period_end"] is False, "the plan was ended anyway"

    def test_a_team_customer_can_still_choose_open(
        self, publisher: tuple[TestClient, str]
    ) -> None:
        client, pid = publisher
        buy_team(client, pid)
        r = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "open"})
        assert r.status_code == 200, r.text
        assert r.json()["cancel_at_period_end"] is True


class TestAHalfFinishedActivation:
    """The payment row is written before the subscription, so a crash in between
    leaves a period that was paid for and never activated.

    Finishing that row is recovery, and it has to be told apart from a replay of a
    hash that already paid for a *different* period. The difference is whether the
    subscription has already moved past the row.
    """

    def _record_without_activating(self, pid: str, tx: str, amount: str) -> None:
        now = _now()
        store.repo.add_subscription_payment(
            SubscriptionPayment(
                publisher_id=pid,
                plan="team",
                amount_usdc=amount,
                tx_hash=tx,
                period_start=now,
                period_end=now + plans.PERIOD,
                paid_at=now,
            )
        )

    def test_it_is_finished_rather_than_stranded(
        self, publisher: tuple[TestClient, str]
    ) -> None:
        client, pid = publisher
        pending = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"}).json()[
            "pending"
        ]
        assert isinstance(store.rail, SimulatedRail)
        tx = store.rail.send(
            pending["payer"], pending["pay_to"], Usdc.from_decimal(pending["amount_usdc"])
        )
        self._record_without_activating(pid, tx, pending["amount_usdc"])

        # The customer retries the hash they paid with.
        r = client.post(f"{API}/publishers/{pid}/subscription/payment", json={"tx_hash": tx})
        assert r.status_code == 200, r.text
        sub = r.json()
        assert sub["plan"] == "team" and sub["status"] == "active"
        assert len(sub["payments"]) == 1, "recovery wrote a second row"

        # And the hash is spent now: a further replay does not buy another period.
        client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
        again = client.post(f"{API}/publishers/{pid}/subscription/payment", json={"tx_hash": tx})
        assert again.status_code == 409, again.text.lower()

    def test_a_hash_from_an_earlier_period_is_not_recovery(
        self, publisher: tuple[TestClient, str]
    ) -> None:
        """The subscription has moved past the row, so the row was already activated."""
        client, pid = publisher
        first = buy_team(client, pid)  # row written and activated
        spent = first["payments"][0]["tx_hash"]
        buy_team(client, pid)  # the subscription is now a period further on

        client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
        r = client.post(f"{API}/publishers/{pid}/subscription/payment", json={"tx_hash": spent})
        assert r.status_code == 409, r.text
        assert len(client.get(f"{API}/publishers/{pid}/subscription").json()["payments"]) == 2
