"""Load ``.env`` into the process environment, and say so when it is wrong.

Why this module exists
----------------------
The project has always read configuration with ``os.getenv`` -- Angel One
credentials, the feed selector, score weights -- but **nothing ever loaded the
``.env`` file**. Every one of those lookups returned ``None`` unless the
variable happened to be exported in the shell, which silently meant: no broker
login, so no live premiums, so the dashboard sat on ESTIMATED data with no error
anywhere explaining why.

A missing credential should be loud. :func:`describe_config` exists so the
dashboard and the CLI can show exactly what is configured and what is not,
instead of degrading quietly.

Format pitfalls this catches
----------------------------
``.env`` is parsed as ``NAME=value`` per line. These do **not** work and are
easy to produce by pasting from a web console:

* ``=gsk_abc123``      -- no variable name
* ``API Key=abc123``   -- a space in the name
* ``Project Name foo`` -- no ``=`` at all

:func:`lint_env_file` reports those by line number rather than letting them be
skipped in silence.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Project root = the directory containing this package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

_VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_loaded = False


def load_env(path: str | Path | None = None, *, override: bool = False) -> bool:
    """Load ``.env`` into ``os.environ``. Idempotent and safe to call anywhere.

    Returns True if a file was found and read. Existing environment variables
    win by default, so a value exported in the shell still beats the file --
    which is what you want when overriding a single setting for one run.
    """
    global _loaded
    target = Path(path) if path else DEFAULT_ENV_PATH
    if _loaded and path is None:
        return True
    if not target.exists():
        return False

    try:
        from dotenv import load_dotenv
    except ImportError:
        _manual_load(target, override=override)
        _loaded = True
        return True

    load_dotenv(target, override=override)
    _loaded = True
    return True


def _manual_load(path: Path, *, override: bool) -> None:
    """Minimal parser, used only when python-dotenv is not installed.

    Deliberately strict in the same way dotenv is: a line without a valid
    ``NAME=`` is skipped rather than guessed at.
    """
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not _VALID_NAME.match(name):
            continue
        value = value.strip().strip('"').strip("'")
        if override or name not in os.environ:
            os.environ[name] = value


def lint_env_file(path: str | Path | None = None) -> list[dict]:
    """Report lines that look like settings but will never load.

    Returns one entry per problem with the line number, the offending text
    (truncated, never the full secret) and what to change it to.
    """
    target = Path(path) if path else DEFAULT_ENV_PATH
    if not target.exists():
        return [{
            "line": 0, "text": str(target), "problem": "No .env file found.",
            "fix": "Create one next to dashboard_server.py.",
        }]

    problems: list[dict] = []
    for number, raw in enumerate(
        target.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            problems.append({
                "line": number, "text": _mask(line),
                "problem": "No '=' — this is not a setting and will be ignored.",
                "fix": "Either delete it or write it as NAME=value.",
            })
            continue

        name = line.partition("=")[0].strip()
        if not name:
            problems.append({
                "line": number, "text": _mask(line),
                "problem": "No variable name before '='.",
                "fix": "Add a name, e.g. GROQ_API_KEY=...",
            })
        elif not _VALID_NAME.match(name):
            suggestion = re.sub(r"[^A-Za-z0-9_]", "_", name).upper().strip("_")
            problems.append({
                "line": number, "text": _mask(line),
                "problem": f"'{name}' is not a valid variable name "
                           f"(letters, digits and underscores only, not starting "
                           f"with a digit).",
                "fix": f"Rename it to {suggestion}=...",
            })
    return problems


def _mask(line: str, keep: int = 12) -> str:
    """Show enough of a line to locate it, never enough to leak a secret."""
    if len(line) <= keep:
        return line
    return f"{line[:keep]}…({len(line) - keep} more chars)"


# --------------------------------------------------------------------------- #
# Config report
# --------------------------------------------------------------------------- #

# name -> (what it is, whether the system is crippled without it)
KNOWN_SETTINGS = {
    "ANGEL_ONE_API_KEY": ("Angel One API key", True),
    "ANGEL_ONE_CLIENT_CODE": ("Angel One client code", True),
    "ANGEL_ONE_PIN": ("Angel One PIN", True),
    "ANGEL_ONE_TOTP_SECRET": ("Angel One TOTP secret", True),
    "GROQ_API_KEY": ("Groq API key (free-tier agent)", False),
    "GEMINI_API_KEY": ("Gemini API key (free-tier agent)", False),
    "AGENT_API_KEY": ("Generic agent API key override", False),
    "AGENT_PROVIDER": ("Which agent to run", False),
    "AGENT_MODEL": ("Agent model name", False),
    "AGENT_BASE_URL": ("Agent endpoint override", False),
    "REALTIME_FEED": ("Market data feed selector", False),
    "SCORE_WEIGHTS_FILE": ("Calibrated score weights", False),
}


def describe_config(path: str | Path | None = None) -> dict:
    """What is configured, what is missing, and what that costs you."""
    found = load_env(path)
    settings = {}
    for name, (label, critical) in KNOWN_SETTINGS.items():
        value = os.getenv(name)
        settings[name] = {
            "label": label, "critical": critical,
            "set": bool(value),
            # Never the value. Length alone is enough to spot a truncated paste.
            "length": len(value) if value else 0,
        }

    missing_critical = [
        name for name, info in settings.items()
        if info["critical"] and not info["set"]
    ]
    broker_ready = not missing_critical

    return {
        "env_file": str(Path(path) if path else DEFAULT_ENV_PATH),
        "env_file_found": found,
        "settings": settings,
        "problems": lint_env_file(path),
        "broker_ready": broker_ready,
        "missing_critical": missing_critical,
        "summary": (
            "Angel One is configured — live premiums available during market hours."
            if broker_ready else
            f"Angel One is NOT configured ({len(missing_critical)} value(s) "
            f"missing), so the feed stays on ESTIMATED data and nothing is "
            f"tradable or recordable as real."
        ),
    }
