"""Publishing from the picker: a connected repository and one of its open issues (#199).

The form no longer asks for an `owner/name` and a number to be typed. The API answers
with the publisher's connected repositories and the issues in the chosen one, and
publishing such an issue marks it on GitHub with the platform label so the label-driven
flow sees the same issue the web app listed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_api import API, sign_in

from misthos.config import settings
from misthos.main import app
from misthos.store import store

LOGIN = "acme-widgets"
REPO = "acme/widgets"
INSTALLATION = 4242


@pytest.fixture(autouse=True)
def fresh_store() -> None:
    store.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def connected(client: TestClient, *, seed: int = 11) -> dict:
    account = sign_in(client, seed, "publisher", "Acme Widgets", LOGIN)
    store.connect_repositories(INSTALLATION, LOGIN, [REPO])
    return account


def issues(client: TestClient, publisher_id: str, repo: str = REPO) -> dict:
    return client.get(
        f"{API}/publishers/{publisher_id}/github/issues", params={"repo": repo}
    )


class TestListingConnectedIssues:
    def test_a_connected_repository_lists_its_open_issues(self, client: TestClient) -> None:
        account = connected(client)

        answer = issues(client, account["party_id"])

        assert answer.status_code == 200, answer.text
        found = answer.json()["issues"]
        assert [i["number"] for i in found] == [77, 78]
        assert found[0]["title"] == "Retry on 503"
        assert found[0]["labels"] == ["bug"]
        assert found[0]["html_url"].endswith("/issues/77")
        assert found[0]["listed_issue_id"] is None

    def test_a_repository_the_publisher_has_not_connected_is_refused(
        self, client: TestClient
    ) -> None:
        account = connected(client)

        answer = issues(client, account["party_id"], repo="other/thing")

        assert answer.status_code == 404, answer.text

    def test_another_publishers_connection_is_not_listed(self, client: TestClient) -> None:
        connected(client)
        other = sign_in(client, 12, "publisher", "Other Co", "other-dev")

        answer = issues(client, other["party_id"])

        assert answer.status_code == 404, answer.text


class TestPublishingAListedIssue:
    def test_the_chosen_issue_is_listed_and_labelled_on_github(
        self, client: TestClient
    ) -> None:
        account = connected(client)

        published = client.post(
            f"{API}/issues",
            json={
                "repo": REPO,
                "number": 77,
                "title": "",
                "summary": "",
                "labels": [],
                "publisher_id": account["party_id"],
                "price": "250",
            },
        )

        assert published.status_code == 201, published.text
        listing = published.json()
        assert listing["number"] == 77
        assert settings.github_label in store.github.issues[(REPO, 77)].labels
        assert any(
            sent.kind == "labels"
            and sent.target == "77"
            and sent.body == settings.github_label
            for sent in store.github.sent
        )

        found = issues(client, account["party_id"]).json()["issues"]
        assert found[0]["listed_issue_id"] == listing["id"]
