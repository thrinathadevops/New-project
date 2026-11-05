"""Credentials must not reach a log file.

The Angel SDK logs the entire request headers on any HTTP error, so one
connection timeout wrote a working session token to disk. Measured on
2026-11-05: 135 Bearer tokens across 10 files in `logs/`, the oldest from
16 October.

The leak is inside a third-party `except` block, preformatted before it reaches
the logger, so there is nothing to configure -- hence a filter, which sits where
every record has to pass. One wrinkle these tests pin: the SDK logs through
logzero, which installs its own handler and does not propagate to root, so
attaching to the root logger alone would miss exactly the messages that matter.
"""

from __future__ import annotations

import io
import logging

import pytest

from intraday_advisor.log_redaction import (
    SecretRedactingFilter,
    install,
    redact,
    scrub_file,
    scrub_logs,
)

JWT = "eyJhbGciOiJIUzUxMiJ9.eyJ1c2VybmFtZSI6IkFBQk43NTQyNzQi.TuL5U5wx3m8YqECg"


# ================================================================== patterns ==

def test_a_bearer_token_is_replaced_but_its_presence_still_visible():
    """The fact that a Bearer token was sent has debugging value. The token
    does not."""
    out = redact(f"Headers: {{'Authorization': 'Bearer {JWT}'}}")
    assert JWT not in out
    assert "Bearer" in out
    assert "<JWT-REDACTED>" in out


def test_a_bare_jwt_with_no_bearer_prefix_is_still_caught():
    assert JWT not in redact(f"token={JWT}")


def test_the_angel_api_key_header_is_redacted():
    out = redact("'X-PrivateKey': '4C4IUPxb', 'X-UserType': 'USER'")
    assert "4C4IUPxb" not in out
    assert "X-PrivateKey" in out, "the header name stays, for diagnosis"
    assert "USER" in out, "unrelated fields must survive"


@pytest.mark.parametrize("secret", [
    "gsk_abcdefghijklmnopqrstuvwx",
    "sk-or-v1-0123456789abcdefghij",
    "AIzaSyAbCdEfGhIjKlMnOp",
])
def test_provider_keys_are_redacted(secret):
    assert secret not in redact(f"KEY={secret}")


def test_login_credentials_are_redacted():
    out = redact("{'password': '1234', 'totp': '998877'}")
    assert "1234" not in out
    assert "998877" not in out


def test_ordinary_log_text_is_left_exactly_alone():
    """Over-redaction destroys the logs' usefulness, which is the reason they
    are scrubbed in place rather than deleted."""
    line = "NIFTY spot 24500 score 42 CALL 24550 CE premium 194.0"
    assert redact(line) == line


def test_redacting_twice_changes_nothing_further():
    once = redact(f"Bearer {JWT}")
    assert redact(once) == once


def test_empty_input_is_handled():
    assert redact("") == ""


# =================================================================== filter ==

def test_the_filter_censors_rather_than_drops():
    """A dropped record would hide the connection failure that produced it, and
    the failure is the thing worth knowing about."""
    record = logging.LogRecord("x", logging.ERROR, __file__, 1,
                               f"Bearer {JWT}", None, None)
    assert SecretRedactingFilter().filter(record) is True
    assert JWT not in record.getMessage()


def test_a_secret_passed_as_a_format_argument_is_redacted():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1,
                               "headers %s", (f"Bearer {JWT}",), None)
    SecretRedactingFilter().filter(record)
    assert JWT not in record.getMessage()


def test_a_non_string_message_does_not_break_the_filter():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1,
                               {"a": 1}, None, None)
    assert SecretRedactingFilter().filter(record) is True


def test_nothing_reaches_a_handler_unredacted():
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    logger = logging.getLogger("test_redaction_handler")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    install()
    logger.error("Headers: {'Authorization': 'Bearer %s'}", JWT)
    assert JWT not in buffer.getvalue()


def test_install_reaches_logzero_which_does_not_propagate_to_root():
    """The wrinkle. Attaching to the root logger alone would leave the SDK's
    own handler writing tokens straight to the file."""
    logzero = pytest.importorskip("logzero")
    install()
    assert any(isinstance(f, SecretRedactingFilter)
               for f in logzero.logger.filters)
    assert all(
        any(isinstance(f, SecretRedactingFilter) for f in handler.filters)
        for handler in logzero.logger.handlers
    )


def test_install_is_safe_to_call_repeatedly():
    install()
    before = len(logging.getLogger().filters)
    install()
    install()
    assert len(logging.getLogger().filters) == before


# =================================================================== scrubbing ==

def test_an_existing_file_is_scrubbed_in_place(tmp_path):
    path = tmp_path / "app.log"
    path.write_text(
        f"10:00 ok\n10:01 Headers: {{'Authorization': 'Bearer {JWT}'}}\n"
        f"10:02 spot 24500\n", encoding="utf-8")
    changed = scrub_file(path)
    text = path.read_text(encoding="utf-8")
    assert changed == 1
    assert JWT not in text
    assert "10:00 ok" in text and "spot 24500" in text, (
        "the history is the only record of what the system did; only the "
        "credentials go"
    )


def test_a_clean_file_is_not_rewritten(tmp_path):
    path = tmp_path / "clean.log"
    path.write_text("nothing secret\n", encoding="utf-8")
    assert scrub_file(path) == 0


def test_a_missing_file_is_not_an_error(tmp_path):
    assert scrub_file(tmp_path / "nope.log") == 0


def test_a_whole_directory_is_walked(tmp_path):
    (tmp_path / "day").mkdir()
    (tmp_path / "day" / "app.log").write_text(f"Bearer {JWT}\n",
                                              encoding="utf-8")
    (tmp_path / "out.out").write_text(f"token {JWT}\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text(f"Bearer {JWT}\n", encoding="utf-8")
    result = scrub_logs(tmp_path)
    assert result["files_changed"] == 2
    assert JWT not in (tmp_path / "day" / "app.log").read_text(encoding="utf-8")
    assert JWT in (tmp_path / "notes.md").read_text(encoding="utf-8"), (
        "only log-shaped files are rewritten; a document is not this "
        "function's business"
    )


def test_a_missing_directory_reports_rather_than_raises(tmp_path):
    result = scrub_logs(tmp_path / "absent")
    assert result["files_changed"] == 0
    assert "No" in result.get("note", "")


def test_the_live_log_directory_holds_no_tokens():
    """A standing assertion, not a one-off cleanup: if this fails, something
    has started leaking again."""
    from pathlib import Path

    logs = Path("logs")
    if not logs.exists():
        pytest.skip("no logs directory in this checkout")
    offenders = []
    for path in logs.rglob("*"):
        if path.is_file() and path.suffix.lower() in (".log", ".out", ".txt"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "Bearer eyJ" in text:
                offenders.append(str(path))
    assert not offenders, f"credentials in {offenders}"
