# Markets, the LLM token gate, and the pre-market brief

## The gate

An LLM polling overnight costs money and learns nothing — there is no live
price to reason about. So the LLM runs **only inside a window around the
session**, and is off the rest of the time.

```
          08:45              09:15                      15:30
  ─────────┬──────────────────┬──────────────────────────┬────────────
   LLM off │  LLM ON          │  LLM ON                  │  LLM off
           │  pre-market      │  session                 │
           │  brief is built  │  signals reviewed        │
```

The **deterministic rule checklist keeps running regardless** — it is free,
offline and instant. Only the paid/metered part is gated.

The topbar shows the state at all times (`LLM ON` / `LLM in 7m` / `LLM off`),
with the reason on hover. A silent LLM is never a mystery.

### Changing the window

NSE's bell is **09:15** (pre-open runs 09:00–09:15), so the default 30-minute
lead opens the gate at **08:45**. For an 08:30 start, widen the lead:

```ini
AGENT_LEAD_MINUTES=45
```

## Markets

34 exchanges, each with its own timezone, session and lunch break. Pick one
from the dropdown; everything — session state, the gate, the news brief —
follows that market's clock.

| Region | Markets |
|---|---|
| **Asia** | India (NSE/BSE), Singapore, Japan, Hong Kong, China, South Korea, Taiwan, Indonesia, Malaysia, Thailand, Philippines, Vietnam |
| **Middle East** | Saudi, Qatar, Kuwait, Israel, UAE, Egypt |
| **Europe** | UK, Germany, France, Netherlands, Spain, Italy, Switzerland, Sweden, Turkey |
| **Americas** | USA, Canada, Brazil, Mexico |
| **Africa / Oceania** | South Africa, Australia, New Zealand |

Two things a naive implementation gets wrong, both handled:

- **Sunday–Thursday markets.** Saudi, Qatar, Kuwait, Israel and Egypt trade
  Sun–Thu. A Mon–Fri assumption reports Tadawul closed on its busiest day and
  open on its weekend. (UAE moved to Mon–Fri in January 2022.)
- **Lunch breaks.** Most of Asia halts midday — Japan 11:30–12:30, China
  11:30–13:00, Hong Kong 12:00–13:00. Treating that as OPEN means trading a
  frozen tape.

**Holidays are deliberately not modelled.** Exchange holiday calendars change
yearly, and a stale hardcoded list is worse than none — it would confidently
report a closed market as open. The live feed reveals a holiday anyway (no
ticks, stale candles) and the dashboard shows data freshness.

## The pre-market brief

Built **once per trading day**, when the gate opens.

1. **Gathers news** from free RSS — Google News searches scoped to your market,
   plus Economic Times and Moneycontrol for India, plus global feeds for
   central banks, commodities/FX and geopolitics. No API key, no quota.
2. **De-duplicates.** The same wire story appears across a dozen outlets;
   ~90 raw headlines reduce to ~40 distinct ones. Twenty copies of one story
   in a prompt reads as twenty independent confirmations of it.
3. **An LLM reads them** and returns a structured view: a lean
   (`LEAN_UP` / `LEAN_DOWN` / `UNSTABLE` / `UNCLEAR`), confidence, the specific
   drivers with the mechanism for each, what to watch today, and **what would
   make the read wrong**.
4. **Announces itself once** as a notification.

A real example:

> **UNSTABLE 60%** — Oil up, gold down, mixed corporate news; markets likely
> to swing without clear direction.
> `volatile` Crude dips to $105 as Trump mulls easing sanctions — *US policy
> uncertainty pushes oil, but easing tempers it*
> **Watch:** opening gap at 09:15 · **Caveat:** a sharp move beyond $105 tilts
> the index higher

### What it costs

Almost nothing. One call per market per day, cached by (market, trading date).
Without that cache a 10-second refresh loop would re-ask the same question
~2,000 times a session.

`reasoning_effort: "low"` is set on every call — Groq's gpt-oss family spends
completion tokens on hidden reasoning *before* answering, and on a long prompt
it burned 1,598 of 1,600 tokens thinking and returned an empty string. Capping
it fixed that and cut token spend roughly four-fold.

### What it is honest about

News is **weak evidence** about intraday direction. Everything in those
headlines is public and largely priced into the opening gap by the time you
read it. The prompt therefore forbids predictions, requires `UNCLEAR` whenever
the news does not genuinely lean (most days), requires each driver to cite a
specific headline, and bans price targets and buy/sell instructions.

Treat it as **context for sizing and caution** — not a reason to take a trade
the system has not signalled.
