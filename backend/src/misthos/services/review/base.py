"""The reviewer protocol."""

from __future__ import annotations

from typing import Protocol

from misthos.domain.review import Judgement, Submitted


class ReviewFailed(Exception):
    """The reviewer could not produce a judgement, such as a model call that failed."""


class Reviewer(Protocol):
    name: str

    def judge(self, submitted: Submitted) -> Judgement:
        """Judge each published criterion against the pull request, with evidence."""
