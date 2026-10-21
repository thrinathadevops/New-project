# Market direction and agents

Two additions that answer different questions. Neither predicts anything.

## Which way is the market leaning?

`intraday_advisor/regime.py` — shown at the top of the dashboard.

It does **not** forecast. Nothing does, and a module claiming to would be
selling the shape of a forecast without its substance. It answers the question
you can act on: *what state is the market in, how much do the timeframes agree,
and what would make this read wrong?*

Six independent inputs:

| Input | What it adds |
|---|---|
| **4 timeframes** (5m / 15m / 1h / daily) | Alignment. Slower frames weigh more — a daily downtrend is the context a 5-minute rally moves inside, not the reverse. |
| **India VIX** | Rising VIX accompanies falling markets far more often than rising ones. High VIX = stops hit by noise, rich premiums. |
| **Breadth** (12 heavyweights vs their own EMA20) | The index is cap-weighted, so a few giants can carry it while most stocks fall. That divergence is the classic late-move warning. |
| **Global cues** (S&P, Nasdaq, Nikkei, Hang Seng) | The Indian open follows these more often than yesterday's Indian close. |
| **Key levels** | Previous day high/low/close — where other traders' orders rest. |
| **Range position** | At an extreme or mid-range decides whether breakout or fade is sane. |

Output: a regime (`TRENDING_UP` / `TRENDING_DOWN` / `RANGE` / `VOLATILE`), a
directional lean, a conviction figure, and — the part most systems omit —
**what would change its mind**. A read you cannot falsify is a horoscope.

**Conviction is agreement among inputs, not a probability of being right.**
Nothing here knows that number; only the ledger can estimate it.

Cached for 5 minutes: it is ~20 network calls and none of it changes meaningfully
inside that window.

## Agents

`intraday_advisor/agents/` — a second opinion on every proposed trade.

### The one rule

**An agent may only hold a trade back. It can never create or improve one.**

That constraint is the entire design. An agent that could turn WAIT into BUY, or
raise a score, would quietly become the system's real strategy — an unmeasured,
unbacktested, non-deterministic one that happens to sound confident. Every
verdict is clamped to `PROCEED` / `CAUTION` / `VETO` *after* parsing, so a model
ignoring its instructions cannot widen its own authority. An unrecognised stance
becomes `CAUTION`, never `PROCEED`: a broken agent must not read as approval.

### Free agents, in order of what I'd actually use

**1. Rule checklist — the default. Free, offline, instant, deterministic.**

No API, no key, no network. It encodes the checks a disciplined trader runs
before every entry — the ones skipped exactly when skipping them costs most,
because the setup looks good and the hand is already moving.

It **vetoes** when the trade is structurally unsound: breakeven above target,
net R:R below 1, cost drag above 5%, one stop-out taking 35%+ of the account,
fighting an aligned market trend, estimated data, past square-off, or a measured
negative expectancy. It **cautions** on contextual problems: range regime, high
VIX, breadth divergence, global cues against, late session, or an edge that is
still unmeasured.

Being deterministic matters more than it sounds: run it twice on the same brief
and you get the same verdict, so a veto can be argued with rather than obeyed.

**2. A local LLM — free and offline.**

```bash
# install from ollama.com, then
ollama pull llama3.1:8b
set AGENT_PROVIDER=ollama
```

**3. OpenRouter — one key, many models, 18 of them free.** Probably the best
option if you already have a key.

```ini
AGENT_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
```

Why it suits this project in particular: **every single model id hardcoded in
this file had already been retired within weeks of being written** — Groq
dropped `llama-3.3-70b-versatile`, `gemini-2.0-flash` never existed,
`gemini-2.5-flash` became unavailable to new users, and all five of the
OpenRouter ids I first chose were gone by the time I tested them. The default
is therefore `openrouter/free`, OpenRouter's **auto-router across free models**,
which picks whatever is currently up. Churn stops being your problem.

It also routes around per-model outages — the thing that made Gemini's bigger
flash models unusable here (constant 503s on the free tier).

**The default is a named model, not the auto-router.** `openrouter/free` picks
a model per request, and was observed sending a structured-JSON task to a
content-safety classifier, which replied `User Safety: safe`. For output that
must parse, a known general model beats a lucky dip. The router stays in the
preference list as a fallback.

Measured against the live free tier: `gemma` models 429, `thinkingmachines`
is 403 (restricted), and `nemotron-3.5-lightning` answered but took **92
seconds** -- unusable in a refresh loop however free it is. The default is
`nvidia/nemotron-3-super-120b-a12b:free`, which returns clean JSON in ~7s.

**Free means free, verified from pricing, not naming.** Discovery filters the
catalogue by the published `pricing` fields rather than trusting the `:free`
suffix, and a model with *no* pricing block is treated as paid. Free-but-wrong
models (audio, content-safety classifiers, code-only) are excluded too — free
does not mean suitable for a risk verdict.

Free tiers are rate-limited (roughly 20 requests/minute, 50/day without
credits). The per-candle caching below keeps usage to about 2 calls per
5 minutes, which fits comfortably.

**4. Groq and Gemini direct — also wired and tested.**

Put the keys in `.env` (which **is** now loaded — it never was before):

```ini
AGENT_PROVIDER=groq          # or gemini
GROQ_API_KEY=gsk_...         # console.groq.com
GEMINI_API_KEY=...           # aistudio.google.com
```

Each provider reads **its own** key, so both can sit in `.env` and switching is
a one-word change. `AGENT_API_KEY` overrides whichever is active.

Check it with `python ledger_cli.py doctor --test-agent`.

### Model names go stale — this heals itself

Both providers had already retired the model names I first shipped
(`llama-3.3-70b-versatile` gone from Groq; `gemini-2.0-flash` never existed).
So the agent now:

1. uses a current default (`openai/gpt-oss-20b`, `gemini-flash-lite-latest`);
2. on a 400/404, reads the replacement the provider **names in its own error**
   ("please use models/X") and retries;
3. failing that, lists `/models` and picks the best available chat model;
4. on 429/503 (free tiers overload per-model), retries once then falls through
   to the next preferred model.

Two details worth knowing, both found by testing rather than reading docs:

- **Groq's gpt-oss models spend completion tokens on hidden reasoning before
  answering.** At `max_tokens=400` the entire budget went to reasoning and the
  reply came back empty — which looks exactly like a parse failure. The budget
  is now 1500, and an empty answer falls back to the `reasoning` field.
- **Gemini's larger flash models 503 constantly on the free tier.**
  `gemini-flash-lite-latest` is the default for that reason: a structured risk
  verdict does not need the bigger model, it needs an answer.

### Cost control

An LLM verdict is cached per **candle**, not per refresh — keyed on
(index, bar_time, direction, score band, data mode). Without that, a 10-second
refresh would make 12 calls a minute, burn a free tier within the hour, add
latency to every snapshot, and keep re-asking a question whose inputs had not
moved. A 5-minute candle means roughly 2 calls per 5 minutes.

### Do not depend on one provider

```ini
AGENT_PROVIDER=auto
```

This is the recommended setting. It uses **every provider that has a key** and
falls through on failure, so a rate limit on one does not drop the LLM layer.
Free tiers rate-limit constantly -- that is what makes them free -- so pinning a
single provider is a design flaw, not bad luck.

Order is by **measured** round-trip latency, since this runs in a refresh loop:

| Provider | Latency | Role |
|---|---|---|
| Groq | ~1.8s | primary |
| Gemini | ~2.2s | fast second |
| OpenRouter | ~5-8s | slowest, but the widest fallback |

OpenRouter fronts many models and routes around individual provider outages,
which makes it a better **safety net** than a first choice. An earlier version
put it first on the assumption that breadth beat speed; measurement disagreed.

Later providers in the chain get longer timeouts. By the time the chain reaches
its last resort the fast options have already burned part of the budget --
OpenRouter answered in 7s alone but 29s after two failures, and a flat 20s
would have thrown the fallback away.

Proven behaviour, with Groq's key deliberately broken:

```
1. normal           -> groq answered in 1.8s
2. groq 401         -> fell through to gemini, verdict returned, and recorded
                       "Answered by gemini after 1 failed: groq (HTTP 401)"
3. all unreachable  -> CAUTION, degraded=true, never a silent PROCEED
```

The provider that last answered is tried first next time, so a dead one is not
re-probed at the head of the queue on every call. An explicit order also works:

```ini
AGENT_PROVIDER=openrouter,groq,gemini
```

Keyless local providers (Ollama, LM Studio) are **probed** rather than assumed:
they pass a key check trivially, and reporting one as available with nothing
listening makes the status panel lie.

### Configuration

```bash
AGENT_PROVIDER=auto       # recommended: all keyed providers, with fallback
AGENT_PROVIDER=rules      # checklist only
AGENT_PROVIDER=ollama     # checklist + free local LLM
AGENT_PROVIDER=groq       # checklist + free-tier hosted
AGENT_PROVIDER=none       # off

AGENT_MODEL=llama3.1:8b
AGENT_BASE_URL=http://localhost:11434/v1
AGENT_API_KEY=...
```

**The rule checklist always runs**, whatever else is configured. An LLM is an
addition, never a replacement: it is slower, non-deterministic, and the
component most likely to be unavailable exactly when the market is busy.

Verdicts combine **pessimistically** — one objection is enough. Averaging would
let a confident-but-wrong model talk over a correct objection, which is the
failure an agent layer exists to prevent.

A failed LLM call is a `CAUTION` naming the failure, never a silent `PROCEED`.
Temperature is 0 so the same brief tends to produce the same verdict; a risk
check that changes its mind at random cannot be tested.

### Claude

Not wired as a direct provider yet — you asked for free agents first. Two routes
when you want it:

1. **Through OpenRouter**, which already serves Anthropic models — a one-line
   change (`AGENT_MODEL=anthropic/claude-...`) with no new key and no new code.
   The cheapest way to find out whether a stronger model earns its cost here.
2. **Direct Anthropic API**, which is better for prompt caching and cost control
   once it is proven worth running.

Worth doing in that order. Worth doing once the free ones have
shown whether the LLM layer adds anything the checklist does not: if a local
model and the rules always agree, paying for a better model buys nothing.

## What this does and does not buy you

It does not make a losing strategy profitable, and it does not know which way
the market will go. What it does is refuse the trades that are unsound *before*
direction even matters — the ones where costs exceed the target, the account
cannot survive the stop, or you are buying into a trend running the other way.

Those refusals are measurable. Every vetoed setup is still recorded by the
ledger, so in time you can ask the only question that settles it: **would the
vetoed trades have made money?** If the agent is blocking winners, that shows up
and the thresholds change.

---

# What was missing (2026-10-21, after researching the field)

You asked what the app was missing to find a good trade. The honest answer was
not another indicator. `scoring.py` already carries **37 weights across 32
price components** — SuperTrend, PSAR, MACD, ADX, ribbons, Chandelier, candle
patterns, VSA. Every one of them is a function of the same OHLC series, so they
are not 32 pieces of evidence; they are 32 views of one piece. That is why the
score saturates, why ranking was once a 30-way tie, and why `validation.py`
measured no usable edge in it. **A 33rd function of the same prices adds
nothing.**

Three things were genuinely missing, and all three attack the constraint that
actually binds — cost, and knowing what is real.

## 1. The most important number in the system was a guess

`ledger/costs.py` modelled the bid-ask spread as `half_spread_pct = 0.0025` —
a flat 0.25% per leg, the same for a liquid ATM weekly and a dead far-month
wing. That constant decides whether any trade clears its costs, so it decides
every verdict the app gives. It was never measured. The module even provided
`with_measured_spread()` for exactly this purpose and **nothing ever called
it.**

Meanwhile `nse_option_chain.py` was already asking Angel One for `FULL` mode
market data on every refresh, and FULL mode returns **five levels of bid and
ask depth per contract**. The parser kept `ltp` and `oi` and threw the book
away.

So the cost that beats every edge was assumed, while the data to measure it
arrived and was discarded.

**`intraday_advisor/microstructure.py`** closes that. It parses the depth, and
the spread is now measured per contract and flows into the cost model, the
breakeven, the cost drag and the ranking.

Why it matters at these premium levels: a Rs 2 spread is routine on an index
option. On a Rs 160 premium that is **1.25% round trip — five times the
modelled constant**, and on its own larger than any edge this project has ever
measured. The same Rs 2 on a Rs 12 wing is 16.7%.

Three details, each there because the feed needs it:

- **`walk()` instead of a percentage.** A fraction cannot express "the best
  offer shows 40 of the 75 you need". One NIFTY lot is 75 units; if the touch
  is thinner than that, a market order fills the rest higher up. `walk()`
  computes the real average fill, and says so plainly when the visible book
  cannot fill the order at all rather than inventing a price for the remainder.
- **The crossed-book self-correction.** The installed `SmartApi` SDK assigns
  `best_5_buy_data` from its own parser's *sell* list and vice versa. Rather
  than hard-code a fix to someone else's bug — which inverts silently the day
  they fix it — the parser checks the one invariant a real market cannot
  violate: **a book cannot be crossed.** Self-correcting in both directions.
- **Paise versus rupees.** The binary feed reports integers in paise, the REST
  quote reports rupees. The scale is *derived* by comparing against the LTP,
  not assumed; without a reference price it says so instead of guessing.

A missing book returns the assumed constant flagged `measured=False`, never a
tight one — the same rule as an agent's failed LLM call becoming `CAUTION`
rather than a silent `PROCEED`.

**Time of day.** NSE's own microstructure research (Competitive Paper 128) puts
spreads **widest at the open and close and narrowest between roughly 11:00 and
14:00**. The scorer rewards opening-range breakouts, so it recommends trading
in the single most expensive window of the day and charged a flat cost for it.
`SPREAD_SHAPE_BY_HOUR` encodes that shape and is **labelled a prior, not a
measurement**, with `calibrate_shape_from_fills()` to replace it from your own
fills. It is applied only where nothing was measured — a measured book already
*is* the book at that hour, so multiplying it by the prior would charge the
same effect twice.

## 2. The significance test was wrong, and the project's headline finding did not survive it

This is the one that changes what you believe.

`validation.py` measured a 24-bar forward return at **every** bar, so
consecutive observations shared 23 of their 24 bars of path — one observation
reported twenty-four times with a small perturbation. It then divided by
`std/sqrt(n)`, the standard error of a mean of *independent* draws.

Re-running the project's own flagship experiment through the corrected
statistics, on the same data:

```
BANKNIFTY  fade score<=9 / >=90, 24-bar hold, OUT-OF-SAMPLE
  n=455  mean +0.1117%  win 56.5%
    i.i.d. t                 +3.90   <- what was reported and written up
    HAC-corrected t          +1.83   <- below the significance bar
    effective sample             19   <- not 455
    non-overlapping median t +0.86   <- and the subsamples disagree on sign
    block bootstrap          p=0.056  <- does not reject a zero mean
```

**There was never a statistically significant edge there.** The in-sample table
shows the same pattern everywhere: every `t_iid` above 2 collapses below it
once the overlap is accounted for.

How bad is the naive statistic? Measured, not argued — on simulated random
walks containing **no effect at all**, with a persistent trend-following score
of the kind `scoring.py` produces, it declares significance **53% of the
time**. A correct test says 5%. It was finding an edge in noise more often than
not. (`tests/test_overlap_correction.py` asserts this.)

Three views are now reported, because the conventional fix is not sufficient
on its own:

| test | false positives on pure noise | power vs a real drift |
|---|---|---|
| naive i.i.d. t | **53%** | 100% |
| HAC (Newey-West) | 13-20% | 100% |
| block bootstrap | 12-20% | 100% |
| non-overlapping median t | **0-7%** | 100% |

The conservatism costs nothing detectable, so `validate_rule` now requires the
overlap-free tests to agree before it will call anything tradeable — a fifth
check alongside the original four. It is judged only where the evidence exists:
absence of corroboration is not disconfirmation.

`research_fade_test.py` had its own hand-rolled copy of the naive statistic and
now delegates to the module, so the error cannot be reintroduced in one place
while the fix sits unused in the other.

## 3. Nothing here had ever measured realised volatility

`options_math.py` computes implied volatility and compares it to India VIX.
That answers "dear or cheap *relative to other options*". It cannot answer the
question that decides whether to **buy or sell** premium: is the market
charging more for movement than the movement that actually happens?

**`intraday_advisor/volatility.py`** — Parkinson, Garman-Klass,
Rogers-Satchell, Yang-Zhang and close-to-close, plus the variance risk premium.
This is the one genuinely **orthogonal** addition: realised volatility is also
computed from price, but comparing it to *implied* brings in information from
outside the price series entirely — what other participants will pay.

Measured (`research_variance_premium.py`, re-runnable):

```
                     NIFTY    BANKNIFTY
daily realised       12.3%      13.8%     (Yang-Zhang, last 30 sessions)
India VIX           ~15.9%     ~15.9%
premium              +3.6       +2.1      volatility points, TO THE SELLER
```

**Every option in this ledger was bought into a structural headwind.** That is
the cleanest explanation yet for "direction was RIGHT and the trade still
lost" — and `trading-strategy-framework` already recorded the symptom without
the cause: *"Puts were the dear side on both and I was buying them."*

Two traps this surfaces, both found by measuring rather than assuming:

- **Annualise in trading time, not calendar time.** `options_math` prices with
  `days/365`. Realised volatility accrues only while the market is open, and
  252 trading days span one calendar year, so the bar count is built on 252
  (5-minute bars: 75 a day, 18,900 a year). Using 365 overstates realised
  volatility by ~20% and makes every option look cheap.
- **The intraday figure overstates the premium.** A session-aware 5-minute
  estimate drops overnight gaps; India VIX is a 30-day calendar measure that
  includes them. Comparing those two gave +8.3 and +6.0 points — more than
  double the honest +3.6 and +2.1. The daily numbers also reproduce the
  textbook result: Parkinson and Garman-Klass read ~4 points lower than
  Yang-Zhang on daily bars, because neither formula contains the overnight
  jump.

**And it still does not clear costs.** The premium over a 6-day hold is worth
**0.46% (NIFTY) and 0.27% (BANKNIFTY)** of the underlying, against the ~1.25%
round trip `microstructure.py` measures on a one-lot option. The favoured side
of the trade loses too, at this size — which matches the published backtest
concluding the NIFTY variance premium cannot be captured by a retail trader
under current microstructure.

So the premium is an argument about which **structure** to prefer — sell inside
defined risk rather than buy — not a trade signal. `variance_risk_premium()`
therefore **refuses to call the premium an opportunity** without a measured
round-trip cost, and when it does clear, it says "a candidate to test, not a
trade to place" and insists on defined risk, because a positive variance
premium is payment for bearing crash risk: small wins often, large losses
rarely, and a win rate that tells you almost nothing.

## What is deliberately NOT scored

Depth imbalance and the variance premium are both computed and both **kept out
of the scorer**, reported as evidence instead. Adding an unvalidated component
to a scorer already measured to have no edge is precisely the mistake that
produced 32 collinear components in the first place. `validate_rule` is the
gate either would have to pass.

## Also fixed while in here

`nse_option_chain.py` used `logger.debug` inside the `except` blocks of
`_volatility_read` and `_pin_read` while **no `logger` existed in the module**.
A `NameError` raised inside an exception handler replaces the exception it was
handling and escapes, so the two functions whose docstrings promised "never
raises" were the two that could — but only once something had already gone
wrong, which is why it went unnoticed.

## The standing conclusion is unchanged, and now better supported

**No rule yet discovered in this project clears its own transaction costs.**
What changed is that the one result which appeared to be an exception has been
withdrawn on its own evidence, and the cost side is now measured rather than
assumed. The app remains a well-instrumented measuring device with no validated
edge in it — which is a real and useful thing to own, provided nobody mistakes
it for a money machine.

**639 tests.**
