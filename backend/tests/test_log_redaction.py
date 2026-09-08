"""Secrets never enter a log record.

`app/observability/logging.py` opens by saying so, and Reality Pass §4 names logs as one
of the three places a credential must never reach. Nothing tested it: the dict branch, the
list branch, the secret-field-name branch, the unserialisable-value branch and the
exception branch were all uncovered.

A redaction rule that has never been run is a comment about a redaction rule.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.observability.logging import JsonFormatter, _redact

API_KEY = "sk-ant-api03-verysecretvalue123456"
BEARER = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"


def _record(message: str = "probe", **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="worldgraph.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestValueShapesThatLookLikeCredentials:
    """Matched even under an innocent field name, because that is how they arrive."""

    def test_an_api_key_in_a_message_is_replaced_not_dropped(self):
        """Replaced, so a reviewer can see that something was there."""
        cleaned = _redact(f"calling upstream with {API_KEY} now")
        assert API_KEY not in cleaned
        assert "[redacted]" in cleaned
        assert cleaned.startswith("calling upstream with ")

    def test_a_bearer_token_is_redacted(self):
        cleaned = _redact(f"Authorization: {BEARER}")
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in cleaned
        assert "[redacted]" in cleaned

    def test_ordinary_text_is_untouched(self):
        """Over-redaction makes a log unreadable, which is its own failure."""
        message = "estate_loaded workspace=atlaspay-demo entities=42"
        assert _redact(message) == message

    def test_a_short_key_shaped_string_is_not_a_credential(self):
        """The 12-character floor: `sk-1` in prose is not a key."""
        assert _redact("the sk-1 sample") == "the sk-1 sample"

    def test_redaction_reaches_inside_a_nested_structure(self):
        cleaned = _redact(
            {"request": {"headers": [{"note": f"sent {API_KEY}"}], "path": "/api/world"}}
        )
        blob = json.dumps(cleaned)
        assert API_KEY not in blob
        assert "/api/world" in blob

    def test_a_list_of_values_is_redacted_element_by_element(self):
        cleaned = _redact([f"a {API_KEY}", "b", 42, None])
        assert API_KEY not in json.dumps(cleaned)
        assert cleaned[1] == "b"
        assert cleaned[2] == 42
        assert cleaned[3] is None

    def test_a_tuple_survives_as_a_list_rather_than_being_dropped(self):
        assert _redact(("a", "b")) == ["a", "b"]

    def test_non_string_scalars_pass_through_unchanged(self):
        for value in (0, False, 1.5, None):
            assert _redact(value) is value or _redact(value) == value


class TestFieldNamesThatLookLikeCredentials:
    @pytest.mark.parametrize(
        "field",
        [
            "api_key",
            "apikey",
            "api-key",
            "secret",
            "client_secret",
            "token",
            "skip_token",
            "password",
            "passwd",
            "credential",
            "authorization",
            "bearer",
        ],
    )
    def test_the_whole_value_goes_regardless_of_what_it_is(self, field: str):
        """Name-based redaction does not inspect the value; that is the point."""
        cleaned = _redact({field: "anything at all"})
        assert cleaned[field] == "[redacted]"

    def test_an_innocent_field_keeps_its_value(self):
        assert _redact({"workspace": "atlaspay-demo"}) == {"workspace": "atlaspay-demo"}

    def test_matching_is_case_insensitive_and_by_substring(self):
        cleaned = _redact({"X-API-Key": "v", "anthropicApiKey": "v", "userToken": "v"})
        assert set(cleaned.values()) == {"[redacted]"}


class TestTheFormatterItself:
    def test_a_record_becomes_one_json_object_per_line(self):
        payload = json.loads(JsonFormatter().format(_record("hello")))
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "worldgraph.test"
        assert payload["ts"]

    def test_a_secret_in_the_message_does_not_survive_formatting(self):
        line = JsonFormatter().format(_record(f"posting {API_KEY}"))
        assert API_KEY not in line
        assert "[redacted]" in line

    def test_a_structured_field_named_like_a_secret_is_dropped_whole(self):
        line = JsonFormatter().format(_record("call", api_key=API_KEY))
        payload = json.loads(line)
        assert payload["api_key"] == "[redacted]"
        assert API_KEY not in line

    def test_a_structured_field_carrying_a_secret_value_is_redacted_too(self):
        """An innocent name is not a defence — the value shape is checked as well."""
        line = JsonFormatter().format(_record("call", note=f"used {API_KEY}"))
        assert API_KEY not in line

    def test_an_unserialisable_value_is_stringified_rather_than_crashing_the_log(self):
        """A logging call must never be the thing that takes the process down."""

        class Opaque:
            def __repr__(self) -> str:
                return "<Opaque object>"

        payload = json.loads(JsonFormatter().format(_record("call", thing=Opaque())))
        assert payload["thing"] == "<Opaque object>"

    def test_an_unserialisable_value_carrying_a_secret_is_still_redacted(self):
        class Leaky:
            def __repr__(self) -> str:
                return f"<Client key={API_KEY}>"

        line = JsonFormatter().format(_record("call", client=Leaky()))
        assert API_KEY not in line
        assert "[redacted]" in line

    def test_reserved_logging_internals_are_not_copied_into_the_payload(self):
        payload = json.loads(JsonFormatter().format(_record("hello")))
        for reserved in ("pathname", "lineno", "levelno", "msg", "args"):
            assert reserved not in payload

    def test_an_exception_is_formatted_into_its_own_field(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = _record("failed")
            record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in payload["exception"]

    def test_an_exception_carrying_a_secret_is_not_a_way_around_redaction(self):
        """A traceback is text like any other, and it reaches the same log line."""
        try:
            raise ValueError(f"auth failed for {API_KEY}")
        except ValueError:
            import sys

            record = _record("failed")
            record.exc_info = sys.exc_info()
        line = JsonFormatter().format(record)
        assert API_KEY not in line
