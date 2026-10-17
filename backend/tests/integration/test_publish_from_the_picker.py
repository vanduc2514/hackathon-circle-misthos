"""Publishing from the picker: a connected repository and one of its open issues (#199).

The form no longer asks for an `owner/name` and a number to be typed. The API answers
with the publisher's connected repositories and the issues in the chosen one, and
publishing such an issue marks it on GitHub with the platform label so the label-driven
flow sees the same issue the web app listed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from misthos.config import settings
from misthos.domain.signals import IssueFacts
from misthos.main import app
from misthos.store import store
from tests.support.fake_github import FakeGitHub

API = "/api/v1"
REPO = "acme/widgets"
OTHER_REPO = "acme/gadgets"
PUBLISHER = "PUB-1"
OTHER_PUBLISHER = "PUB-2"
INSTALLATION = 4242


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def github() -> FakeGitHub:
    assert isinstance(store.github, FakeGitHub)
    return store.github


def with_open_issues() -> None:
    github().put_issue(
        IssueFacts(
            repo=REPO,
            number=77,
            title="Retry on 503",
            body="The retry budget ignores jitter.",
            labels=("bug",),
        )
    )
    github().put_issue(
        IssueFacts(
            repo=REPO,
            number=78,
            title="Show the fee split before funding",
            body="A publisher should see what the contributor receives.",
            labels=("feature",),
        )
    )


def connect(publisher: str = PUBLISHER) -> None:
    with_open_issues()
    login = f"{publisher.lower()}-login"
    store.connect_repositories(INSTALLATION, login, [REPO])


def issues(client: TestClient, publisher: str = PUBLISHER, repo: str = REPO) -> dict:
    return client.get(f"{API}/publishers/{publisher}/github/issues", params={"repo": repo})


class TestListingConnectedIssues:
    def test_a_connected_repository_lists_its_open_issues(self, client: TestClient) -> None:
        connect()

        answer = issues(client)

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
        connect()

        answer = issues(client, repo="other/thing")

        assert answer.status_code == 404, answer.text

    def test_another_publishers_connection_is_not_listed(self, client: TestClient) -> None:
        connect(PUBLISHER)
        store.connect_repositories(INSTALLATION + 1, "pub-2-login", [OTHER_REPO])

        answer = issues(client, OTHER_PUBLISHER)

        assert answer.status_code == 404, answer.text


class TestPublishingAListedIssue:
    def test_the_chosen_issue_is_listed_and_labelled_on_github(
        self, client: TestClient
    ) -> None:
        connect()

        published = client.post(
            f"{API}/issues",
            json={
                "repo": REPO,
                "number": 77,
                "title": "",
                "summary": "",
                "labels": [],
                "publisher_id": PUBLISHER,
                "price": "250",
            },
        )

        assert published.status_code == 201, published.text
        listing = published.json()
        assert listing["number"] == 77
        assert settings.github_label in github().issues[(REPO, 77)].labels
        assert any(
            sent.kind == "labels"
            and sent.target == "77"
            and sent.body == settings.github_label
            for sent in github().sent
        )

        found = issues(client).json()["issues"]
        assert found[0]["listed_issue_id"] == listing["id"]
