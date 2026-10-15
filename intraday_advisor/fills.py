"""How a bar resolves a stop or a target. The single copy of these rules.

Both the live grader (:mod:`intraday_advisor.ledger.grader`) and the historical
backtest (:mod:`intraday_advisor.backtest`) decide exits with this function.
That is deliberate: two implementations of fill logic always drift, and the
backtest copy always drifts optimistic, because nobody notices a backtest that
got better.

The rules, and what each one prevents
-------------------------------------

* **Stops test against the bar's LOW, targets against its HIGH.** Comparing
  only ``Close`` -- which this project's backtest used to do -- makes an
  intrabar spike through the stop that closes back above it record no loss at
  all. Those stop-outs are invisible, and the equity curve is too good by
  however often it happens.

* **Both touched in one bar => the STOP wins.** Bar data cannot say which came
  first. Assuming the target is a free win-rate boost that disappears the
  moment real money is on it. The caller is told via ``conflict`` so the
  frequency stays measurable rather than hidden.

* **A bar that gaps through a level fills at the OPEN, not at the level.** If
  price opens below your stop you are filled at the open; the stop price was
  never available. Options gap hard at 09:15, so pretending otherwise
  understates exactly the losses that hurt most.
"""

from __future__ import annotations

from dataclasses import dataclass

STOP = "STOP"
TARGET = "TARGET"


@dataclass(frozen=True)
class BarExit:
    """The outcome of testing one bar against a stop and a target."""

    hit: bool
    reason: str | None = None       # STOP / TARGET / None
    price: float | None = None
    conflict: bool = False          # both levels touched inside this bar


def resolve_bar_exit(
    bar,
    *,
    stop: float,
    target: float,
    direction: str = "LONG",
) -> BarExit:
    """Decide whether ``bar`` exits a position, and at what price.

    ``bar`` is any mapping with ``Open`` / ``High`` / ``Low``. ``direction`` is
    LONG (the default; long options and long equity) or SHORT.
    """
    high = float(bar["High"])
    low = float(bar["Low"])
    bar_open = float(bar["Open"])
    long_side = str(direction).upper() in ("LONG", "BUY", "CALL", "PUT")

    # A long option position is long its own premium regardless of whether the
    # option is a CALL or a PUT, so CALL/PUT both map to the long side here.
    hit_stop = low <= stop if long_side else high >= stop
    hit_target = high >= target if long_side else low <= target

    if hit_stop and hit_target:
        price = min(stop, bar_open) if long_side else max(stop, bar_open)
        return BarExit(True, STOP, price, conflict=True)
    if hit_stop:
        price = min(stop, bar_open) if long_side else max(stop, bar_open)
        return BarExit(True, STOP, price)
    if hit_target:
        price = max(target, bar_open) if long_side else min(target, bar_open)
        return BarExit(True, TARGET, price)
    return BarExit(False)
