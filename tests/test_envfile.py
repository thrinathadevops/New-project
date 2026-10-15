"""Tests for .env loading and linting.

These exist because of a real failure: the project read every setting with
``os.getenv`` but nothing ever loaded ``.env``. Angel One credentials were
therefore never present, the broker login never happened, and the dashboard sat
on ESTIMATED data with no error anywhere saying why. Silent misconfiguration is
the worst kind.
"""

from __future__ import annotations

import pytest

from intraday_advisor.envfile import (
    describe_config,
    lint_env_file,
    load_env,
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Keep the developer's real environment out of these tests."""
    for name in ("ANGEL_ONE_API_KEY", "ANGEL_ONE_CLIENT_CODE", "ANGEL_ONE_PIN",
                 "ANGEL_ONE_TOTP_SECRET", "GROQ_API_KEY", "GEMINI_API_KEY",
                 "AGENT_API_KEY", "AGENT_PROVIDER"):
        monkeypatch.delenv(name, raising=False)


def write_env(tmp_path, body: str):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


# ==================================================================== load ==

def test_load_env_sets_variables(tmp_path, monkeypatch):
    path = write_env(tmp_path, "GROQ_API_KEY=abc123\nAGENT_PROVIDER=groq\n")
    assert load_env(path) is True

    import os
    assert os.getenv("GROQ_API_KEY") == "abc123"
    assert os.getenv("AGENT_PROVIDER") == "groq"


def test_existing_environment_wins_by_default(tmp_path, monkeypatch):
    """A value exported in the shell should beat the file, so a single setting
    can be overridden for one run."""
    monkeypatch.setenv("AGENT_PROVIDER", "ollama")
    path = write_env(tmp_path, "AGENT_PROVIDER=groq\n")
    load_env(path)

    import os
    assert os.getenv("AGENT_PROVIDER") == "ollama"


def test_override_forces_the_file_value(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "ollama")
    path = write_env(tmp_path, "AGENT_PROVIDER=groq\n")
    load_env(path, override=True)

    import os
    assert os.getenv("AGENT_PROVIDER") == "groq"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_env(tmp_path / "nope.env") is False


# ==================================================================== lint ==

def test_lint_accepts_a_well_formed_file(tmp_path):
    path = write_env(tmp_path, "# comment\nGROQ_API_KEY=abc\n\nAGENT_PROVIDER=groq\n")
    assert lint_env_file(path) == []


def test_lint_catches_a_value_with_no_name(tmp_path):
    """The exact shape of a key pasted from a web console: '=gsk_...'."""
    path = write_env(tmp_path, "=gsk_secretvalue123\n")
    problems = lint_env_file(path)

    assert len(problems) == 1
    assert "No variable name" in problems[0]["problem"]
    assert "GROQ_API_KEY" in problems[0]["fix"] or "NAME=value" in problems[0]["fix"]


def test_lint_catches_a_space_in_the_name(tmp_path):
    """'API Key=...' is not loadable, and suggests the valid form."""
    path = write_env(tmp_path, "API Key=abc123\n")
    problems = lint_env_file(path)

    assert len(problems) == 1
    assert "not a valid variable name" in problems[0]["problem"]
    assert problems[0]["fix"].startswith("Rename it to API_KEY=")


def test_lint_catches_a_line_with_no_equals(tmp_path):
    path = write_env(tmp_path, "Project Name projects/12345\n")
    problems = lint_env_file(path)

    assert len(problems) == 1
    assert "No '='" in problems[0]["problem"]


def test_lint_never_prints_a_whole_secret(tmp_path):
    """The report has to be safe to paste into a chat or an issue."""
    secret = "gsk_" + "x" * 80
    path = write_env(tmp_path, f"={secret}\n")
    problems = lint_env_file(path)

    assert secret not in problems[0]["text"]
    assert "more chars" in problems[0]["text"]


def test_lint_reports_a_missing_file(tmp_path):
    problems = lint_env_file(tmp_path / "absent.env")
    assert problems and "No .env file" in problems[0]["problem"]


# ================================================================ describe ==

def test_describe_flags_missing_broker_credentials(tmp_path):
    path = write_env(tmp_path, "GROQ_API_KEY=abc\n")
    report = describe_config(path)

    assert report["broker_ready"] is False
    assert "ANGEL_ONE_API_KEY" in report["missing_critical"]
    # The consequence is spelled out, not left for the user to infer.
    assert "ESTIMATED" in report["summary"]


def test_describe_confirms_a_complete_broker_config(tmp_path):
    path = write_env(tmp_path, "\n".join([
        "ANGEL_ONE_API_KEY=a", "ANGEL_ONE_CLIENT_CODE=b",
        "ANGEL_ONE_PIN=c", "ANGEL_ONE_TOTP_SECRET=d",
    ]) + "\n")
    report = describe_config(path)

    assert report["broker_ready"] is True
    assert report["missing_critical"] == []
    assert "live premiums" in report["summary"]


def test_describe_reports_lengths_never_values(tmp_path):
    """A config report must be safe to show on screen or paste into a chat."""
    path = write_env(tmp_path, "GROQ_API_KEY=supersecretvalue\n")
    report = describe_config(path)

    assert report["settings"]["GROQ_API_KEY"]["set"] is True
    assert report["settings"]["GROQ_API_KEY"]["length"] == len("supersecretvalue")
    assert "supersecretvalue" not in str(report)
