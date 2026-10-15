# Retired surfaces

These are the six front doors the project used to have. They are kept for
reference and are **no longer maintained**. Everything they did is now in one
place:

    python dashboard_server.py        →  http://localhost:8700

| Retired file | Why |
|---|---|
| `app.py` | Streamlit equities watchlist. Carried its own `confluence_score` — one of four competing scorers. |
| `trading_dashboard.py` | Streamlit. Carried `calculate_combined_score` — another competing scorer. |
| `options_dashboard.py` | Streamlit index options. Superseded by the unified dashboard; its position monitor now persists to the database instead of session state. |
| `options_scanner_dashboard.py` | Streamlit all-strikes scanner. |
| `serve.py` | Plain-HTTP fallback renderer. |
| `launcher.py` | Menu for choosing between the above. Nothing left to choose. |

The analysis modules these imported (`intraday_advisor/*`) are all still live
and are what the new dashboard runs on. Only the UI shells were retired.

**Why they went:** four scorers meant four different answers for the same
setup, and no way to know which to believe. `intraday_advisor/scoring.py` is now
the only one, and `intraday_advisor/engine.py` the only thing that calls it.
Any UI is a renderer of one snapshot and performs no analysis, so a second view
cannot reintroduce a second opinion.

Running them may fail: `index_options.analyze_index_option` no longer returns
the old ad-hoc `confidence`, and `options_scanner.DEFAULT_LOT_SIZES` still holds
a stale BANKNIFTY lot size of 15 (it is 35 — see
`intraday_advisor/ledger/sizing.py:LOT_SIZES`).

Also here: `patch.py` (a one-off script that rewrote `trading_dashboard.py`) and
`test_nse.py` (a scratch connectivity check).

**Still live at the root:** `realtime_server.py` — the equities WebSocket
backend. It is not a competing view (it has no UI) and it already uses the
unified `scoring.py`, so it was left in place for the equities phase. It has not
yet been migrated onto `engine.py`.
