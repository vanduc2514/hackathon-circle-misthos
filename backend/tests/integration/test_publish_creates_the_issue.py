"""Publishing without a number opens the issue, when the gateway can.

A publisher who is describing work that is not on GitHub yet used to get a number the
platform made up, so what was priced, reviewed and paid pointed at an issue nobody
could open. Where the App is configured the issue is created first, and the price is
put on the issue that now exists.
"""

from __future__ import annotations

import pytest

from misthos.domain.signals import IssueFacts
from misthos.repositories import MemoryRepository
from misthos.schemas import PublishRequest
from misthos.services.github import GitHubError
from misthos.store import Store, UnreadableIssue
from tests.support.fake_github import FakeGitHub

REPO = "acme/ledger"
PUBLISHER = "PUB-1"


class CreatingGitHub(FakeGitHub):
    """An App that can open issues: the fake's reads, plus a real creation."""

    creates_issues = True

    def __init__(self) -> None:
        super().__init__()
        self.created: list[tuple[str, str, str, list[str]]] = []
        self.refuse = False

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        if self.refuse:
            raise GitHubError("Issues are disabled for this repository")
        self.created.append((repo, title, body, labels))
        number = 900 + len(self.created)
        self.put_issue(
            IssueFacts(repo=repo, number=number, title=title, body=body, labels=tuple(labels))
        )
        return number


def store_with(github: CreatingGitHub) -> Store:
    from tests.support.fake_chain import FakeChain
    from tests.support.fake_compliance import FakeIdentity, FakeScreening
    from tests.support.seeded import SeededStore

    return SeededStore(
        MemoryRepository(),
        github=github,
        chain=FakeChain(),
        screening=FakeScreening(),
        identity=FakeIdentity(),
    )


def publish(store: Store, number: int | None = None) -> str:
    return store.publish(
        PublishRequest(
            repo=REPO,
            number=number,
            title="Retry on 503",
            summary="The retry budget ignores jitter.",
            labels=["bug"],
            publisher_id=PUBLISHER,
        )
    )


class TestPublishingDescription:
    def test_no_number_opens_the_issue_and_prices_it(self) -> None:
        github = CreatingGitHub()
        rec = publish(store_with(github))

        # The platform label goes on with the publisher's, so the issue it creates is
        # visible to the label-driven flow as well as to this form (#199).
        assert github.created == [
            (REPO, "Retry on 503", "The retry budget ignores jitter.", ["bug", "misthos"])
        ]
        # The number is GitHub's, and the issue that was priced is the one created.
        assert rec.number == 901

    def test_a_number_the_publisher_gave_is_never_created(self) -> None:
        github = CreatingGitHub()
        rec = publish(store_with(github), number=412)

        assert github.created == []
        assert rec.number == 412

    def test_a_refused_creation_fails_the_publish_rather_than_inventing_a_number(self) -> None:
        github = CreatingGitHub()
        github.refuse = True

        try:
            publish(store_with(github))
        except UnreadableIssue as exc:
            assert "Issues are disabled" in str(exc)
        else:  # pragma: no cover - the refusal is the point of the test
            raise AssertionError("publishing invented a number instead of reporting the refusal")

    def test_linking_an_existing_issue_adds_the_platform_label(self) -> None:
        github = CreatingGitHub()
        store = store_with(github)

        publish(store, number=412)

        assert [(sent.kind, sent.repo, sent.target, sent.body) for sent in github.sent] == [
            ("labels", REPO, "412", "misthos")
        ]

    def test_a_label_that_cannot_be_added_refuses_the_link(self) -> None:
        github = CreatingGitHub()
        store = store_with(github)
        github.fail_writes = True

        with pytest.raises(UnreadableIssue, match="misthos"):
            publish(store, number=412)

        assert store.find_open(REPO, 412) is None

    def test_a_gateway_that_cannot_create_issues_still_makes_a_number_up(self) -> None:
        """The fake holds no repository to open an issue in, so nothing is created."""
        github = CreatingGitHub()
        github.creates_issues = False
        rec = publish(store_with(github))

        assert github.created == []
        assert rec.number > 0
