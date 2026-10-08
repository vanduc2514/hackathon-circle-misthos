"""The review agent: judges a pull request against the published acceptance criteria.

`ClaudeReviewer` reads the diff and is used once `MISTHOS_ANTHROPIC_API_KEY` is set.
`RuleReviewer` judges only what the file list proves, costs nothing, and is what the
simulation and the regression corpus run. Either way the verdict follows from the
judgement by the rules in domain/review.py.
"""

from __future__ import annotations

from misthos.services.review.base import Reviewer, ReviewFailed
from misthos.services.review.rules import RuleReviewer


def build_reviewer(
    api_key: str,
    model: str,
    *,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
    api_url: str,
) -> Reviewer:
    if not api_key:
        return RuleReviewer()
    from misthos.services.review.claude import ClaudeReviewer

    return ClaudeReviewer(
        api_key,
        model,
        input_usd_per_mtok=input_usd_per_mtok,
        output_usd_per_mtok=output_usd_per_mtok,
        api_url=api_url,
    )


__all__ = ["ReviewFailed", "Reviewer", "RuleReviewer", "build_reviewer"]
