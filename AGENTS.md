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

### Configuration

```bash
AGENT_PROVIDER=rules      # default: checklist only
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
