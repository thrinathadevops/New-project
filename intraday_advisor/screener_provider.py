from __future__ import annotations

import os
import re
from io import StringIO

import pandas as pd

from intraday_advisor.fundamentals import apply_fundamental_quality_screen, load_screener_csv, normalize_screener_export


LOGIN_URL = "https://www.screener.in/login/"


def _csrf_token(html: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
    return match.group(1) if match else ""


def screener_export_url_from_env() -> str:
    direct_url = os.getenv("SCREENER_EXPORT_URL", "").strip()
    if direct_url:
        return direct_url
    screen_id = os.getenv("SCREENER_SCREEN_ID", "").strip()
    if not screen_id:
        return ""
    slug_name = os.getenv("SCREENER_SLUG_NAME", "screen").strip()
    url_name = os.getenv("SCREENER_URL_NAME", "screen").strip()
    return f"https://www.screener.in/api/export/screen/?screen_id={screen_id}&slug_name={slug_name}&url_name={url_name}"


def fetch_screener_export(export_url: str | None = None, session=None) -> pd.DataFrame:
    url = export_url or screener_export_url_from_env()
    if not url:
        raise RuntimeError("Set SCREENER_EXPORT_URL or SCREENER_SCREEN_ID to enable automatic Screener fetching.")

    if session is None:
        import requests

        session = requests.Session()

    email = os.getenv("SCREENER_EMAIL", "").strip()
    password = os.getenv("SCREENER_PASSWORD", "").strip()
    if email and password:
        login_page = session.get(LOGIN_URL, timeout=30)
        login_page.raise_for_status()
        csrf = _csrf_token(login_page.text)
        response = session.post(
            LOGIN_URL,
            data={"username": email, "password": password, "csrfmiddlewaretoken": csrf},
            headers={"Referer": LOGIN_URL},
            timeout=30,
        )
        response.raise_for_status()

    response = session.get(url, headers={"Accept": "text/csv, text/html"}, timeout=30)
    response.raise_for_status()
    text = response.text
    if "Welcome back!" in text or "Login to your account" in text:
        raise RuntimeError("Screener export requires login or premium export access. Set SCREENER_EMAIL and SCREENER_PASSWORD.")

    try:
        return load_screener_csv(StringIO(text))
    except Exception:
        tables = pd.read_html(StringIO(text))
        if not tables:
            raise
        return normalize_screener_export(tables[0])


def fetch_fundamental_candidates(export_url: str | None = None, session=None) -> pd.DataFrame:
    return apply_fundamental_quality_screen(fetch_screener_export(export_url=export_url, session=session))

