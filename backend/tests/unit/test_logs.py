"""The logging set-up, and what it must keep out of a log line."""

from __future__ import annotations

import logging

import pytest

from misthos.observability.logs import RedactOAuth, configure, redact_oauth


def _access_record(path: str) -> logging.LogRecord:
    """A record shaped like the one uvicorn's access logger writes."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", "GET", path, "1.1", 303),
        exc_info=None,
    )


class TestRedactOAuth:
    def test_the_callback_url_keeps_neither_the_code_nor_the_state(self) -> None:
        line = redact_oauth("/api/v1/auth/github/callback?code=abc123&state=xyz789")
        assert "abc123" not in line and "xyz789" not in line
        assert line == "/api/v1/auth/github/callback?code=[redacted]&state=[redacted]"

    def test_a_query_the_callback_does_not_own_is_left_alone(self) -> None:
        # Only the OAuth callback's two parameters are secrets. Keying on the names
        # alone would rewrite another page's `state`, which a reader may come for.
        line = "/api/v1/issues?state=FUNDED&code=ISS-1001"
        assert redact_oauth(line) == line

    def test_the_order_of_the_parameters_does_not_matter(self) -> None:
        line = redact_oauth("/api/v1/auth/github/callback?state=s&code=c")
        assert line == "/api/v1/auth/github/callback?state=[redacted]&code=[redacted]"

    def test_a_value_at_the_end_of_the_query_is_redacted_whole(self) -> None:
        assert redact_oauth("/x/auth/github/callback?code=abc") == (
            "/x/auth/github/callback?code=[redacted]"
        )

    def test_the_filter_rewrites_an_access_record(self) -> None:
        record = _access_record("/api/v1/auth/github/callback?code=abc&state=xyz")
        assert RedactOAuth().filter(record) is True
        line = record.getMessage()
        assert "abc" not in line and "xyz" not in line
        assert "/api/v1/auth/github/callback" in line

    def test_the_filter_leaves_another_logger_s_message_alone(self) -> None:
        # Anything can contain the words, and only uvicorn's access line holds a URL
        # the callback's secrets could be read out of.
        record = logging.LogRecord(
            name="misthos.store",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="posted code=abc to the API",
            args=(),
            exc_info=None,
        )
        assert RedactOAuth().filter(record) is True
        assert "code=abc" in record.getMessage()


class TestConfigure:
    def test_the_configured_handler_redacts_a_callback_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """What an operator's log actually gets: the filter is on the handler, so a line
        written the way uvicorn writes one comes out redacted."""
        root = logging.getLogger()
        saved, saved_level = root.handlers[:], root.level
        try:
            configure(json_lines=False, level="INFO")
            logging.getLogger("uvicorn.access").info(
                '%s - "%s %s HTTP/%s" %d',
                "127.0.0.1:1",
                "GET",
                "/api/v1/auth/github/callback?code=abc123&state=xyz789",
                "1.1",
                303,
            )
            written = capsys.readouterr().err
        finally:
            root.handlers, root.level = saved, saved_level

        assert "abc123" not in written and "xyz789" not in written
        assert "/api/v1/auth/github/callback" in written
