from intraday_advisor.screener_provider import fetch_fundamental_candidates, screener_export_url_from_env


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, text):
        self.text = text
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return FakeResponse(self.text)


def test_screener_url_can_be_built_from_screen_id(monkeypatch):
    monkeypatch.delenv("SCREENER_EXPORT_URL", raising=False)
    monkeypatch.setenv("SCREENER_SCREEN_ID", "123")
    monkeypatch.setenv("SCREENER_SLUG_NAME", "quality")
    monkeypatch.setenv("SCREENER_URL_NAME", "quality")
    assert screener_export_url_from_env() == "https://www.screener.in/api/export/screen/?screen_id=123&slug_name=quality&url_name=quality"


def test_fetch_fundamental_candidates_from_export_csv(monkeypatch):
    monkeypatch.delenv("SCREENER_EMAIL", raising=False)
    monkeypatch.delenv("SCREENER_PASSWORD", raising=False)
    csv = "\n".join(
        [
            "Symbol,Mar Cap Rs.Cr.,ROE %,ROCE %,Debt / Eq.,Sales growth %,Prom. Hold. %,Profit growth %,OPM %,EPS Rs.",
            "GOOD,9500,25,24,0.1,25,60,35,18,25",
            "BAD,12000,25,24,0.1,25,60,35,18,25",
        ]
    )
    result = fetch_fundamental_candidates("https://example.test/export.csv", session=FakeSession(csv))
    assert result["Ticker"].tolist() == ["GOOD"]
