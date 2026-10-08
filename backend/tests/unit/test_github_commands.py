"""Which lines of a GitHub comment are commands.

A command can commit a publisher's escrow funds, so only the commenter's own prose
counts: not a line shown as code, quoted from another comment or hidden in an HTML
comment. The bodies here are the shapes those take on GitHub.
"""

from __future__ import annotations

import pytest

from misthos.services.github.events import Command, commands_in


def names(body: str) -> list[str]:
    return [c.name for c in commands_in(body)]


class TestWhatIsNotACommand:
    @pytest.mark.parametrize(
        "body",
        [
            "Should I run this?\n\n```\n/misthos approve\n```\n",
            "Example:\n\n    /misthos approve\n",
            "Run `/misthos approve` when you are happy.",
            "```bash\n/misthos approve\n```",
            "~~~\n/misthos approve\n~~~",
            "\t/misthos approve",
            "> /misthos approve",
            "   > /misthos approve",
            "<!-- /misthos approve -->",
            "<!--\n/misthos approve\n-->",
            "/misthos\napprove",
        ],
    )
    def test_code_quotes_comments_and_a_split_line_are_not_commands(self, body: str) -> None:
        assert commands_in(body) == []

    def test_a_fence_closes_only_on_the_same_mark_at_least_as_long(self) -> None:
        assert names("~~~\n```\n/misthos approve\n~~~\n/misthos claim") == ["claim"]
        assert names("````\n```\n/misthos approve\n````\n/misthos claim") == ["claim"]

    def test_a_closing_mark_indented_as_code_inside_the_fence_does_not_close_it(self) -> None:
        assert names("```\n    ```\n/misthos approve\n```\n/misthos claim") == ["claim"]

    def test_a_fence_left_open_runs_to_the_end_of_the_comment(self) -> None:
        assert names("/misthos help\n```\n/misthos approve") == ["help"]

    def test_inline_triple_backticks_do_not_open_a_fence(self) -> None:
        assert names("```/misthos approve```\n/misthos claim") == ["claim"]


class TestWhatIsACommand:
    def test_every_command_line_in_order_with_the_list_after_it(self) -> None:
        body = "/misthos criteria\n- A\n- [x] B\n1. C\n/misthos approve\n- not a criterion"
        assert commands_in(body) == [
            Command("criteria", ("A", "B", "C")),
            Command("approve", ("not a criterion",)),
        ]

    def test_up_to_three_spaces_of_indentation_and_any_case(self) -> None:
        assert names("   /MISTHOS Approve") == ["approve"]

    def test_text_around_a_closed_html_comment_is_still_read(self) -> None:
        assert names("Done. <!-- a note -->\n/misthos claim") == ["claim"]

    def test_a_list_in_code_or_a_quote_is_not_part_of_the_criteria(self) -> None:
        body = "/misthos criteria\n- A\n```\n- B\n```\n> - C\n<!-- - D -->"
        assert commands_in(body) == [Command("criteria", ("A",))]
