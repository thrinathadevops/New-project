"""Keep credentials out of the log files.

The leak
--------
The Angel One SDK logs the **entire request headers** on any HTTP error,
including ``Authorization: Bearer <jwt>`` and ``X-PrivateKey``. One connection
timeout writes a working session token to disk in plaintext. Measured on
2026-11-05: **135 Bearer tokens across 10 files** in ``logs/``, the oldest from
16 October, all world-readable, all sitting in a directory that gets copied
around and pasted into bug reports.

Angel's tokens are short-lived, which lowers the severity and does not remove
it: a token in a log is a token anyone reading the log can replay until it
expires, and the API key beside it does not expire at all.

Why a filter and not a patch
----------------------------
The logging happens inside a third-party package, in an ``except`` block, built
into one preformatted string before it is handed to the logger. There is nothing
to configure and nothing to pass. Patching the SDK means re-patching it on every
upgrade, and the patch silently stops applying the day they rename the method.

A ``logging.Filter`` sits where every message has to pass regardless of who
wrote it, so it keeps working through SDK upgrades and catches any future
logger that makes the same mistake.

One wrinkle, found by testing: the SDK logs through **logzero**, which installs
its own handler rather than propagating to the root logger. Attaching the filter
to the root logger alone therefore does nothing for exactly the messages that
matter -- so :func:`install` walks every live logger and handler.
"""

from __future__ import annotations

import logging
import re

#: What gets replaced. Each pattern keeps a short identifying prefix so a log
#: line stays diagnosable -- you can still see that a Bearer token was sent,
#: which is the part with debugging value, without the token itself.
PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # JWTs, with or without the Bearer prefix. Three base64url segments.
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
     "<JWT-REDACTED>"),
    # Angel's own API key header.
    (re.compile(r"(['\"]?X-PrivateKey['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9]{6,})"),
     r"\1<REDACTED>"),
    # Provider keys. Prefixes are stable and published by each vendor.
    (re.compile(r"\bgsk_[A-Za-z0-9]{10,}"), "gsk_<REDACTED>"),
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9]{10,}"), "sk-or-v1-<REDACTED>"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "sk-<REDACTED>"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{10,}"), "AIza<REDACTED>"),
    # TOTP/PIN fields that appear in login payloads.
    (re.compile(r"(['\"]?(?:password|pin|totp)['\"]?\s*[:=]\s*['\"]?)([^'\",}\s]+)",
                re.IGNORECASE), r"\1<REDACTED>"),
)


def redact(text: str) -> str:
    """Replace every known secret shape in a string."""
    if not text:
        return text
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Rewrites a record in place so no handler can write a secret.

    Returns True always -- this censors, it does not drop. A dropped error
    message would hide the connection failure that produced it, and the failure
    is the thing worth knowing about.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                cleaned = redact(record.msg)
                if cleaned != record.msg:
                    # Collapse args into the message once it has been rewritten,
                    # or %-formatting will fail against the new placeholders.
                    if record.args:
                        try:
                            cleaned = redact(record.msg % record.args)
                        except Exception:  # noqa: BLE001
                            pass
                        record.args = ()
                    record.msg = cleaned
            elif record.args:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
                return True
            if record.args:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        except Exception:  # noqa: BLE001 - logging must never raise
            return True
        return True


_FILTER = SecretRedactingFilter()

#: Set once the record factory is in place, so repeat calls are cheap.
_FACTORY_INSTALLED = False


def _install_record_factory() -> bool:
    """Redact at record CREATION, which closes the ordering hole.

    The filter-walking below only reaches loggers and handlers that **already
    exist**. The Angel SDK's logger is created when ``SmartApi`` is first
    imported, and logzero attaches its handler at the same moment -- so a
    logger born after ``install()`` ran never received the filter, and that is
    precisely the logger that leaks.

    Measured on 2026-11-06, with ``install()`` wired into three entry points
    and the patterns verified to match: a fresh Bearer token still reached
    ``logs/2026-11-06/app.log``. The redaction was correct and simply was not
    attached to the logger that mattered yet.

    ``logging.setLogRecordFactory`` runs for every record created anywhere in
    the process, whatever logger it came from and whenever that logger was
    made, so there is no ordering to get wrong. The factory is chained onto
    whatever was there before rather than replacing it, in case something else
    (pytest, logzero) installed one.
    """
    global _FACTORY_INSTALLED
    if _FACTORY_INSTALLED:
        return False

    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                # Redacted independently of msg, so %-placeholders in the
                # message are untouched: no secret pattern looks like "%s".
                if isinstance(record.args, dict):
                    record.args = {
                        k: (redact(v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:  # noqa: BLE001 -- logging must never raise
            pass
        return record

    logging.setLogRecordFactory(factory)
    _FACTORY_INSTALLED = True
    return True


def install() -> int:
    """Attach the redaction everywhere a record can escape. Returns the count.

    Two mechanisms, because one is not enough:

    1. A **record factory**, which catches every record at creation regardless
       of which logger made it or when that logger was created. This is what
       covers the Angel SDK, whose logger does not exist yet when an entry
       point calls this.
    2. Walking every live logger and handler, which covers anything that
       bypasses the factory -- a handler writing a preformatted string it
       built itself, for instance.

    Safe to call repeatedly: the factory installs once and a filter is only
    added where it is not already present.
    """
    attached = 1 if _install_record_factory() else 0
    root = logging.getLogger()
    targets = [root] + [
        logging.getLogger(name)
        for name in list(logging.root.manager.loggerDict)
        if isinstance(logging.getLogger(name), logging.Logger)
    ]
    for logger in targets:
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
            attached += 1
        for handler in list(getattr(logger, "handlers", []) or []):
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
                attached += 1
    return attached


def scrub_file(path) -> int:
    """Redact secrets already written to one file. Returns lines changed."""
    from pathlib import Path

    target = Path(path)
    try:
        original = target.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return 0
    cleaned = redact(original)
    if cleaned == original:
        return 0
    changed = sum(1 for a, b in zip(original.splitlines(), cleaned.splitlines())
                  if a != b)
    target.write_text(cleaned, encoding="utf-8")
    return changed or 1


def scrub_logs(directory="logs") -> dict:
    """Redact every log file under a directory.

    Rewrites in place rather than deleting: the logs are the only record of what
    the system did on those days, and that history is worth keeping. It is the
    credentials that are not.
    """
    from pathlib import Path

    root = Path(directory)
    result = {"files_scanned": 0, "files_changed": 0, "lines_changed": 0,
              "changed": []}
    if not root.exists():
        result["note"] = f"No {directory} directory."
        return result
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (
                ".log", ".out", ".txt", ".err", ".json"):
            continue
        result["files_scanned"] += 1
        changed = scrub_file(path)
        if changed:
            result["files_changed"] += 1
            result["lines_changed"] += changed
            result["changed"].append(str(path))
    return result
