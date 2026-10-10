"""The Enterprise support commitment (#53): asked in the web app, answered by us,
measured against its deadline, and raised when it is missed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from misthos.main import app
from misthos.services import support as operator
from misthos.store import Store, store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"
ASK = {"severity": "urgent", "subject": "Payout held", "body": "ISS-1006 has not released."}


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


def publisher(client: TestClient, login: str = "acme-bot") -> str:
    client.post(f"{API}/auth/github/simulate-signin", json={"login": login})
    r = client.post(f"{API}/auth/role", json={"role": "publisher", "name": "Acme"})
    assert r.status_code == 201, r.text
    return r.json()["party_id"]


def enterprise(client: TestClient) -> str:
    pid = publisher(client)
    store.set_contract_plan(pid, "enterprise", datetime.now(UTC) + timedelta(days=365))
    return pid


class TestAsking:
    def test_the_commitment_is_in_the_enterprise_plan_only(self) -> None:
        client = TestClient(app)
        pid = publisher(client)
        r = client.post(f"{API}/publishers/{pid}/support", json=ASK)
        assert r.status_code == 402 and "Enterprise" in r.json()["detail"]
        out = client.get(f"{API}/publishers/{pid}/support").json()
        assert out["entitled"] is False and out["requests"] == []
        assert "4 hours" in out["commitment"]

    def test_a_request_says_when_its_first_response_is_due(self) -> None:
        client = TestClient(app)
        pid = enterprise(client)
        r = client.post(f"{API}/publishers/{pid}/support", json=ASK)
        assert r.status_code == 201, r.text
        opened = r.json()
        due = datetime.fromisoformat(opened["respond_by"]) - datetime.fromisoformat(
            opened["opened_at"]
        )
        assert due == timedelta(hours=4)
        assert opened["opened_by"] == "acme-bot" and not opened["overdue"]
        listed = client.get(f"{API}/publishers/{pid}/support").json()["requests"]
        assert [x["id"] for x in listed] == [opened["id"]]

    def test_staff_through_single_sign_on_are_named_as_who_asked(self) -> None:
        owner, staff = TestClient(app), TestClient(app)
        pid = enterprise(owner)
        owner.put(
            f"{API}/publishers/{pid}/sso",
            json={"issuer": "https://idp.acme.example", "client_id": "m",
                  "client_secret_ref": "acme-oidc", "domains": ["acme.example"]},
        )  # fmt: skip
        staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "dana@acme.example"})
        r = staff.post(f"{API}/publishers/{pid}/support", json={**ASK, "severity": "normal"})
        assert r.status_code == 201 and r.json()["opened_by"] == "dana@acme.example"

    def test_an_issue_named_must_be_the_organisations_own(self) -> None:
        client = TestClient(app)
        pid = enterprise(client)
        other = next(r.id for r in store.list_issues() if r.publisher_id != pid)
        r = client.post(f"{API}/publishers/{pid}/support", json={**ASK, "issue_id": other})
        assert r.status_code == 404

    def test_after_the_plan_lapses_the_record_stays_and_new_requests_stop(self) -> None:
        client = TestClient(app)
        pid = enterprise(client)
        client.post(f"{API}/publishers/{pid}/support", json=ASK)
        store.set_contract_plan(pid, "open", datetime.now(UTC) + timedelta(days=1))
        assert len(client.get(f"{API}/publishers/{pid}/support").json()["requests"]) == 1
        assert client.post(f"{API}/publishers/{pid}/support", json=ASK).status_code == 402


class TestAnswering:
    def test_an_answer_in_time_is_recorded_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = TestClient(app)
        pid = enterprise(client)
        sid = client.post(f"{API}/publishers/{pid}/support", json=ASK).json()["id"]

        assert operator.main(["answer", sid, "--by", "Ana at Misthos", "--message", "On it."]) == 0
        assert f"{sid} answered in time" in capsys.readouterr().out
        (row,) = client.get(f"{API}/publishers/{pid}/support").json()["requests"]
        assert row["responder"] == "Ana at Misthos" and row["first_response"] == "On it."
        assert row["overdue"] is False

        assert operator.main(["answer", sid, "--by", "Bo", "--message", "Again"]) == 1
        assert "was answered by Ana at Misthos" in capsys.readouterr().err

    def test_the_operator_lists_what_is_late(self, capsys: pytest.CaptureFixture[str]) -> None:
        pid = enterprise(TestClient(app))
        long_ago = datetime.now(UTC) - timedelta(days=2)
        late = store.open_support(pid, severity="urgent", subject="Late", body="x", by="a",
                                  now=long_ago)  # fmt: skip
        store.open_support(pid, severity="normal", subject="Fresh", body="x", by="a")
        operator.main(["list", "--overdue"])
        out = capsys.readouterr().out
        assert late.id in out and "LATE" in out and "Fresh" not in out


class TestMissingIt:
    def test_the_sweeper_raises_a_missed_deadline_once(self) -> None:
        pid = enterprise(TestClient(app))
        opened = datetime.now(UTC) - timedelta(hours=5)
        late = store.open_support(pid, severity="urgent", subject="Held", body="x", by="a",
                                  now=opened)  # fmt: skip
        assert sweep_once(store).support_overdue == [late.id]
        assert sweep_once(store).support_overdue == []
        (row,) = store.support_requests(pid)
        assert row.overdue and row.alerted_at is not None

    def test_an_answer_before_the_deadline_is_never_raised(self) -> None:
        pid = enterprise(TestClient(app))
        opened = datetime.now(UTC) - timedelta(hours=5)
        req = store.open_support(pid, severity="urgent", subject="Held", body="x", by="a",
                                 now=opened)  # fmt: skip
        in_time = opened + timedelta(hours=1)
        store.answer_support(req.id, responder="Ana", message="Done", now=in_time)
        assert sweep_once(store).support_overdue == []
        assert not store.support_requests(pid)[0].overdue


class TestKeepingIt:
    def test_a_request_and_its_answer_come_back_from_every_store(self, every_store: Store) -> None:
        opened = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
        req = every_store.open_support("PUB-1", severity="normal", subject="Q", body="b",
                                       by="dana@acme.example", now=opened)  # fmt: skip
        every_store.answer_support(req.id, responder="Ana", message="A",
                                   now=opened + timedelta(days=4))  # fmt: skip
        (row,) = every_store.support_requests("PUB-1", now=opened + timedelta(days=5))
        assert row.respond_by == datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
        assert row.first_response_at == opened + timedelta(days=4)
        assert row.overdue  # answered on Tuesday, due on Monday
