/* ===========================================================================
   Intraday Options Desk — client
   ===========================================================================

   This file renders a snapshot produced by intraday_advisor.engine and sends
   setting changes and position commands back over the same socket. It holds NO
   analysis logic: every number shown is computed server-side by the one
   scoring engine, so the screen can never disagree with the ledger.

   The chart is hand-drawn on a canvas rather than pulled from a charting CDN.
   Two reasons: a trading tool should keep working with no internet, and the
   "live" feel needs direct control over how the forming candle animates
   between snapshots.
   =========================================================================== */

'use strict';

// --------------------------------------------------------------- utilities --

const $ = (id) => document.getElementById(id);

const REDUCED_MOTION =
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function rupees(value, { sign = false, decimals = 0 } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const prefix = sign && value > 0 ? '+' : '';
  return `${prefix}${value < 0 ? '-' : ''}₹${Math.abs(value).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

function percent(fraction, decimals = 1) {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) return '—';
  return `${(fraction * 100).toFixed(decimals)}%`;
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Flash a value element when its text changes, so updates are noticeable. */
function setText(element, text, { flash = false, positive = null } = {}) {
  if (!element) return;
  const next = String(text);
  if (element.textContent === next) return;
  element.textContent = next;
  if (!flash || REDUCED_MOTION) return;
  const klass = positive === null ? null : (positive ? 'flash-up' : 'flash-down');
  if (!klass) return;
  element.classList.remove('flash-up', 'flash-down');
  void element.offsetWidth;            // restart the animation
  element.classList.add(klass);
}

// ------------------------------------------------------------------ state --

const state = {
  snapshot: null,
  selectedIndex: null,
  socket: null,
  connected: false,
  reconnectDelay: 1000,
  soundOn: false,
  tableView: false,
  seenEvents: new Set(),
  controlsSynced: false,
};

// ------------------------------------------------------------------ sound --

let audioContext = null;

/** Short synthesised blip for danger alerts. Synthesised so the dashboard
 *  needs no audio asset and still works offline. */
function beep(severity) {
  if (!state.soundOn) return;
  try {
    audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
    const now = audioContext.currentTime;
    const osc = audioContext.createOscillator();
    const gain = audioContext.createGain();
    osc.type = 'sine';
    osc.frequency.value = severity === 'danger' ? 440 : 660;
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.18, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.34);
    osc.connect(gain).connect(audioContext.destination);
    osc.start(now);
    osc.stop(now + 0.36);
    if (severity === 'danger') {
      const second = audioContext.createOscillator();
      const secondGain = audioContext.createGain();
      second.type = 'sine';
      second.frequency.value = 440;
      secondGain.gain.setValueAtTime(0.0001, now + 0.4);
      secondGain.gain.exponentialRampToValueAtTime(0.18, now + 0.42);
      secondGain.gain.exponentialRampToValueAtTime(0.0001, now + 0.74);
      second.connect(secondGain).connect(audioContext.destination);
      second.start(now + 0.4);
      second.stop(now + 0.76);
    }
  } catch (err) {
    /* audio is a nice-to-have; never let it break the screen */
  }
}

// ----------------------------------------------------------------- toasts --

const TOAST_ICON = {
  SIGNAL_NEW: '🎯', SIGNAL_FLIP: '🔄', SIGNAL_CLEARED: '○', SCORE_MOVE: '📊',
  POSITION_TARGET: '🎉', POSITION_STOP: '🚨', POSITION_WARNING: '⚠️',
  RISK_HALT: '🛑', SQUARE_OFF: '⏰', DATA_ESTIMATED: '🟡',
};

function showToast(event) {
  const container = $('toasts');
  const node = document.createElement('div');
  node.className = `toast toast--${event.severity}`;
  node.innerHTML = `
    <div class="toast__icon">${TOAST_ICON[event.kind] || 'ℹ️'}</div>
    <div>
      <div class="toast__title">${escapeHtml(event.title)}</div>
      <div class="toast__msg">${escapeHtml(event.message)}</div>
      <div class="toast__time">${escapeHtml((event.ts || '').slice(11, 19))}</div>
    </div>
    <button class="toast__close" aria-label="Dismiss">×</button>`;

  const dismiss = () => {
    node.classList.add('is-leaving');
    setTimeout(() => node.remove(), 300);
  };
  node.querySelector('.toast__close').addEventListener('click', dismiss);
  // Appended, not prepended: the stack is anchored to the bottom, so the
  // newest alert lands closest to the corner the eye goes to.
  container.append(node);

  // Sticky events (stop hit, direction flip, square-off) stay until dismissed;
  // everything else clears itself so the corner does not fill up.
  if (!event.sticky) setTimeout(dismiss, 9000);

  // Keep the stack bounded.
  while (container.children.length > 6) container.firstElementChild.remove();

  if (event.severity === 'danger' || event.severity === 'warn') beep(event.severity);
}

// ---------------------------------------------------------- signal cards ---

/** What the chain charges for movement, and whether it is pinning a strike.
 *
 *  "Am I overpaying?" is the first question an option buyer has to answer and
 *  there was nothing on this screen to answer it from. Measured live on
 *  2026-10-21 the answer differed by index: NIFTY puts sat at 15.2% IV against
 *  India VIX 15.9 (fair), while BANKNIFTY puts sat at 17.1% with calls at
 *  15.0% -- two volatility points dearer for the same bearish view.
 */
function volatilityChip(signal) {
  const v = signal.volatility || {};
  const pin = signal.pin || {};
  const bits = [];

  if (v.atm_pe_iv || v.atm_ce_iv) {
    const dear = (v.skew ?? 0) >= 1 ? ' iv-chip--dear'
      : (v.skew ?? 0) <= -1 ? ' iv-chip--dear' : '';
    bits.push(`<span class="iv-chip${dear}" title="Implied volatility backed `
      + `out of the live premiums. The skew is put IV minus call IV: positive `
      + `means puts are the dearer side and you pay for that when you buy `
      + `one.">IV ${v.atm_ce_iv ?? '—'}c / ${v.atm_pe_iv ?? '—'}p${
        v.skew != null ? ` · skew ${v.skew > 0 ? '+' : ''}${v.skew}` : ''
      }</span>`);
  }
  if (pin.is_pin) {
    bits.push(`<span class="iv-chip iv-chip--pin" title="${escapeHtml(
      pin.note || '')}">PIN ${Math.round(pin.strike)}</span>`);
  }
  return bits.length ? ' ' + bits.join(' ') : '';
}

function riskStrip(plan) {
  if (!plan) return '';
  // Distinguish "you do not have the money" from "your own deploy cap is
  // blocking it". Showing total capital next to "needed per lot" made the
  // second case read as an arithmetic error.
  const capped = plan.max_capital_fraction < 1
    && plan.spendable_capital < plan.capital_per_lot
    && plan.available_capital >= plan.capital_per_lot;
  if (!plan.affordable) {
    return `
      <div class="risk risk--UNAFFORDABLE">
        <span class="tag tag--UNAFFORDABLE">✕ UNAFFORDABLE</span>
        <div class="risk__metric">
          <b>${rupees(plan.capital_per_lot)}</b><span>needed per lot</span>
        </div>
        <div class="risk__metric">
          <b>${rupees(capped ? plan.spendable_capital : plan.available_capital)}</b>
          <span>${capped ? `deployable (${Math.round(
            plan.max_capital_fraction * 100)}% cap)` : 'you have'}</span>
        </div>
        ${capped ? `<div class="risk__note">Your balance is ${rupees(
          plan.available_capital)} — the deploy cap is what blocks this, not the
          balance. Raise the cap or pick a cheaper strike.</div>` : ''}
      </div>`;
  }
  const icon = { SAFE: '✓', CAUTION: '!', SEVERE: '‼' }[plan.risk_band] || '';
  return `
    <div class="risk risk--${plan.risk_band}">
      <span class="tag tag--${plan.risk_band}">${icon} ${plan.risk_band}</span>
      <div class="risk__metric">
        <b>${plan.lots}</b><span>lots</span>
      </div>
      <div class="risk__metric">
        <b>${percent(plan.risk_pct_of_capital)}</b><span>risk</span>
      </div>
      <div class="risk__metric">
        <b>${plan.net_rr.toFixed(2)}</b><span>net R:R</span>
      </div>
      <div class="risk__metric">
        <b>${plan.losses_to_halve}</b><span>losses to halve</span>
      </div>
    </div>`;
}

function componentBars(components) {
  const entries = Object.entries(components || {}).filter(([, v]) => v !== 0);
  if (!entries.length) {
    return '<p class="field__hint">No scoring components contributed.</p>';
  }
  entries.sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  const scale = Math.max(...entries.map(([, v]) => Math.abs(v)), 10);

  return `<div class="comp">${entries.map(([name, value]) => {
    const width = (Math.abs(value) / scale) * 50;      // half-track max
    const positive = value > 0;
    const left = positive ? 50 : 50 - width;
    return `
      <div class="comp__row">
        <span class="comp__name">${escapeHtml(name.replace(/_/g, ' '))}</span>
        <span class="comp__track">
          <span class="comp__fill comp__fill--${positive ? 'pos' : 'neg'}"
                style="left:${left}%;width:${width}%"></span>
        </span>
        <span class="comp__val">${positive ? '+' : ''}${value.toFixed(1)}</span>
      </div>`;
  }).join('')}</div>`;
}

function scoreRing(score) {
  const radius = 27;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - Math.max(0, Math.min(100, score)) / 100);
  // Score colour is a magnitude cue, so it rides the sequential blue ramp
  // rather than a status colour — a high score is not "good news", it is a
  // stronger confluence reading.
  const stroke = score >= 70 ? cssVar('--accent')
    : score >= 45 ? cssVar('--series-1') : cssVar('--text-muted');
  return `
    <div class="ring">
      <svg width="66" height="66" viewBox="0 0 66 66" aria-hidden="true">
        <circle class="ring__track" cx="33" cy="33" r="${radius}"
                fill="none" stroke-width="6"></circle>
        <circle class="ring__value" cx="33" cy="33" r="${radius}"
                fill="none" stroke-width="6" stroke="${stroke}"
                stroke-dasharray="${circumference.toFixed(2)}"
                stroke-dashoffset="${offset.toFixed(2)}"></circle>
      </svg>
      <div class="ring__num">${score}</div>
    </div>`;
}

function signalCard(signal, changed) {
  const direction = signal.direction || 'WAIT';
  const plan = signal.plan;
  const tradable = direction === 'CALL' || direction === 'PUT';
  const reasons = signal.reasons || [];
  const warnings = signal.warnings || [];

  const contract = tradable
    ? `${signal.strike} ${signal.option_type}`
    : 'no position';

  const planRow = tradable ? `
    <div class="plan-row">
      <div class="plan-cell">
        <div class="plan-cell__label">Entry</div>
        <div class="plan-cell__value">₹${(signal.entry_premium || 0).toFixed(2)}</div>
      </div>
      <div class="plan-cell plan-cell--target">
        <div class="plan-cell__label">Target</div>
        <div class="plan-cell__value">₹${(signal.target_premium || 0).toFixed(2)}</div>
      </div>
      <div class="plan-cell plan-cell--stop">
        <div class="plan-cell__label">Stop</div>
        <div class="plan-cell__value">₹${(signal.stop_premium || 0).toFixed(2)}</div>
      </div>
    </div>` : '';

  const breakeven = plan && plan.affordable ? `
    <p class="field__hint" style="margin:-4px 0 12px">
      Breakeven ₹${plan.breakeven.toFixed(2)} · costs ${percent(plan.cost_drag, 2)} of
      position · ${rupees(plan.round_trip_cost)} round trip
    </p>` : '';

  const warnBlocks = warnings.length ? `
    <div class="warn-list">
      ${warnings.map((w) => {
        const danger = /SEVERE|cannot make money|Cannot afford|Conflict/i.test(w);
        return `<div class="warn-item ${danger ? 'warn-item--danger' : ''}">
                  <span class="warn-item__icon">${danger ? '‼' : '⚠'}</span>
                  <span>${escapeHtml(w)}</span>
                </div>`;
      }).join('')}
    </div>` : '';

  return `
    <article class="card signal-card ${changed ? 'is-changed' : ''}"
             data-index="${escapeHtml(signal.index)}">
      <div class="sig-top">
        <span class="badge-dir badge-dir--${direction}">${direction}</span>
        <div>
          <div class="sig-name">${escapeHtml(signal.index)}</div>
          <div class="sig-sub">${escapeHtml(contract)} · spot
            <span data-spot-for="${escapeHtml(signal.index)}">${
              (signal.spot || 0).toFixed(2)}</span></div>
        </div>
      </div>

      <div class="score">
        ${scoreRing(signal.score || 0)}
        <div class="score__meta">
          <div class="score__label">Confluence score</div>
          <div class="score__note">
            price ${signal.underlying_score ?? '—'} ·
            chain ${escapeHtml(signal.chain_bias || '—')} ·
            ${escapeHtml(signal.agreement || '—')}${volatilityChip(signal)}
          </div>
          <div class="score__note" style="color:var(--text-muted)">
            bar ${escapeHtml((signal.bar_time || '—').slice(0, 19))}
            ${signal.recorded ? '· recorded' : ''}
          </div>
        </div>
      </div>

      ${planRow}
      ${breakeven}
      ${riskStrip(plan)}
      ${warnBlocks}

      ${expiryPicker(signal)}
      ${agentPanel(signal.agent)}
      ${playbookPanel(signal.playbook)}

      ${candidatesTable(signal)}

      <details class="why">
        <summary>Live read — what each indicator is saying now</summary>
        <div class="why__body">${readingsPanel(signal.readings)}</div>
      </details>

      <details class="why">
        <summary>Why this score — full breakdown</summary>
        <div class="why__body">
          ${componentBars(signal.score_components)}
          <ul class="reasons">
            ${reasons.slice(0, 14).map((r) => `<li>${escapeHtml(r)}</li>`).join('')}
          </ul>
          <p class="field__hint" style="margin-top:9px">
            Source: ${escapeHtml(signal.data_source || 'unknown')} ·
            PCR ${(signal.pcr || 0).toFixed(2)} ·
            max pain ${(signal.max_pain || 0).toFixed(0)}
          </p>
          ${tradable && plan && plan.affordable ? `
            <button class="btn btn--primary take-trade"
                    data-index="${escapeHtml(signal.index)}">
              I took this trade
            </button>` : ''}
        </div>
      </details>
    </article>`;
}






// -------------------------------------------------------- stock watchlist ---

function renderStocks(snapshot) {
  const data = snapshot.stocks || {};
  const rows = data.rows || [];
  const body = $('stocks-body');
  const meta = $('stocks-meta');
  if (!body) return;

  const select = $('stock-universe');
  if (select && data.mode && select.value !== data.mode && !select.dataset.touched) {
    select.value = data.mode;
  }

  if (data.disabled) {
    body.innerHTML = '';
    setText(meta, 'off');
    setText($('stocks-note'), data.note || '');
    return;
  }

  setText(meta, data.scanning
    ? 'scanning…'
    : `${rows.length} of ${data.scored || 0} · ${data.age_seconds ?? 0}s ago`);

  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="16" class="empty">${
      data.scanning ? '<span class="scan-spinner"></span>' : ''
    }${escapeHtml(data.note || data.error || 'Nothing scanned yet.')}</td></tr>`;
    setText($('stocks-note'), '');
    return;
  }

  body.innerHTML = rows.map((r) => {
    // Net R:R below 1 means you risk more than you stand to make. At small
    // capital this is common for high-priced shares, because the position is
    // capped by cash while costs are not.
    const poor = r.net_rr > 0 && r.net_rr < 1;
    return `
      <tr title="${escapeHtml((r.warnings || []).join(' · '))}">
        <td class="sym">${escapeHtml(r.symbol)}</td>
        <td class="num">₹${r.price.toFixed(2)}</td>
        <td class="num ${r.change_pct > 0 ? 'pos' : r.change_pct < 0 ? 'neg' : ''}">
          ${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(2)}%</td>
        <td class="num"><b>${r.score}</b></td>
        <td><span class="sig-pill sig-pill--${escapeHtml(r.signal)}">${escapeHtml(r.signal)}</span>${
          r.side === 'SHORT'
            ? '<span class="side-flag" title="Short plan: the stop is ABOVE '
              + 'the entry and the target BELOW it. Needs MIS margin.">▼ SHORT</span>'
            : ''}</td>
        <td class="num">${r.rsi ?? '—'}</td>
        <td class="num">${r.adx ?? '—'}</td>
        <td class="num">${r.atr_pct ?? '—'}</td>
        <td class="num">${r.volume_ratio ?? '—'}</td>
        <td class="num">₹${r.entry.toFixed(2)}</td>
        <td class="num neg">₹${r.stop.toFixed(2)}</td>
        <td class="num pos">₹${r.target.toFixed(2)}</td>
        <td class="num">${r.shares || '—'}</td>
        <td class="num">${r.shares ? `${rupees(r.risk_amount)} (${percent(r.risk_pct)})` : '—'}</td>
        <td class="num ${poor ? 'neg' : ''}">${r.net_rr.toFixed(2)}</td>
        <td class="num">${percent(r.cost_pct, 2)}</td>
      </tr>`;
  }).join('');

  const poorCount = rows.filter((r) => r.net_rr > 0 && r.net_rr < 1).length;
  setText($('stocks-note'),
    `${data.note || ''}`
    + (poorCount
      ? ` ${poorCount} of ${rows.length} show net reward:risk below 1 — at this `
        + `capital the position is capped by cash while costs are not, so you `
        + `would risk more than you stand to make.`
      : ''));
}

// ------------------------------------------------------------ candidates ---

function expiryPicker(signal) {
  const expiries = signal.expiries || [];
  if (!expiries.length) return '';
  const current = signal.expiry || '';
  const chosen = expiries.find((e) => e.label === current);
  return `
    <div class="expiry-row">
      <label for="exp-${escapeHtml(signal.index)}">Expiry</label>
      <select id="exp-${escapeHtml(signal.index)}" class="expiry-select"
              data-index="${escapeHtml(signal.index)}">
        ${expiries.map((e) => `
          <option value="${escapeHtml(e.label)}" ${e.label === current ? 'selected' : ''}>
            ${escapeHtml(e.pretty)} · ${e.days}d · ${escapeHtml(e.kind)}
          </option>`).join('')}
      </select>
      <span class="expiry-row__note">${escapeHtml(chosen ? chosen.decay_note : '')}</span>
    </div>`;
}

/** Alternative contracts. The engine names one; this shows the other ways to
 *  express the same view, each priced and sized at the real capital. */
function candidatesTable(signal) {
  const rows = signal.candidates || [];
  if (!rows.length) return '';
  return `
    <details class="why">
      <summary>Other contracts — ${rows.length} ways to trade this</summary>
      <div class="why__body">
        <div class="table-scroll">
          <table class="cands">
            <thead>
              <tr>
                <th>Contract</th><th>Premium</th><th>Lots</th><th>Per lot</th>
                <th>Risk</th><th>Net R:R</th><th>Breakeven</th><th>Costs</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map((c) => {
                const klass = !c.affordable ? 'cand--unaffordable'
                  : c.aligned ? 'cand--aligned' : 'cand--counter';
                return `
                <tr class="${klass}" title="${escapeHtml((c.notes || []).join(' · '))}">
                  <td><span class="cand__contract">${c.strike} ${escapeHtml(c.option_type)}</span>
                      <span class="cand__tag cand__tag--${escapeHtml(c.moneyness)}">${escapeHtml(c.moneyness)}</span></td>
                  <td>₹${c.premium.toFixed(2)}</td>
                  <td>${c.affordable ? c.lots : '—'}</td>
                  <td>${rupees(c.capital_per_lot)}</td>
                  <td class="cand__risk--${escapeHtml(c.risk_band)}">${
                    c.affordable ? percent(c.risk_pct) : 'n/a'}</td>
                  <td>${c.net_rr.toFixed(2)}</td>
                  <td>₹${c.breakeven.toFixed(2)}</td>
                  <td>${percent(c.cost_drag, 2)}</td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>
        </div>
        <p class="field__hint" style="margin-top:9px">
          Green edge = agrees with the engine's read; faded = counter-trend or
          unaffordable at your capital. Further OTM is cheaper and buys more
          lots, but needs a bigger move and carries higher cost drag. Only the
          engine's own signal is recorded and graded.
        </p>
      </div>
    </details>`;
}

function renderTickBadge(snapshot) {
  const ticks = snapshot.ticks || {};
  const badge = $('tick-badge');
  if (!badge) return;
  const on = !!ticks.connected && (ticks.subscribed || 0) > 0;
  badge.className = `pill ${on ? 'pill--ticks-on' : 'pill--closed'}`;
  badge.title = ticks.error
    ? `${ticks.note || ''} — ${ticks.error}`
    : (ticks.note || '');
  setText($('tick-text'), on ? `TICKS ${ticks.subscribed}` : 'TICKS —');
}

// ------------------------------------------------------ market & premarket ---

function renderMarketPicker(snapshot) {
  const select = $('market');
  const markets = snapshot.markets || [];
  const current = (snapshot.settings || {}).market
    || (snapshot.market_session || {}).market;

  if (select.dataset.filled !== '1' && markets.length) {
    select.innerHTML = markets.map((m) => `
      <option value="${escapeHtml(m.key)}">
        ${escapeHtml(m.name)} — ${escapeHtml(m.exchange)} (${escapeHtml(m.open)}-${escapeHtml(m.close)})
      </option>`).join('');
    select.dataset.filled = '1';
  }
  if (current) select.value = current;

  const session = snapshot.market_session || {};
  setText($('market-hint'),
    `${session.local_time ? session.local_time.slice(11, 16) : '--:--'} local · `
    + `${(session.state || '').replace('_', '-').toLowerCase()}`);

  // The token gate, visible at all times so a silent LLM is never a mystery.
  const gate = $('llm-gate');
  const on = !!session.llm_active;
  gate.className = `pill ${on ? 'pill--gate-on' : 'pill--gate-off'}`;
  gate.title = session.llm_reason || '';
  setText($('llm-gate-text'),
    on ? 'LLM ON'
       : (session.llm_opens_in_minutes != null
          ? `LLM in ${session.llm_opens_in_minutes}m`
          : 'LLM off'));
}

const LEAN_LABEL = {
  LEAN_UP: 'LEANS UP', LEAN_DOWN: 'LEANS DOWN',
  UNSTABLE: 'UNSTABLE', UNCLEAR: 'NO CLEAR LEAN',
};

function renderPremarket(snapshot) {
  const brief = snapshot.premarket || {};
  const body = $('premarket-body');
  const lean = brief.lean || 'UNCLEAR';

  setText($('premarket-meta'),
    brief.model ? `${brief.model} · ${brief.headlines_used || 0} headlines`
                : `${brief.headlines_used || 0} headlines`);

  const drivers = (brief.drivers || []).map((d) => `
    <div class="driver">
      <span class="driver__effect driver__effect--${escapeHtml(d.effect || 'volatile')}">
        ${escapeHtml(d.effect || '?')}</span>
      <span>
        <span class="driver__what">${escapeHtml(d.what)}</span>
        <span class="driver__why">${escapeHtml(d.why)}</span>
      </span>
    </div>`).join('');

  const columns = [];
  if ((brief.watch || []).length) {
    columns.push(`<div class="brief__col"><h4>Watch today</h4><ul>${
      brief.watch.map((w) => `<li>${escapeHtml(w)}</li>`).join('')}</ul></div>`);
  }
  if ((brief.caveats || []).length) {
    columns.push(`<div class="brief__col"><h4>What would make this wrong</h4><ul>${
      brief.caveats.map((c) => `<li>${escapeHtml(c)}</li>`).join('')}</ul></div>`);
  }

  body.innerHTML = `
    <div class="brief brief--${escapeHtml(lean)}">
      <span class="brief__lean">${LEAN_LABEL[lean] || lean}${
        brief.confidence ? ` ${brief.confidence}%` : ''}</span>
      <div class="brief__main">
        <p class="brief__headline">${escapeHtml(
          brief.headline || brief.note || 'No brief yet.')}</p>
        ${drivers}
        ${columns.length ? `<div class="brief__cols">${columns.join('')}</div>` : ''}
        ${brief.headline ? `<p class="brief__note">${escapeHtml(brief.note || '')}</p>` : ''}
        ${brief.error ? `<p class="brief__note">⚠ ${escapeHtml(brief.error)}</p>` : ''}
      </div>
    </div>`;

  const list = $('premarket-headlines');
  const headlines = brief.top_headlines || [];
  list.innerHTML = headlines.length
    ? headlines.map((h) => `
        <div class="headline-row">
          <span class="headline-row__age">${h.age_hours != null ? `${Math.round(h.age_hours)}h` : '—'}</span>
          <span class="headline-row__src">${escapeHtml(h.source || h.feed || '')}</span>
          <span class="headline-row__title">${escapeHtml(h.title)}</span>
        </div>`).join('')
    : '<p class="field__hint">No headlines retrieved.</p>';
}

// ------------------------------------------------------- market direction ---

const REGIME_LABEL = {
  TRENDING_UP: 'TRENDING UP', TRENDING_DOWN: 'TRENDING DOWN',
  RANGE: 'RANGE-BOUND', VOLATILE: 'HIGH VOLATILITY', UNKNOWN: 'NO READ',
};
const REGIME_TAG = {
  TRENDING_UP: 'tag--SAFE', TRENDING_DOWN: 'tag--SEVERE',
  VOLATILE: 'tag--CAUTION', RANGE: 'tag--muted', UNKNOWN: 'tag--muted',
};

/** Timeframe ladder, slowest first: the slow frames are the context the fast
 *  ones move inside, so they are read top-down. */
function timeframeLadder(timeframes) {
  if (!timeframes || !timeframes.length) {
    return '<p class="field__hint">No timeframe data.</p>';
  }
  const ordered = [...timeframes].reverse();
  return `<div class="tf-ladder">${ordered.map((tf) => {
    const width = Math.max(4, (tf.strength || 0) * 50);
    const left = tf.trend === 'up' ? 50 : tf.trend === 'down' ? 50 - width : 48;
    const w = tf.trend === 'flat' ? 4 : width;
    return `
      <div class="tf-row" title="${escapeHtml(tf.note || '')}">
        <span class="tf-row__label">${escapeHtml(tf.label)}</span>
        <span class="tf-row__bar">
          <span class="tf-row__fill tf-row__fill--${tf.trend}"
                style="left:${left}%;width:${w}%"></span>
        </span>
        <span class="tf-row__state">${escapeHtml(tf.trend)}</span>
      </div>`;
  }).join('')}</div>`;
}

function regimeCard(regime) {
  const vol = regime.volatility || {};
  const breadth = regime.breadth || {};
  const glob = regime.global_cues || {};
  const levels = regime.levels || {};

  const context = [];
  if (vol.vix != null) {
    context.push(`<div class="context-item"><b>${vol.vix}</b><span>India VIX (${escapeHtml(vol.level)})</span></div>`);
  }
  if (breadth.pct != null) {
    context.push(`<div class="context-item"><b>${Math.round(breadth.pct * 100)}%</b><span>heavyweights up${breadth.divergence ? ' ⚠' : ''}</span></div>`);
  }
  if (glob.lean && glob.lean !== 'unknown') {
    context.push(`<div class="context-item"><b>${escapeHtml(glob.lean)}</b><span>global cues</span></div>`);
  }
  if (levels.range_position != null) {
    context.push(`<div class="context-item"><b>${Math.round(levels.range_position * 100)}%</b><span>of day's range</span></div>`);
  }

  return `
    <div class="regime regime--${escapeHtml(regime.regime)}">
      <div class="regime__head">
        <span class="regime__index">${escapeHtml(regime.index)}</span>
        <span class="regime__label tag ${REGIME_TAG[regime.regime] || 'tag--muted'}">
          ${REGIME_LABEL[regime.regime] || regime.regime}</span>
        <span class="regime__conv">${regime.conviction}% of inputs agree</span>
      </div>
      ${timeframeLadder(regime.timeframes)}
      <div class="context-row">${context.join('')}</div>
    </div>`;
}

function regimeDetail(regimes) {
  return Object.values(regimes).map((regime) => {
    const vol = regime.volatility || {};
    const glob = regime.global_cues || {};
    const cues = (glob.cues || []).filter((c) => c.change_pct != null);
    return `
      <div class="card" style="background:var(--surface-2);margin-top:14px">
        <h3 class="card__title" style="margin-bottom:8px">${escapeHtml(regime.index)} — full read</h3>
        <ul class="reasons">
          ${(regime.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join('')}
        </ul>
        ${(regime.warnings || []).length ? `<div class="warn-list">${
          regime.warnings.map((w) => `<div class="warn-item"><span class="warn-item__icon">⚠</span><span>${escapeHtml(w)}</span></div>`).join('')
        }</div>` : ''}
        ${vol.meaning ? `<p class="field__hint" style="margin-top:9px">${escapeHtml(vol.meaning)} ${escapeHtml(vol.implication || '')}</p>` : ''}
        ${cues.length ? `<p class="field__hint" style="margin-top:7px">${
          cues.map((c) => `${escapeHtml(c.name)} ${c.change_pct > 0 ? '+' : ''}${c.change_pct}%`).join(' · ')
        }</p>` : ''}
        ${(regime.invalidation || []).length ? `
          <div class="invalidation">
            <b>What would change this read:</b>
            <ul>${regime.invalidation.map((i) => `<li>${escapeHtml(i)}</li>`).join('')}</ul>
          </div>` : ''}
      </div>`;
  }).join('');
}

function renderRegime(snapshot) {
  const regimes = snapshot.regimes || {};
  const cards = $('regime-cards');
  if (!Object.keys(regimes).length) {
    cards.innerHTML = '<p class="field__hint">Market context loading…</p>';
    return;
  }
  cards.innerHTML = Object.values(regimes).map(regimeCard).join('');
  $('regime-detail').innerHTML = regimeDetail(regimes);

  const status = snapshot.agent_status || {};
  setText($('agent-status'), status.provider ? `agent: ${status.provider}` : '');
  $('agent-status').title = status.detail || '';
}

// ----------------------------------------------------------- agent verdict ---

const AGENT_ICON = { PROCEED: '✓', CAUTION: '⚠', VETO: '⛔' };

function agentPanel(agent) {
  if (!agent || !agent.stance) return '';
  const concerns = (agent.concerns || []).slice(0, 4);
  const supports = (agent.supports || []).slice(0, 2);
  return `
    <div class="agent agent--${escapeHtml(agent.stance)}">
      <span class="agent__icon">${AGENT_ICON[agent.stance] || '•'}</span>
      <div class="agent__body">
        <div class="agent__head">
          <span class="agent__stance">${escapeHtml(agent.stance)}</span>
          <span class="agent__by">${escapeHtml(agent.provider || '')}${
            agent.degraded ? ' · degraded' : ''}</span>
        </div>
        <p class="agent__summary">${escapeHtml(agent.summary || '')}</p>
        ${concerns.length ? `<ul class="agent__list">${
          concerns.map((c) => `<li>${escapeHtml(c)}</li>`).join('')}</ul>` : ''}
        ${supports.length ? `<ul class="agent__list" style="opacity:0.75">${
          supports.map((s) => `<li>✓ ${escapeHtml(s)}</li>`).join('')}</ul>` : ''}
      </div>
    </div>`;
}

// ------------------------------------------------------- live read panel ---

const STATE_ICON = { bullish: '\u25B2', bearish: '\u25BC', neutral: '\u2022', caution: '!' };

function readingsPanel(readings) {
  if (!readings || !readings.length) return '';
  return `<div class="readings">${readings.map((r) => `
    <div class="reading reading--${r.state}">
      <div class="reading__head">
        <span class="reading__icon">${STATE_ICON[r.state] || '\u2022'}</span>
        <span class="reading__label">${escapeHtml(r.label)}</span>
        <span class="reading__value">${escapeHtml(r.value)}</span>
      </div>
      <p class="reading__what">${escapeHtml(r.what)}</p>
      <p class="reading__meaning">${escapeHtml(r.meaning)}</p>
      <p class="reading__action"><b>So:</b> ${escapeHtml(r.action)}</p>
    </div>`).join('')}</div>`;
}

function playbookPanel(playbook) {
  if (!playbook) return '';
  const steps = (playbook.steps || []).map((step) => {
    const parts = String(step).split(' \u2014 ');
    const head = parts.shift();
    const body = parts.join(' \u2014 ');
    return body
      ? `<li><b>${escapeHtml(head)}</b> \u2014 ${escapeHtml(body)}</li>`
      : `<li>${escapeHtml(head)}</li>`;
  }).join('');
  return `
    <div class="playbook ${playbook.tradable ? '' : 'playbook--wait'}">
      <div class="playbook__head">${escapeHtml(playbook.headline)}</div>
      <p class="playbook__why">${escapeHtml(playbook.why)}</p>
      <ol class="playbook__steps">${steps}</ol>
    </div>`;
}

// ------------------------------------------------------------ renderers ----

let previousDirections = {};

function renderSignals(snapshot) {
  const container = $('signals');
  const cards = snapshot.signals.map((signal) => {
    const changed = previousDirections[signal.index] !== undefined
      && previousDirections[signal.index] !== signal.direction;
    return signalCard(signal, changed);
  });
  container.innerHTML = cards.join('');
  previousDirections = Object.fromEntries(
    snapshot.signals.map((s) => [s.index, s.direction])
  );

  container.querySelectorAll('.take-trade').forEach((button) => {
    button.addEventListener('click', () => openPositionFromSignal(button.dataset.index));
  });

  // Expiry is per index: NIFTY can sit on a weekly while BANKNIFTY has only
  // monthlies available.
  container.querySelectorAll('.expiry-select').forEach((select) => {
    select.addEventListener('change', () => {
      send({ type: 'settings',
             expiries: { [select.dataset.index]: select.value } });
    });
  });
}

function renderKpis(snapshot) {
  const session = snapshot.session || {};
  const total = session.total_pnl || 0;

  const pnlNode = $('pnl-total');
  setText(pnlNode, rupees(total, { sign: true }), { flash: true, positive: total >= 0 });
  pnlNode.className = `hero__value ${total > 0 ? 'hero__value--up' : total < 0 ? 'hero__value--down' : ''}`;

  const realised = session.realised_pnl || 0;
  const open = session.open_pnl || 0;
  setText($('pnl-sub'),
    `${rupees(realised, { sign: true })} realised · ${rupees(open, { sign: true })} open`);

  setText($('kpi-exposure'), rupees(session.exposure || 0));
  setText($('kpi-free'), rupees(session.capital_free || 0));
  setText($('kpi-open'), session.open_trades || 0);
  setText($('kpi-closed'), session.closed_trades || 0);

  const ledger = snapshot.ledger || {};
  const performance = ledger.performance || {};
  setText($('kpi-graded'), ledger.graded || 0);
  setText($('kpi-ungraded'), ledger.ungraded || 0);
  setText($('kpi-winrate'),
    performance.win_rate === null || performance.win_rate === undefined
      ? '—'
      : `${percent(performance.win_rate, 0)}`);
  setText($('kpi-expectancy'),
    performance.expectancy === null || performance.expectancy === undefined
      ? '—'
      : rupees(performance.expectancy, { sign: true }));
}

function renderEdge(snapshot) {
  const ledger = snapshot.ledger || {};
  const performance = ledger.performance || {};
  const verdict = performance.verdict || 'No data yet.';

  const node = $('edge-verdict');
  node.textContent = verdict;
  node.className = 'verdict' + (
    /^Positive expectancy/.test(verdict) ? ' verdict--good'
      : /NEGATIVE expectancy/.test(verdict) ? ' verdict--danger'
      : /Inconclusive|too few/.test(verdict) ? ' verdict--warn' : ''
  );

  setText($('edge-engine'), snapshot.engine_version || '');

  const body = $('bands-body');
  const bands = ledger.bands || [];
  if (!bands.length) {
    body.innerHTML = `<tr><td colspan="5" class="empty">
      Nothing graded yet. Record signals, then grade after the close.
    </td></tr>`;
    return;
  }
  body.innerHTML = bands.map((band) => `
    <tr>
      <td>${escapeHtml(band.band)}</td>
      <td class="num">${band.trades}</td>
      <td class="num">${band.win_rate === null ? '—' : percent(band.win_rate, 0)}</td>
      <td class="num ${band.expectancy > 0 ? 'pos' : band.expectancy < 0 ? 'neg' : ''}">
        ${band.expectancy === null ? '—' : rupees(band.expectancy, { sign: true })}
      </td>
      <td class="num">${band.avg_r === null ? '—' : band.avg_r.toFixed(2)}</td>
    </tr>`).join('');
}

// Status presentation lives in one place: the full render and the 1.5s live
// update must never disagree about what a stop-out looks like.
const POS_TAG = {
  TARGET_HIT: 'tag--SAFE', STOP_HIT: 'tag--SEVERE', WARNING: 'tag--CAUTION',
  ON_TRACK: 'tag--SAFE', NO_DATA: 'tag--muted',
};
const POS_ICON = {
  TARGET_HIT: '\u{1F3AF}', STOP_HIT: '\u{1F6A8}', WARNING: '\u26A0',
  ON_TRACK: '\u2713', NO_DATA: '\u2014',
};

const pnlClass = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : '');

/** The current premium, plus an honest marker of where it came from.
 *
 *  A price from the tick socket and the last price a REST call managed are not
 *  the same thing, and on an exit decision the difference matters, so the cell
 *  says which it is instead of showing a bare number.
 */
function premiumCell(position) {
  if (position.current_premium === null || position.current_premium === undefined) {
    return '\u2014';
  }
  const isTick = (position.premium_source || '').startsWith('live tick');
  return `<span class="prem${isTick ? ' prem--live' : ''}">\u20B9${
    position.current_premium.toFixed(2)}</span>`;
}

/** Re-price the open rows in place, on the tick clock.
 *
 *  Deliberately not a full re-render: replacing the table's innerHTML every
 *  1.5s would destroy the Close button between a mousedown and its mouseup,
 *  so the one click that has to work would be the one that silently does not.
 *  Only the cells whose numbers moved are touched.
 */
function updatePositionsLive(positions) {
  const body = $('positions-body');
  if (!body) return;

  const shown = Array.from(body.querySelectorAll('tr[data-pos]'))
    .map((tr) => tr.dataset.pos);
  const incoming = positions.map((p) => p.position_id);
  if (shown.length !== incoming.length
      || shown.some((id, i) => id !== incoming[i])) {
    // A position was opened or closed: structure changed, so rebuild.
    renderPositions(state.snapshot);
    return;
  }

  for (const position of positions) {
    const row = body.querySelector(
      `tr[data-pos="${CSS.escape(position.position_id)}"]`);
    if (!row) continue;
    const net = position.net_pnl || 0;

    setCell(row, 'premium', premiumCell(position), {
      html: true, title: position.premium_source || '',
    });
    setCell(row, 'net', rupees(net, { sign: true }), { cls: pnlClass(net) });
    setCell(row, 'pnlpct', percent(position.pnl_pct),
      { cls: pnlClass(position.pnl_pct) });
    setCell(row, 'status',
      `<span class="tag ${POS_TAG[position.status] || 'tag--muted'}">${
        POS_ICON[position.status] || ''} ${escapeHtml(
        position.status.replace('_', ' '))}</span>`, { html: true });
    setCell(row, 'action', position.action);
    // The bracket moves now: Protect pulls the stop up to breakeven. Leaving
    // these out of the live update meant a stop could move in the ledger and
    // stay wrong on screen until a reload -- the one number you must be able
    // to trust at a glance.
    setCell(row, 'stop', `₹${position.stop_premium.toFixed(2)}`);
    setCell(row, 'target', `₹${position.target_premium.toFixed(2)}`);

    row.classList.toggle('pos-row--alarm', Boolean(position.is_alarm));
    const button = row.querySelector('.close-position');
    if (button) button.dataset.premium = position.current_premium ?? '';
  }
}

/** Write a cell only when its content actually changed, and flash it if so. */
function setCell(row, field, content, { html = false, cls = null, title = null } = {}) {
  const cell = row.querySelector(`[data-field="${field}"]`);
  if (!cell) return;
  const next = html ? content : String(content);
  const current = html ? cell.innerHTML.trim() : cell.textContent.trim();
  if (cls !== null) {
    cell.classList.remove('pos', 'neg');
    if (cls) cell.classList.add(cls);
  }
  if (title !== null) cell.title = title;
  if (current === next.trim()) return;      // no change: no DOM write, no flash

  if (html) cell.innerHTML = next; else cell.textContent = next;
  // A number that moved should catch the eye; one that did not should not.
  cell.classList.remove('cell-flash');
  void cell.offsetWidth;                    // restart the animation
  cell.classList.add('cell-flash');
}

function renderPositions(snapshot) {
  const body = $('positions-body');
  const empty = $('positions-empty');
  const positions = snapshot.positions || [];

  if (!positions.length) {
    body.innerHTML = '';
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  body.innerHTML = positions.map((position) => {
    const net = position.net_pnl || 0;
    const statusTag = POS_TAG[position.status] || 'tag--muted';
    const icon = POS_ICON[position.status] || '';

    return `
      <tr data-pos="${escapeHtml(position.position_id)}"
          class="${position.is_alarm ? 'pos-row--alarm' : ''}">
        <td style="color:var(--text-primary);font-weight:600">
          ${escapeHtml(position.index_name)} ${position.strike}
          ${escapeHtml(position.option_type)}
        </td>
        <td class="num">${position.lots}×${position.lot_size}</td>
        <td class="num">₹${position.entry_premium.toFixed(2)}</td>
        <td class="num" data-field="premium"
            title="${escapeHtml(position.premium_source || '')}">${
          premiumCell(position)}</td>
        <td class="num" data-field="target">₹${position.target_premium.toFixed(2)}</td>
        <td class="num" data-field="stop">₹${position.stop_premium.toFixed(2)}</td>
        <td class="num ${pnlClass(net)}" data-field="net">
          ${rupees(net, { sign: true })}</td>
        <td class="num ${pnlClass(position.pnl_pct)}" data-field="pnlpct">
          ${percent(position.pnl_pct)}</td>
        <td data-field="status"><span class="tag ${statusTag}">${icon} ${escapeHtml(
          position.status.replace('_', ' '))}</span></td>
        <td data-field="action" style="color:var(--text-primary);font-weight:600">
          ${escapeHtml(position.action)}</td>
        <td style="display:flex;gap:5px">
          <button class="btn btn--ghost protect-position"
                  data-id="${escapeHtml(position.position_id)}"
                  title="Move the stop up to net breakeven (entry + costs). A stop can only ever be tightened.">Protect</button>
          <button class="btn btn--danger close-position"
                  data-id="${escapeHtml(position.position_id)}"
                  data-premium="${position.current_premium ?? ''}">Close</button>
        </td>
      </tr>`;
  }).join('');

  body.querySelectorAll('.close-position').forEach((button) => {
    button.addEventListener('click', () => closePosition(
      button.dataset.id, button.dataset.premium
    ));
  });
  body.querySelectorAll('.protect-position').forEach((button) => {
    button.addEventListener('click', () => {
      send({ type: 'protect_position', position_id: button.dataset.id });
    });
  });
}


// --------------------------------------------------------------- journal ---

function renderJournal(snapshot) {
  const history = snapshot.history || {};
  const trades = history.trades || [];
  const summary = history.summary || {};

  setText($('journal-count'), `${summary.trades || 0} closed`);

  const verdict = $('realised-verdict');
  verdict.textContent = summary.verdict || 'No closed trades yet.';
  verdict.className = 'verdict' + (
    (summary.net || 0) > 0 ? ' verdict--good'
      : (summary.net || 0) < 0 ? ' verdict--danger' : ''
  );

  setText($('rp-gross'), rupees(summary.gross || 0, { sign: true }));
  setText($('rp-costs'), rupees(summary.costs || 0));
  setText($('rp-net'), rupees(summary.net || 0, { sign: true }));
  setText($('rp-winrate'),
    summary.win_rate === null || summary.win_rate === undefined
      ? '\u2014' : percent(summary.win_rate, 0));
  setText($('rp-pf'), summary.profit_factor ?? '\u2014');
  setText($('rp-extremes'), summary.trades
    ? `${rupees(summary.best, { sign: true })} / ${rupees(summary.worst, { sign: true })}`
    : '\u2014');

  const body = $('journal-body');
  const empty = $('journal-empty');
  if (!trades.length) {
    body.innerHTML = '';
    empty.hidden = false;
  } else {
    empty.hidden = true;
    body.innerHTML = trades.map((t) => `
      <tr>
        <td>${escapeHtml((t.closed_at || '').slice(11, 16))}</td>
        <td style="color:var(--text-primary);font-weight:600">
          ${escapeHtml(t.contract)}</td>
        <td class="num">${t.lots}</td>
        <td class="num">\u20B9${t.entry_premium.toFixed(2)}</td>
        <td class="num">\u20B9${t.exit_premium.toFixed(2)}</td>
        <td class="num ${t.gross_pnl > 0 ? 'pos' : t.gross_pnl < 0 ? 'neg' : ''}">
          ${rupees(t.gross_pnl, { sign: true })}</td>
        <td class="num" style="color:var(--warning)">-${rupees(t.cost)}</td>
        <td class="num ${t.net_pnl > 0 ? 'pos' : 'neg'}">
          ${rupees(t.net_pnl, { sign: true })}</td>
        <td class="num">${percent(t.pnl_pct)}</td>
        <td><button class="btn btn--ghost delete-trade"
                    data-id="${escapeHtml(t.position_id)}"
                    title="Remove this row (for test entries)">\u2715</button></td>
      </tr>`).join('');

    body.querySelectorAll('.delete-trade').forEach((button) => {
      button.addEventListener('click', () => {
        const ok = window.confirm(
          'Delete this trade from your record permanently?\n\n'
          + 'Use this only for test rows. Deleting a real trade makes your '
          + 'track record a lie.');
        if (ok) send({ type: 'delete_position', position_id: button.dataset.id });
      });
    });
  }

  drawEquity(history.daily || []);
}

/** Cumulative realised P&L by session. A single day says nothing; the shape of
 *  this line is the only honest picture of whether the system works. */
function drawEquity(series) {
  const canvas = $('equity');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const wrap = canvas.parentElement;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(wrap.clientWidth * ratio));
  canvas.height = Math.max(1, Math.floor(wrap.clientHeight * ratio));
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  ctx.clearRect(0, 0, width, height);

  setText($('equity-note'), series.length
    ? `${series.length} session(s) with closed trades. Each point is that `
      + `session's running total, net of costs.`
    : 'Nothing to plot yet \u2014 close a trade and the curve starts here.');

  if (series.length < 1) {
    ctx.fillStyle = cssVar('--text-muted');
    ctx.font = '13px ' + cssVar('--font');
    ctx.textAlign = 'center';
    ctx.fillText('No closed trades yet', width / 2, height / 2);
    return;
  }

  const pad = { top: 14, right: 62, bottom: 22, left: 8 };
  const values = series.map((d) => d.cumulative);
  let low = Math.min(0, ...values);
  let high = Math.max(0, ...values);
  if (high === low) { high += 1; low -= 1; }
  const span = high - low;
  low -= span * 0.12;
  high += span * 0.12;

  const x = (i) => (series.length === 1
    ? (pad.left + width - pad.right) / 2
    : pad.left + ((width - pad.left - pad.right) * i) / (series.length - 1));
  const y = (v) => (height - pad.bottom)
    - ((v - low) / (high - low)) * (height - pad.top - pad.bottom);

  // Zero line: the only reference that matters on a P&L curve.
  const zeroY = y(0);
  ctx.strokeStyle = cssVar('--baseline');
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, Math.round(zeroY) + 0.5);
  ctx.lineTo(width - pad.right, Math.round(zeroY) + 0.5);
  ctx.stroke();

  ctx.fillStyle = cssVar('--text-muted');
  ctx.font = '11px ' + cssVar('--font');
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText('\u20B90', width - pad.right + 6, zeroY);
  const last = values[values.length - 1];
  ctx.fillText(`\u20B9${Math.round(last).toLocaleString('en-IN')}`,
    width - pad.right + 6, y(last));

  const positive = last >= 0;
  const colour = positive ? cssVar('--good') : cssVar('--critical');

  // Fill between the curve and zero, so time spent underwater is obvious.
  ctx.beginPath();
  ctx.moveTo(x(0), zeroY);
  series.forEach((d, i) => ctx.lineTo(x(i), y(d.cumulative)));
  ctx.lineTo(x(series.length - 1), zeroY);
  ctx.closePath();
  ctx.fillStyle = positive ? cssVar('--good-wash') : cssVar('--critical-wash');
  ctx.fill();

  ctx.beginPath();
  series.forEach((d, i) => (i ? ctx.lineTo(x(i), y(d.cumulative))
    : ctx.moveTo(x(i), y(d.cumulative))));
  ctx.strokeStyle = colour;
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.stroke();

  series.forEach((d, i) => {
    ctx.beginPath();
    ctx.arc(x(i), y(d.cumulative), 3.5, 0, Math.PI * 2);
    ctx.fillStyle = colour;
    ctx.fill();
    // 2px surface ring so overlapping points stay separable.
    ctx.strokeStyle = cssVar('--surface-1');
    ctx.lineWidth = 2;
    ctx.stroke();
  });

  ctx.fillStyle = cssVar('--text-muted');
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.fillText((series[0].session_date || '').slice(5), x(0), height - pad.bottom + 6);
  if (series.length > 1) {
    ctx.fillText((series[series.length - 1].session_date || '').slice(5),
      x(series.length - 1), height - pad.bottom + 6);
  }
}

function renderGlossary(snapshot) {
  const container = $('glossary');
  if (!container || container.dataset.filled === '1') return;
  const terms = snapshot.glossary || [];
  if (!terms.length) return;
  container.innerHTML = terms.map((t) => `
    <div class="gloss">
      <div class="gloss__term">${escapeHtml(t.term)}</div>
      <div class="gloss__plain">${escapeHtml(t.plain)}</div>
      <p class="gloss__detail">${escapeHtml(t.detail)}</p>
      <p class="gloss__use"><b>Use it for:</b> ${escapeHtml(t.use)}</p>
    </div>`).join('');
  container.dataset.filled = '1';
}

function renderHeader(snapshot) {
  const pill = $('mode-pill');
  const modeClass = {
    LIVE: 'pill pill--live', ESTIMATED: 'pill pill--estimated',
    CLOSED: 'pill pill--closed',
  }[snapshot.mode] || 'pill pill--closed';
  pill.className = modeClass;
  setText($('mode-text'), snapshot.mode);
  setText($('engine-version'), snapshot.engine_version || '');
  setText($('clock-time'), (snapshot.ts || '').slice(11, 19));
  setText($('compute-ms'), `· ${snapshot.compute_ms}ms`);

  const squareoff = $('squareoff');
  const remaining = snapshot.minutes_to_square_off;
  if (remaining === null || remaining === undefined) {
    squareoff.hidden = true;
  } else {
    squareoff.hidden = false;
    squareoff.className = remaining <= 15 ? 'pill pill--danger' : 'pill pill--closed';
    setText($('squareoff-text'), `⏰ ${remaining}m to square-off`);
  }

  const banner = $('mode-banner');
  if (snapshot.mode === 'LIVE') {
    banner.innerHTML = '';
  } else {
    banner.innerHTML = `
      <div class="banner">
        <span>🟡</span>
        <span><b>Estimated data.</b> The broker feed is not live
        ${snapshot.market_open ? '' : '(market is closed)'}, so premiums are
        modelled estimates. Nothing here is actionable, and none of it enters
        the performance statistics — recorded rows are flagged synthetic and
        excluded.</span>
      </div>`;
  }
}

// ----------------------------------------------------------------- chart ---

const chart = {
  canvas: null,
  ctx: null,
  bars: [],
  forming: null,
  // Animated value of the forming candle's close, eased toward the real one so
  // the bar grows smoothly instead of teleporting between snapshots.
  formingClose: null,
  formingTarget: null,
  hover: null,
  raf: null,
  phase: 0,
};

const PADDING = { top: 14, right: 66, bottom: 26, left: 10 };

function initChart() {
  chart.canvas = $('chart');
  chart.ctx = chart.canvas.getContext('2d');

  chart.canvas.addEventListener('mousemove', (event) => {
    const rect = chart.canvas.getBoundingClientRect();
    chart.hover = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    drawChart();
  });
  chart.canvas.addEventListener('mouseleave', () => {
    chart.hover = null;
    $('chart-tooltip').classList.remove('is-visible');
    drawChart();
  });

  const observer = new ResizeObserver(() => { sizeCanvas(); drawChart(); });
  observer.observe($('chart-wrap'));
  sizeCanvas();
  tick();
}

function sizeCanvas() {
  const wrap = $('chart-wrap');
  const ratio = window.devicePixelRatio || 1;
  chart.canvas.width = Math.max(1, Math.floor(wrap.clientWidth * ratio));
  chart.canvas.height = Math.max(1, Math.floor(wrap.clientHeight * ratio));
  chart.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
}

/** Animation loop. Runs only while there is something to animate, so an idle
 *  dashboard does not burn CPU. */
function tick() {
  let animating = false;

  if (chart.formingTarget !== null && chart.formingClose !== null) {
    const delta = chart.formingTarget - chart.formingClose;
    if (Math.abs(delta) > 0.01) {
      chart.formingClose += delta * (REDUCED_MOTION ? 1 : 0.16);
      animating = true;
    } else {
      chart.formingClose = chart.formingTarget;
    }
  }
  if (chart.forming && !REDUCED_MOTION) {
    chart.phase = (chart.phase + 0.03) % (Math.PI * 2);
    animating = true;
  }
  if (animating) drawChart();
  chart.raf = requestAnimationFrame(tick);
}

function setChartData(signal) {
  const chartData = (signal && signal.chart) || { closed: [], forming: null };
  chart.signal = signal || null;
  chart.bars = chartData.closed || [];
  chart.forming = chartData.forming || null;

  if (chart.forming) {
    chart.formingTarget = chart.forming.close;
    if (chart.formingClose === null) chart.formingClose = chart.forming.open;
  } else {
    chart.formingClose = chart.formingTarget = null;
  }
  renderChartTable();
  drawChart();
}

// ----------------------------------------------------- what to buy, on chart ---

/** The reference levels worth drawing, in INDEX POINTS.
 *
 *  Units discipline: this chart's y-axis is the index, while entry, stop and
 *  target are option PREMIUMS. Drawing a Rs 172.80 premium onto a 22,750-point
 *  axis would be a category error, so premiums belong in the ticket panel and
 *  only genuine index levels become lines.
 *
 *  Tone follows what a level means FOR THIS TRADE, not what it is: the wall you
 *  take profit into is good news, the one standing in your way is not.
 */
function overlayLevels(signal) {
  if (!signal) return [];
  const dir = signal.direction;
  const levels = (signal.regime && signal.regime.levels) || {};
  const out = [];

  if (signal.strike > 0 && dir !== 'WAIT') {
    out.push({
      value: signal.strike, tone: 'neutral', weight: 2,
      label: `STRIKE ${signal.strike.toFixed(0)} ${signal.option_type || ''}`.trim(),
    });
  }
  // A PUT books into the put wall below it; a CALL into the call wall above.
  if (signal.pe_wall > 0) {
    out.push({
      value: signal.pe_wall, weight: 1,
      tone: dir === 'PUT' ? 'good' : dir === 'CALL' ? 'bad' : 'neutral',
      label: `PUT WALL ${signal.pe_wall.toFixed(0)}${
        dir === 'PUT' ? ' - book here' : ''}`,
    });
  }
  if (signal.ce_wall > 0) {
    out.push({
      value: signal.ce_wall, weight: 1,
      tone: dir === 'CALL' ? 'good' : dir === 'PUT' ? 'bad' : 'neutral',
      label: `CALL WALL ${signal.ce_wall.toFixed(0)}${
        dir === 'CALL' ? ' - book here' : ''}`,
    });
  }
  if (levels.prev_close > 0) {
    out.push({
      value: levels.prev_close, tone: 'muted', weight: 1,
      label: `PREV CLOSE ${levels.prev_close.toFixed(0)}`,
    });
  }
  return out;
}

const LEVEL_INK = {
  good: '--good', bad: '--critical', neutral: '--text-secondary',
  muted: '--text-muted',
};

/** Horizontal reference lines, with an edge chip for anything off-scale. */
function drawLevels(ctx, g) {
  const all = overlayLevels(chart.signal);
  if (!all.length) return;

  ctx.save();
  ctx.font = '10px ' + cssVar('--font');
  ctx.textBaseline = 'middle';
  // Explicit: textAlign is canvas-global and drawGrid leaves it on 'right'.
  // save() preserves the INHERITED value, it does not reset to the default,
  // so every label here was right-aligned and ran off the left edge.
  ctx.textAlign = 'left';
  const spot = (chart.signal && chart.signal.spot) || 0;

  for (const level of all) {
    const ink = cssVar(LEVEL_INK[level.tone] || '--text-muted');
    const inside = level.value >= g.low && level.value <= g.high;

    if (!inside) {
      // Off-scale: say which way and how far rather than silently dropping a
      // level the trade depends on.
      const above = level.value > g.high;
      const y = above ? g.plotTop + 8 : g.plotBottom - 8;
      const pts = spot ? Math.abs(level.value - spot) : 0;
      const text = `${above ? '↑' : '↓'} ${level.label}${
        pts ? ` (${pts.toFixed(0)} pts away)` : ''}`;
      const w = ctx.measureText(text).width + 12;
      ctx.fillStyle = cssVar('--surface-2');
      ctx.globalAlpha = 0.92;
      roundRect(ctx, g.plotLeft + 4, y - 8, w, 16, 4);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.fillStyle = ink;
      ctx.fillText(text, g.plotLeft + 10, y);
      continue;
    }

    const y = g.y(level.value);
    const w = ctx.measureText(level.label).width + 12;

    // The right edge already carries the price axis, the EMA/VWAP end labels
    // and the last-price tag, and a chip there collided with "EMA 9". The left
    // edge is free except under the ticket, so prefer left and fall back to
    // right only where the ticket would cover the label.
    const onLeft = y > g.plotTop + ticketHeight(chart.signal) + 8;
    const x = onLeft ? g.plotLeft + 2 : g.plotRight - w - 2;

    // Stop the line where the chip begins: running it edge to edge left a stub
    // poking past the label, which reads as a rendering fault.
    ctx.strokeStyle = ink;
    ctx.lineWidth = level.weight;
    ctx.globalAlpha = level.tone === 'muted' ? 0.45 : 0.7;
    ctx.beginPath();
    if (onLeft) {
      ctx.moveTo(x + w + 4, y);
      ctx.lineTo(g.plotRight, y);
    } else {
      ctx.moveTo(g.plotLeft, y);
      ctx.lineTo(x - 4, y);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;

    ctx.fillStyle = cssVar('--surface-1');
    ctx.globalAlpha = 0.9;
    roundRect(ctx, x, y - 8, w, 16, 4);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = ink;
    ctx.fillText(level.label, x + 6, y);
  }
  ctx.restore();
}

/** The trade ticket: what to buy, at what price, and what it costs you.
 *
 *  This exists because reading a chart plus four panels and arriving at
 *  "buy one lot of the 22750 PE at Rs 172.80" is work the screen should do.
 */
/** The ticket's content, built separately so its height is known before the
 *  levels are drawn -- a level chip must not land underneath the panel. */
function ticketContent(signal) {
  if (!signal) return null;
  const plan = signal.plan || {};
  const agent = signal.agent || {};
  const trading = signal.direction === 'CALL' || signal.direction === 'PUT';
  const lines = [];
  let headline;
  let tone;

  if (!trading) {
    headline = 'NO TRADE — STAND ASIDE';
    tone = '--text-secondary';
    lines.push(signal.agreement === 'conflict'
      ? 'Price and the option chain disagree.'
      : 'No directional edge in the indicators right now.');
    lines.push('Watch the marked levels; act when one breaks with agreement.');
  } else {
    headline = `BUY  ${signal.index} ${signal.strike.toFixed(0)} ${signal.option_type}`;
    tone = signal.direction === 'CALL' ? '--good' : '--critical';
    lines.push(
      `entry ₹${(signal.entry_premium || 0).toFixed(2)}`
      + `    stop ₹${(signal.stop_premium || 0).toFixed(2)}`
      + `    target ₹${(signal.target_premium || 0).toFixed(2)}`
    );
    if (plan.affordable) {
      lines.push(
        `${plan.lots} lot × ${plan.lot_size || ''} = ${
          rupees(plan.deployed_capital)}    ·  one stop costs ${
          percent(plan.risk_pct_of_capital)} of capital`
      );
      lines.push(
        `flat at ₹${(plan.breakeven || 0).toFixed(2)} after costs — `
        + `below that you are losing even while the premium is up`
      );
    } else {
      lines.push(
        `NOT AFFORDABLE — one lot needs ${rupees(plan.capital_per_lot)}, `
        + `you can deploy ${rupees(
          plan.spendable_capital ?? plan.available_capital)}`
      );
    }
  }

  if (agent.stance) {
    const mark = { PROCEED: '✓', CAUTION: '!', VETO: '⛔' }[agent.stance] || '';
    // The agent's verdict is cached per CANDLE (deliberately -- an LLM call
    // every refresh would burn a free tier in an hour), while the plan above
    // re-prices every snapshot as the premium moves. So the agent's own risk
    // percentage goes stale and the panel showed the same fact twice with two
    // different numbers ("12.3% of capital" above, "11.3% of the account"
    // below). The plan is the live source for risk, so drop any concern that
    // merely restates it and show the next one, which adds something.
    const concerns = (agent.concerns || []).filter(
      (c) => !/%\s*of\s*(the\s*)?(account|capital)/i.test(c));
    const first = concerns[0] || '';
    lines.push(`${mark} ${agent.stance}${first ? '  —  ' + first : ''}`);
  }

  return { headline, lines, tone };
}

/** How tall the ticket will be, so drawLevels can keep clear of it. */
function ticketHeight(signal) {
  const content = ticketContent(signal);
  return content ? 20 + 21 + content.lines.length * 16 + 6 : 0;
}

function drawTicket(ctx, g) {
  const content = ticketContent(chart.signal);
  if (!content) return;
  const { headline, lines, tone } = content;

  // Measure first, then draw: the panel fits its content, not the reverse.
  ctx.save();
  ctx.textAlign = 'left';   // see drawLevels: inherited from drawGrid
  const padX = 11;
  const padY = 10;
  const maxWidth = (g.plotRight - g.plotLeft) - 16;
  ctx.font = '600 15px ' + cssVar('--font');
  let boxWidth = ctx.measureText(headline).width;
  ctx.font = '11.5px ' + cssVar('--font');
  for (const line of lines) {
    boxWidth = Math.max(boxWidth, ctx.measureText(line).width);
  }
  boxWidth = Math.min(boxWidth + padX * 2, maxWidth);
  const boxHeight = padY * 2 + 21 + lines.length * 16;
  const x = g.plotLeft + 6;
  const y = g.plotTop + 6;

  ctx.fillStyle = cssVar('--surface-2');
  ctx.globalAlpha = 0.95;
  roundRect(ctx, x, y, boxWidth, boxHeight, 8);
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.strokeStyle = cssVar('--border-strong');
  ctx.lineWidth = 1;
  roundRect(ctx, x + 0.5, y + 0.5, boxWidth - 1, boxHeight - 1, 8);
  ctx.stroke();

  // A spine in the verdict's colour, so the answer reads at a glance.
  ctx.fillStyle = cssVar(tone);
  roundRect(ctx, x, y, 3, boxHeight, 2);
  ctx.fill();

  ctx.textBaseline = 'top';
  ctx.fillStyle = cssVar(tone);
  ctx.font = '600 15px ' + cssVar('--font');
  ctx.fillText(headline, x + padX, y + padY, boxWidth - padX * 2);

  ctx.font = '11.5px ' + cssVar('--font');
  ctx.fillStyle = cssVar('--text-secondary');
  lines.forEach((line, i) => {
    ctx.fillText(line, x + padX, y + padY + 23 + i * 16, boxWidth - padX * 2);
  });
  ctx.restore();
}

function roundRect(ctx, x, y, w, h, r) {
  const radius = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

function chartGeometry() {
  const width = chart.canvas.clientWidth;
  const height = chart.canvas.clientHeight;
  const plotLeft = PADDING.left;
  const plotRight = width - PADDING.right;
  const plotTop = PADDING.top;
  const plotBottom = height - PADDING.bottom;

  const visible = chart.bars.slice();
  const formingBar = chart.forming
    ? { ...chart.forming, close: chart.formingClose ?? chart.forming.close }
    : null;
  const all = formingBar ? visible.concat([formingBar]) : visible;
  if (!all.length) return null;

  let low = Infinity;
  let high = -Infinity;
  for (const bar of all) {
    if (bar.low < low) low = bar.low;
    if (bar.high > high) high = bar.high;
    for (const key of ['vwap', 'ema9', 'ema21']) {
      const value = bar[key];
      if (value !== null && value !== undefined) {
        if (value < low) low = value;
        if (value > high) high = value;
      }
    }
  }
  // Pull a marked level into view only when it is JUST off the edge. The
  // first attempt allowed 45% of the candle span, which dragged the 22,500
  // put wall onto a chart whose candles bottomed at 22,710 and left a quarter
  // of the plot empty -- the exact dead space this chart was widened to
  // remove. Anything further away is worth more as an edge chip (which states
  // the distance) than as a line bought with blank chart.
  const candleSpan = (high - low) || 1;
  for (const level of overlayLevels(chart.signal)) {
    if (level.value < low && (low - level.value) < candleSpan * 0.12) {
      low = level.value;
    } else if (level.value > high && (level.value - high) < candleSpan * 0.12) {
      high = level.value;
    }
  }

  const span = (high - low) || 1;
  low -= span * 0.06;
  high += span * 0.06;

  const count = all.length;
  const step = (plotRight - plotLeft) / Math.max(count, 1);
  const bodyWidth = Math.max(1.5, Math.min(11, step - 2));  // 2px surface gap

  return {
    width, height, plotLeft, plotRight, plotTop, plotBottom,
    low, high, count, step, bodyWidth, all, formingBar,
    x: (i) => plotLeft + step * (i + 0.5),
    y: (v) => plotBottom - ((v - low) / (high - low)) * (plotBottom - plotTop),
  };
}

function drawChart() {
  if (!chart.ctx) return;
  const ctx = chart.ctx;
  const geometry = chartGeometry();

  ctx.clearRect(0, 0, chart.canvas.clientWidth, chart.canvas.clientHeight);
  if (!geometry) {
    ctx.fillStyle = cssVar('--text-muted');
    ctx.font = '13px ' + cssVar('--font');
    ctx.textAlign = 'center';
    ctx.fillText('No candle data', chart.canvas.clientWidth / 2,
      chart.canvas.clientHeight / 2);
    return;
  }

  drawGrid(ctx, geometry);
  drawLevels(ctx, geometry);
  const labels = [
    drawSeriesLine(ctx, geometry, 'vwap', cssVar('--series-1'), 'VWAP'),
    drawSeriesLine(ctx, geometry, 'ema9', cssVar('--series-2'), 'EMA 9'),
    drawSeriesLine(ctx, geometry, 'ema21', cssVar('--series-3'), 'EMA 21'),
  ];
  drawCandles(ctx, geometry);
  drawSeriesLabels(ctx, geometry, labels);
  drawCrosshair(ctx, geometry);
  // Last, so the instruction is never hidden behind a candle.
  drawTicket(ctx, geometry);

  // Tell the legend which series actually rendered. Index candles from Yahoo
  // often carry no volume, so VWAP is NaN and no line is drawn — a legend
  // entry for a series that is not on the chart is misleading.
  const present = new Set(labels.filter(Boolean).map((item) => item.label));
  document.querySelectorAll('.legend__item[data-series]').forEach((node) => {
    const absent = !present.has(node.dataset.series);
    node.style.opacity = absent ? '0.38' : '1';
    node.title = absent ? 'No data for this series in the current window' : '';
  });
}

function drawGrid(ctx, g) {
  // Solid hairlines, one shade off the surface. Never dashed.
  ctx.save();
  ctx.strokeStyle = cssVar('--grid');
  ctx.lineWidth = 1;
  ctx.fillStyle = cssVar('--text-muted');
  ctx.font = '11px ' + cssVar('--font');
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';

  const lines = 5;
  for (let i = 0; i <= lines; i += 1) {
    const value = g.low + ((g.high - g.low) / lines) * i;
    const y = Math.round(g.y(value)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(g.plotLeft, y);
    ctx.lineTo(g.plotRight, y);
    ctx.stroke();
    ctx.fillText(value.toFixed(0), g.plotRight + 7, y);
  }

  // Time axis: a handful of labels, never one per bar.
  ctx.strokeStyle = cssVar('--baseline');
  ctx.beginPath();
  ctx.moveTo(g.plotLeft, Math.round(g.plotBottom) + 0.5);
  ctx.lineTo(g.plotRight, Math.round(g.plotBottom) + 0.5);
  ctx.stroke();

  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';

  // The window spans several sessions, so an HH:MM-only label reads as
  // non-monotonic ("08:00" then "03:45") and looks broken. Include the date
  // whenever the series crosses a day boundary.
  const firstDay = (g.all[0].time || '').slice(0, 10);
  const lastDay = (g.all[g.count - 1].time || '').slice(0, 10);
  const multiDay = firstDay !== lastDay;

  // Tick count follows the available width: six labels that fit on a desktop
  // overlap into an unreadable smear at phone width.
  const plotWidth = g.plotRight - g.plotLeft;
  const maxTicks = plotWidth < 420 ? 3 : plotWidth < 760 ? 4 : 6;
  const ticks = Math.min(maxTicks, g.count);
  let previousDay = null;
  for (let i = 0; i < ticks; i += 1) {
    const index = Math.floor((g.count - 1) * (i / Math.max(ticks - 1, 1)));
    const bar = g.all[index];
    if (!bar) continue;
    const time = bar.time || '';
    const day = time.slice(0, 10);
    const hhmm = time.slice(11, 16);
    let label = hhmm;
    if (multiDay && day !== previousDay) {
      label = `${time.slice(8, 10)}/${time.slice(5, 7)} ${hhmm}`;
    }
    previousDay = day;
    // Anchor the first and last ticks inward; centre-aligning them clips the
    // label against the plot edge ("/10 06:05" instead of "15/10 06:05").
    if (i === 0) ctx.textAlign = 'left';
    else if (i === ticks - 1) ctx.textAlign = 'right';
    else ctx.textAlign = 'center';
    ctx.fillText(label, g.x(index), g.plotBottom + 7);
  }
  ctx.restore();
}

function drawSeriesLine(ctx, g, key, color, label) {
  const points = [];
  g.all.forEach((bar, index) => {
    const value = bar[key];
    if (value !== null && value !== undefined) {
      points.push([g.x(index), g.y(value)]);
    }
  });
  if (points.length < 2) return null;

  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;                      // thin marks
  ctx.lineJoin = 'round';
  ctx.beginPath();
  points.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.stroke();
  ctx.restore();

  // The label is NOT drawn here. Three series ending close together collide
  // (and overlap the last-price badge in the axis gutter), which is exactly
  // the "label clipped / colliding" anti-pattern. Hand the endpoint back so
  // drawSeriesLabels can lay all of them out together.
  return { label, color, y: points[points.length - 1][1] };
}

/** Place the direct labels for all series at once, nudged apart so none
 *  overlaps another, and kept inside the plot so none collides with the price
 *  badge. Direct labels are also the "relief" that lets a sub-3:1 series
 *  colour be used in light mode: identity never rests on hue alone. */
function drawSeriesLabels(ctx, g, entries) {
  const items = entries.filter(Boolean).sort((a, b) => a.y - b.y);
  if (!items.length) return;

  const LINE = 13;
  // Resolve downward, then clamp back up if we ran past the plot bottom.
  for (let i = 1; i < items.length; i += 1) {
    if (items[i].y - items[i - 1].y < LINE) items[i].y = items[i - 1].y + LINE;
  }
  const overflow = items[items.length - 1].y - (g.plotBottom - 4);
  if (overflow > 0) items.forEach((item) => { item.y -= overflow; });
  if (items[0].y < g.plotTop + 8) {
    const shift = g.plotTop + 8 - items[0].y;
    items.forEach((item) => { item.y += shift; });
  }

  ctx.save();
  ctx.font = '600 10px ' + cssVar('--font');
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const item of items) {
    const width = ctx.measureText(item.label).width;
    // A 2px surface pad behind the text keeps it legible over the candles.
    ctx.fillStyle = cssVar('--surface-1');
    ctx.globalAlpha = 0.82;
    ctx.fillRect(g.plotRight - width - 8, item.y - 7, width + 6, 14);
    ctx.globalAlpha = 1;
    ctx.fillStyle = item.color;
    ctx.fillText(item.label, g.plotRight - 5, item.y);
  }
  ctx.restore();
}

function drawCandles(ctx, g) {
  const up = cssVar('--candle-up');
  const down = cssVar('--candle-down');

  g.all.forEach((bar, index) => {
    const isForming = g.formingBar && index === g.count - 1;
    const close = isForming ? (chart.formingClose ?? bar.close) : bar.close;
    const rising = close >= bar.open;
    const color = rising ? up : down;
    const x = g.x(index);

    ctx.save();
    // A forming bar is drawn softer and outlined, so a decision is never
    // mistaken for having been made on it.
    if (isForming) ctx.globalAlpha = 0.55 + 0.35 * Math.sin(chart.phase);

    // Wick
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, g.y(Math.max(bar.high, close)));
    ctx.lineTo(Math.round(x) + 0.5, g.y(Math.min(bar.low, close)));
    ctx.stroke();

    // Body
    const yOpen = g.y(bar.open);
    const yClose = g.y(close);
    const top = Math.min(yOpen, yClose);
    const bodyHeight = Math.max(1, Math.abs(yClose - yOpen));
    ctx.fillStyle = color;
    ctx.fillRect(x - g.bodyWidth / 2, top, g.bodyWidth, bodyHeight);

    if (isForming) {
      ctx.globalAlpha = 1;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x - g.bodyWidth / 2 - 1.5, top - 1.5,
        g.bodyWidth + 3, bodyHeight + 3);
    }
    ctx.restore();
  });

  // Last price marker on the axis.
  const lastBar = g.all[g.count - 1];
  if (lastBar) {
    const close = g.formingBar
      ? (chart.formingClose ?? lastBar.close) : lastBar.close;
    const y = g.y(close);
    ctx.save();
    ctx.fillStyle = close >= lastBar.open ? cssVar('--candle-up') : cssVar('--candle-down');
    ctx.fillRect(g.plotRight + 2, y - 9, PADDING.right - 6, 18);
    ctx.fillStyle = '#ffffff';
    ctx.font = '600 11px ' + cssVar('--font');
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(close.toFixed(1), g.plotRight + 2 + (PADDING.right - 6) / 2, y);
    ctx.restore();
  }
}

function drawCrosshair(ctx, g) {
  const tooltip = $('chart-tooltip');
  if (!chart.hover || chart.hover.x < g.plotLeft || chart.hover.x > g.plotRight) {
    tooltip.classList.remove('is-visible');
    return;
  }
  const index = Math.max(0, Math.min(g.count - 1,
    Math.round((chart.hover.x - g.plotLeft) / g.step - 0.5)));
  const bar = g.all[index];
  if (!bar) return;

  ctx.save();
  ctx.strokeStyle = cssVar('--text-muted');
  ctx.lineWidth = 1;
  ctx.globalAlpha = 0.55;
  ctx.beginPath();
  ctx.moveTo(Math.round(g.x(index)) + 0.5, g.plotTop);
  ctx.lineTo(Math.round(g.x(index)) + 0.5, g.plotBottom);
  ctx.stroke();
  ctx.restore();

  const close = (g.formingBar && index === g.count - 1)
    ? (chart.formingClose ?? bar.close) : bar.close;
  const row = (label, value) =>
    `<div class="tooltip__row"><span>${label}</span><b>${value}</b></div>`;

  tooltip.innerHTML = `
    <div class="tooltip__time">${escapeHtml((bar.time || '').slice(0, 16))}${
      (g.formingBar && index === g.count - 1) ? ' · forming' : ''}</div>
    ${row('Open', bar.open.toFixed(2))}
    ${row('High', bar.high.toFixed(2))}
    ${row('Low', bar.low.toFixed(2))}
    ${row('Close', close.toFixed(2))}
    ${bar.vwap != null ? row('VWAP', bar.vwap.toFixed(2)) : ''}
    ${bar.ema9 != null ? row('EMA 9', bar.ema9.toFixed(2)) : ''}
    ${bar.ema21 != null ? row('EMA 21', bar.ema21.toFixed(2)) : ''}`;

  tooltip.classList.add('is-visible');
  const wrapWidth = $('chart-wrap').clientWidth;
  const tipWidth = tooltip.offsetWidth;
  let left = g.x(index) + 14;
  if (left + tipWidth > wrapWidth) left = g.x(index) - tipWidth - 14;
  tooltip.style.left = `${Math.max(0, left)}px`;
  tooltip.style.top = `${Math.max(0, Math.min(chart.hover.y - 10,
    $('chart-wrap').clientHeight - tooltip.offsetHeight - 4))}px`;
}

function renderChartTable() {
  const body = $('chart-table-body');
  if (!body) return;
  const rows = chart.bars.slice(-40).reverse();
  body.innerHTML = rows.map((bar) => `
    <tr>
      <td>${escapeHtml((bar.time || '').slice(0, 16))}</td>
      <td class="num">${bar.open.toFixed(2)}</td>
      <td class="num">${bar.high.toFixed(2)}</td>
      <td class="num">${bar.low.toFixed(2)}</td>
      <td class="num">${bar.close.toFixed(2)}</td>
      <td class="num">${bar.vwap == null ? '—' : bar.vwap.toFixed(2)}</td>
      <td class="num">${bar.ema9 == null ? '—' : bar.ema9.toFixed(2)}</td>
      <td class="num">${bar.ema21 == null ? '—' : bar.ema21.toFixed(2)}</td>
    </tr>`).join('');
}

/** A text twin of the on-chart ticket.
 *
 *  The chart is a <canvas role="img">, so anything drawn only in pixels is
 *  invisible to a screen reader, to the table view, and to anyone who cannot
 *  separate the colours. The instruction is the one thing on this screen that
 *  must never be pixels-only, so it is also written out here and into the
 *  canvas's own aria-label.
 */
function chartGuideText(signal, snapshot) {
  if (!signal) return '';
  const plan = signal.plan || {};
  const parts = [];

  if (signal.direction === 'CALL' || signal.direction === 'PUT') {
    parts.push(
      `What to buy: ${signal.index} ${signal.strike} ${signal.option_type} `
      + `near ₹${(signal.entry_premium || 0).toFixed(2)}. `
      + `Stop ₹${(signal.stop_premium || 0).toFixed(2)}, `
      + `target ₹${(signal.target_premium || 0).toFixed(2)}.`
    );
    if (plan.affordable) {
      parts.push(
        `Size ${plan.lots} lot of ${plan.lot_size} = ${
          rupees(plan.deployed_capital)}; one stop-out costs ${
          percent(plan.risk_pct_of_capital)} of capital. You break even at `
        + `₹${(plan.breakeven || 0).toFixed(2)}, not at the entry.`
      );
    } else {
      parts.push(
        `Not affordable: one lot needs ${rupees(plan.capital_per_lot)} and you `
        + `can deploy ${rupees(plan.spendable_capital ?? plan.available_capital)}.`
      );
    }
  } else {
    parts.push('No trade: the indicators give no directional edge, so the '
      + 'marked levels are what to watch rather than something to buy.');
  }

  const levels = overlayLevels(signal);
  if (levels.length) {
    parts.push('Levels marked on the chart: '
      + levels.map((l) => `${l.label} at ${l.value.toFixed(0)}`).join('; ') + '.');
  }
  const agent = signal.agent || {};
  if (agent.stance) {
    parts.push(`Risk check: ${agent.stance}${
      agent.concerns && agent.concerns.length
        ? ' — ' + agent.concerns[0] : ''}`);
  }
  const forming = signal.chart && signal.chart.forming;
  parts.push(forming
    ? `The outlined bar is still forming — shown but never scored, because `
      + `signals come only from closed candles. Interval ${snapshot.interval}.`
    : `All candles closed. Interval ${snapshot.interval}.`);
  return parts.join(' ');
}

function renderChartChrome(snapshot) {
  const tabs = $('chart-tabs');
  if (!state.selectedIndex && snapshot.signals.length) {
    state.selectedIndex = snapshot.signals[0].index;
  }
  tabs.innerHTML = snapshot.signals.map((signal) => `
    <button class="chart-tab" role="tab" data-index="${escapeHtml(signal.index)}"
            aria-selected="${signal.index === state.selectedIndex}">
      ${escapeHtml(signal.index)}
    </button>`).join('');
  tabs.querySelectorAll('.chart-tab').forEach((button) => {
    button.addEventListener('click', () => {
      state.selectedIndex = button.dataset.index;
      renderChartChrome(state.snapshot);
      const signal = state.snapshot.signals.find(
        (s) => s.index === state.selectedIndex);
      setChartData(signal);
    });
  });

  // Legend always present for ≥2 series.
  $('chart-legend').innerHTML = `
    <span class="legend__item">
      <span class="legend__swatch legend__swatch--candle"
            style="background:var(--candle-up)"></span>Up</span>
    <span class="legend__item">
      <span class="legend__swatch legend__swatch--candle"
            style="background:var(--candle-down)"></span>Down</span>
    <span class="legend__item" data-series="VWAP">
      <span class="legend__swatch" style="background:var(--series-1)"></span>VWAP</span>
    <span class="legend__item" data-series="EMA 9">
      <span class="legend__swatch" style="background:var(--series-2)"></span>EMA 9</span>
    <span class="legend__item" data-series="EMA 21">
      <span class="legend__swatch" style="background:var(--series-3)"></span>EMA 21</span>`;

  const signal = snapshot.signals.find((s) => s.index === state.selectedIndex);
  const forming = signal && signal.chart && signal.chart.forming;
  const selected = snapshot.signals.find((s) => s.index === state.selectedIndex);
  const guide = chartGuideText(selected, snapshot);
  setText($('chart-note'), guide || (forming
    ? `Outlined bar is still forming — signals are computed only from closed candles, so it is shown but never scored. Interval ${snapshot.interval}.`
    : `All candles closed. Interval ${snapshot.interval}.`));
  // The canvas is role="img": its label has to carry the same instruction.
  const canvas = $('chart');
  if (canvas && guide) canvas.setAttribute('aria-label', guide);
}

// ------------------------------------------------------------- commands ----

function send(message) {
  if (state.socket && state.socket.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(message));
  }
}

/** What the deploy slider should say about risk.
 *
 *  It used to say `stop_pct * deploy_pct`, which is the arithmetic for a
 *  position you can size to the rupee. An option lot cannot be subdivided, so
 *  that figure was wrong in BOTH directions, as measured on 2026-10-21:
 *
 *    deploy 100% -> label promised 20.0%, the sized plan risked 11.5%
 *    deploy   5% -> label promised  1.0%, the plan was UNAFFORDABLE
 *
 *  A risk number a trader sizes from must come from the sized position, not
 *  from an intention. So: report the measured worst case across the live
 *  plans, and while dragging (before the recompute lands) state the cap as a
 *  rupee budget -- a fact -- rather than a risk the position may not honour.
 */
function deployHint({ pending = false } = {}) {
  const capital = Number($('capital').value) || 0;
  const pct = Number($('deploy').value) || 0;
  const budget = capital * (pct / 100);

  if (pending) return `deploying at most ${rupees(budget)} — recomputing…`;

  const plans = (state.snapshot?.signals || [])
    .map((s) => s.plan)
    .filter(Boolean);
  if (!plans.length) return `deploying at most ${rupees(budget)}`;

  const sized = plans.filter((p) => p.affordable);
  if (!sized.length) {
    const cheapest = Math.min(...plans.map((p) => p.capital_per_lot || Infinity));
    return `no contract fits ${rupees(budget)} — cheapest lot is ${
      rupees(cheapest)}`;
  }
  // The worst case is what matters: it is the one that can hurt.
  const worst = Math.max(...sized.map((p) => p.risk_pct_of_capital || 0));
  return `one stop costs ${(worst * 100).toFixed(1)}% of capital — measured, `
    + `not estimated (one lot is indivisible)`;
}

function pushSettings() {
  send({
    type: 'settings',
    market: $('market').value || undefined,
    available_capital: Number($('capital').value) || 0,
    max_capital_fraction: Number($('deploy').value) / 100,
    target_pct: Number($('target').value) / 100,
    stop_pct: Number($('stop').value) / 100,
    record: $('record').checked,
    refresh_seconds: Number($('refresh').value),
  });
}

function openPositionFromSignal(indexName) {
  const signal = state.snapshot.signals.find((s) => s.index === indexName);
  if (!signal || !signal.plan) return;
  const lots = window.prompt(
    `How many lots of ${signal.index} ${signal.strike} ${signal.option_type} `
    + `did you buy?\n(plan suggests ${signal.plan.lots})`,
    String(signal.plan.lots));
  if (lots === null) return;
  const premium = window.prompt(
    'Fill premium actually paid (₹):', signal.entry_premium.toFixed(2));
  if (premium === null) return;

  send({
    type: 'open_position',
    index_name: signal.index,
    option_type: signal.option_type,
    strike: signal.strike,
    lot_size: signal.lot_size,
    lots: Number(lots),
    entry_premium: Number(premium),
    target_pct: signal.target_pct,
    stop_pct: signal.stop_pct,
    option_token: signal.option_token || null,
    signal_id: signal.signal_id || null,
  });
}

function closePosition(positionId, suggestedPremium) {
  const premium = window.prompt('Exit premium (₹):', suggestedPremium || '');
  if (premium === null) return;
  send({ type: 'close_position', position_id: positionId,
         exit_premium: Number(premium) });
}

// ------------------------------------------------------------- socket -----

function connect() {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${protocol}://${location.host}/ws`);
  state.socket = socket;

  socket.addEventListener('open', () => {
    state.connected = true;
    state.reconnectDelay = 1000;
    $('conn-lost').hidden = true;
    document.querySelector('.app').classList.remove('is-stale');
    // Deliberately does NOT push settings here. The server owns the initial
    // state (it has the command-line flags); pushing the form's defaults on
    // connect silently overrode them -- a server started with --no-record
    // still recorded, because the checkbox defaults to checked.
  });

  socket.addEventListener('message', (message) => {
    let payload;
    try {
      payload = JSON.parse(message.data);
    } catch (err) {
      // Never swallow this. A silent `catch { return; }` here once hid a
      // server emitting bare NaN (valid for Python's json, rejected by
      // JSON.parse): every snapshot was dropped, nothing threw, and the UI sat
      // on "CONNECTING" with no clue anywhere as to why.
      console.error('Snapshot dropped — invalid JSON from server:', err);
      showToast({
        kind: 'RISK_HALT', severity: 'danger', title: 'Bad data from server',
        message: 'A snapshot could not be parsed and was discarded. '
          + 'Check the server log.',
        ts: new Date().toISOString(), sticky: true,
      });
      return;
    }
    if (payload.type === 'snapshot') applySnapshot(payload);
    else if (payload.type === 'ticks') applyTicks(payload);
    else if (payload.type === 'ack' && payload.message) {
      showToast({ kind: 'SIGNAL_NEW', severity: 'good', title: payload.title
        || 'Done', message: payload.message, ts: new Date().toISOString() });
    } else if (payload.type === 'error') {
      showToast({ kind: 'RISK_HALT', severity: 'danger', title: 'Command failed',
        message: payload.message, ts: new Date().toISOString(), sticky: true });
    }
  });

  const dropped = () => {
    state.connected = false;
    $('conn-lost').hidden = false;
    // Hold the last render at reduced opacity rather than flashing a skeleton.
    document.querySelector('.app').classList.add('is-stale');
    setTimeout(connect, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 1.6, 15000);
  };
  socket.addEventListener('close', dropped);
  socket.addEventListener('error', () => socket.close());
}

/** Adopt the server's settings into the controls, once, on first snapshot.
 *  After that the user owns them and the server follows. */
function syncControls(settings) {
  if (!settings || state.controlsSynced) return;
  state.controlsSynced = true;

  $('capital').value = settings.available_capital ?? 20000;
  $('target').value = Math.round((settings.target_pct ?? 0.25) * 100);
  $('stop').value = Math.round((settings.stop_pct ?? 0.20) * 100);
  $('record').checked = settings.record !== false;

  const deploy = Math.round((settings.max_capital_fraction ?? 1) * 100);
  $('deploy').value = deploy;
  setText($('deploy-val'), `${deploy}%`);
  setText($('deploy-hint'), deployHint());
}

/** Move the chart to the live price between snapshots.
 *
 *  The chart is built from candles that run minutes behind the market, and a
 *  snapshot only lands every 10-20 seconds. Without this the graph looks
 *  frozen, which is what "the graph is slow" actually meant -- the canvas
 *  redraw itself measures 0.5 ms.
 */
function applyTicks(payload) {
  if (!state.snapshot) return;
  const prices = payload.prices || {};

  // Open positions ride this same clock. Their P&L, status and alarms are
  // computed by the engine, not here -- a second copy of that arithmetic in
  // the browser would drift from the one the ledger records.
  if (Array.isArray(payload.positions)) {
    state.snapshot.positions = payload.positions;
    state.snapshot.tick_priced = payload.priced;
    updatePositionsLive(payload.positions);
  }
  (payload.events || []).forEach((event) => {
    const key = `${event.kind}:${event.subject}:${event.ts}`;
    if (state.seenEvents.has(key)) return;
    state.seenEvents.add(key);
    showToast(event);
  });

  let touched = false;

  for (const signal of state.snapshot.signals || []) {
    const tick = prices[signal.index];
    if (!tick || !tick.price) continue;
    signal.spot = tick.price;
    signal.spot_is_tick = true;

    const forming = signal.chart && signal.chart.forming;
    if (forming) {
      // Extend the forming bar the way the exchange would: close moves,
      // high and low ratchet outward, open never changes.
      forming.close = tick.price;
      forming.high = Math.max(forming.high, tick.price);
      forming.low = Math.min(forming.low, tick.price);
    }
    if (signal.index === state.selectedIndex) touched = true;
  }

  if (touched) {
    const signal = state.snapshot.signals.find(
      (s) => s.index === state.selectedIndex);
    if (signal) {
      chart.forming = (signal.chart && signal.chart.forming) || null;
      chart.formingTarget = chart.forming ? chart.forming.close : null;
      if (chart.forming && chart.formingClose === null) {
        chart.formingClose = chart.forming.open;
      }
    }
  }
  renderLiveSpots(prices);
}

/** Update the spot figure on each card without re-rendering the card. */
function renderLiveSpots(prices) {
  document.querySelectorAll('[data-spot-for]').forEach((node) => {
    const tick = prices[node.dataset.spotFor];
    if (tick && tick.price) {
      setText(node, tick.price.toLocaleString('en-IN', {
        minimumFractionDigits: 2, maximumFractionDigits: 2 }));
      node.title = `live tick, ${tick.age}s old`;
    }
  });
}

function applySnapshot(snapshot) {
  state.snapshot = snapshot;
  syncControls(snapshot.settings);
  document.querySelector('.app').classList.remove('is-stale');

  renderHeader(snapshot);
  renderSignals(snapshot);
  // Derived from the plans, so it refreshes every snapshot. syncControls is
  // latched to run once (it must not overwrite what the user is typing), and
  // that latch previously froze this hint on the first snapshot's answer.
  setText($('deploy-hint'), deployHint());
  renderKpis(snapshot);
  renderPositions(snapshot);
  renderMarketPicker(snapshot);
  renderTickBadge(snapshot);
  renderPremarket(snapshot);
  renderRegime(snapshot);
  renderEdge(snapshot);
  renderStocks(snapshot);
  renderJournal(snapshot);
  renderGlossary(snapshot);
  renderChartChrome(snapshot);

  const selected = snapshot.signals.find((s) => s.index === state.selectedIndex);
  setChartData(selected);

  (snapshot.events || []).forEach((event) => {
    const key = `${event.kind}:${event.subject}:${event.ts}`;
    if (state.seenEvents.has(key)) return;
    state.seenEvents.add(key);
    showToast(event);
  });
  if (state.seenEvents.size > 400) state.seenEvents.clear();
}

// --------------------------------------------------------------- controls --

function wireControls() {
  ['capital', 'target', 'stop'].forEach((id) => {
    $(id).addEventListener('change', pushSettings);
  });
  $('refresh').addEventListener('change', pushSettings);
  $('market').addEventListener('change', pushSettings);
  $('record').addEventListener('change', pushSettings);

  const deploy = $('deploy');
  deploy.addEventListener('input', () => {
    setText($('deploy-val'), `${deploy.value}%`);
    setText($('deploy-hint'), deployHint({ pending: true }));
  });
  deploy.addEventListener('change', pushSettings);

  $('theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('desk-theme', next); } catch { /* private mode */ }
    drawChart();
  });

  const sound = $('sound-toggle');
  sound.addEventListener('click', () => {
    state.soundOn = !state.soundOn;
    sound.setAttribute('aria-pressed', String(state.soundOn));
    sound.textContent = state.soundOn ? '🔔' : '🔕';
    sound.title = state.soundOn ? 'Alert sound on' : 'Alert sound off';
    try { localStorage.setItem('desk-sound', state.soundOn ? '1' : '0'); } catch { /**/ }
    if (state.soundOn) beep('info');
  });

  $('view-toggle').addEventListener('click', () => {
    state.tableView = !state.tableView;
    $('chart-wrap').hidden = state.tableView;
    $('chart-table').hidden = !state.tableView;
    $('view-toggle').textContent = state.tableView ? 'Chart view' : 'Table view';
    if (!state.tableView) { sizeCanvas(); drawChart(); }
  });

  $('stock-universe').addEventListener('change', () => {
    const select = $('stock-universe');
    select.dataset.touched = '1';
    send({ type: 'settings', stock_universe: select.value });
  });

  $('premarket-toggle').addEventListener('click', () => {
    const panel = $('premarket-headlines');
    panel.hidden = !panel.hidden;
    $('premarket-toggle').textContent = panel.hidden ? 'Headlines' : 'Hide headlines';
  });

  $('regime-toggle').addEventListener('click', () => {
    const panel = $('regime-detail');
    panel.hidden = !panel.hidden;
    $('regime-toggle').textContent = panel.hidden ? 'Detail' : 'Hide detail';
  });

  $('glossary-toggle').addEventListener('click', () => {
    const panel = $('glossary');
    panel.hidden = !panel.hidden;
    $('glossary-toggle').textContent = panel.hidden ? 'Show guide' : 'Hide guide';
  });

  $('add-position').addEventListener('click', () => {
    if (!state.snapshot) return;
    const tradable = state.snapshot.signals.filter(
      (s) => s.direction === 'CALL' || s.direction === 'PUT');
    if (!tradable.length) {
      showToast({ kind: 'SIGNAL_CLEARED', severity: 'info',
        title: 'No tradable signal',
        message: 'Both indices read WAIT. Open the card of the contract you '
          + 'traded once a signal appears, or add it after one does.',
        ts: new Date().toISOString() });
      return;
    }
    openPositionFromSignal(tradable[0].index);
  });
}

function restorePreferences() {
  try {
    const theme = localStorage.getItem('desk-theme');
    if (theme) document.documentElement.dataset.theme = theme;
    if (localStorage.getItem('desk-sound') === '1') {
      state.soundOn = true;
      $('sound-toggle').setAttribute('aria-pressed', 'true');
    } else {
      $('sound-toggle').textContent = '🔕';
    }
  } catch {
    /* localStorage can throw in private mode; defaults are fine */
  }
}

// Local clock keeps ticking between snapshots so the header never looks frozen.
function startClock() {
  setInterval(() => {
    if (!state.connected) return;
    const now = new Date();
    const ist = new Date(now.getTime() + (now.getTimezoneOffset() + 330) * 60000);
    setText($('clock-time'), ist.toTimeString().slice(0, 8));
  }, 1000);
}

/** The canvas caches whatever colours were current when it last painted, so a
 *  theme change has to repaint it explicitly. The toggle button does that
 *  directly; this covers every other route (a restored preference, devtools,
 *  another script) so the chart can never be left in the previous theme's
 *  palette on the new surface. */
function watchTheme() {
  new MutationObserver(() => {
    drawChart();
    if (state.snapshot) drawEquity((state.snapshot.history || {}).daily || []);
  }).observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme'],
  });
}

document.addEventListener('DOMContentLoaded', () => {
  restorePreferences();
  wireControls();
  initChart();
  watchTheme();
  startClock();
  connect();
});
