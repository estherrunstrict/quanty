"""
Bubble-collapse exit overlay — Kim Hyo-jin (신영증권) framework.

Source: Dr. Kim Hyo-jin interview on the three "scientific" signals that
precede a bubble unwind. Encoded here as pure, deterministic risk primitives
that overlay onto a long signal (e.g. quant_rv / managed-vol) to dial down
exposure *before* the broad market rolls over.

The three signals (each maps to one function below):

  A. Breadth divergence  (강제적 자금 쏠림)
     In the final ~3 months no fresh money enters, so investors sell their
     other holdings (Dow, S&P 500) to chase the single leader (Nasdaq).
     Same-country index returns diverge violently: the leader keeps rising
     while everything else turns negative.

  B. Global negative count  (계단식 하락)
     Markets furthest from the leadership theme roll over first, and the
     count of countries in the red rises in *steps* — 3, then 5, then more.

  C. Rate threshold  (표면장력을 깨는 마지막 한 방울)
     Early rate hikes can coexist with rising stocks. But once the 10y yield
     pushes past the prior cycle high, the burden on the real economy and on
     multiples crosses a threshold and the bubble breaks. This caps how much
     a vol-based long signal can be trusted.

All functions are pure (no I/O), take plain floats / iterables, and return
deterministic results — same contract as `framework.portfolio.risk`. Data
fetching lives in `research/market_exit_monitor.py`, never here.

Conventions:
  - Returns: simple, decimal (0.01 = 1%)
  - Scores: 0.0 (calm) .. 1.0 (max de-risk pressure)
  - Factors / budgets: multipliers in [0.0, 1.0] applied to a long weight
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def cumulative_return(returns: Iterable[float], window: int | None = None) -> float:
    """Compound the trailing `window` daily simple returns into one figure.

    cumulative_return([0.01, 0.01]) -> 0.0201. Empty input -> 0.0.
    `window=None` uses every sample provided.
    """
    r = list(returns)
    if window is not None:
        r = r[-window:]
    if not r:
        return 0.0
    growth = 1.0
    for x in r:
        growth *= 1.0 + x
    return growth - 1.0


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _ramp(value: float, start: float, full: float) -> float:
    """0.0 at/below `start`, 1.0 at/above `full`, linear in between.

    Works in either direction: if full < start the ramp counts *down*
    (value below `full` -> 1.0)."""
    if start == full:
        return 1.0 if value >= start else 0.0
    t = (value - start) / (full - start)
    return _clamp(t)


# --------------------------------------------------------------------------- #
# Signal A — breadth divergence (강제적 자금 쏠림)
# --------------------------------------------------------------------------- #
@dataclass
class BreadthResult:
    score: float                 # 0..1 de-risk pressure
    leader_mom: float            # cumulative leader return over momentum_window
    broad_mom: float
    oldecon_mom: float
    spread: float                # leader_mom - broad_mom
    divergence_days: int         # days in persistence_window: broad<0 & leader>0
    concentration: bool          # leader up while broad/old-economy down
    reason: str = ""


def breadth_divergence(
    leader_returns: Iterable[float],
    broad_returns: Iterable[float],
    oldecon_returns: Iterable[float],
    momentum_window: int = 63,
    persistence_window: int = 20,
    spread_threshold: float = 0.03,
    spread_full: float = 0.10,
    persistence_threshold: int = 8,
) -> BreadthResult:
    """Signal A: leader (QQQ) pulling away while the broad market (SPY) and
    old-economy index (DIA) stall or fall = forced money concentration.

    Args:
        leader_returns:  daily returns of the leadership index (e.g. QQQ).
        broad_returns:   daily returns of the broad index (e.g. SPY).
        oldecon_returns: daily returns of the old-economy index (e.g. DIA).
                         (most-recent last for all three)
        momentum_window: lookback for cumulative momentum comparison.
        persistence_window: lookback for counting daily divergence days.
        spread_threshold: leader-minus-broad momentum gap where pressure starts.
        spread_full: gap at which the spread component saturates to 1.0.
        persistence_threshold: divergence-day count where that component hits 1.0.

    Returns:
        BreadthResult. `score` rises with both the momentum spread and the
        number of recent days the leader rose while the broad market fell.
    """
    lead = list(leader_returns)
    broad = list(broad_returns)
    old = list(oldecon_returns)

    leader_mom = cumulative_return(lead, momentum_window)
    broad_mom = cumulative_return(broad, momentum_window)
    oldecon_mom = cumulative_return(old, momentum_window)
    spread = leader_mom - broad_mom

    # Daily divergence persistence: leader up, broad down on the same day.
    n = min(len(lead), len(broad), persistence_window)
    divergence_days = 0
    if n > 0:
        for li, bi in zip(lead[-n:], broad[-n:]):
            if li > 0.0 and bi < 0.0:
                divergence_days += 1

    concentration = leader_mom > 0.0 and min(broad_mom, oldecon_mom) < 0.0

    # Component scores.
    spread_score = _ramp(spread, spread_threshold, spread_full)
    persist_score = _clamp(divergence_days / persistence_threshold) if persistence_threshold > 0 else 0.0

    # Spread and persistence each evidence the same pathology; take the
    # stronger, then require the concentration condition to "arm" it fully —
    # a wide spread in a broad rally (everything up) is not the warning.
    raw = max(spread_score, persist_score)
    score = raw if concentration else raw * 0.4

    reason = (
        f"leader {leader_mom:+.1%} vs broad {broad_mom:+.1%} (spread {spread:+.1%}), "
        f"oldecon {oldecon_mom:+.1%}, divergence {divergence_days}/{persistence_window}d"
        + (", forced-concentration" if concentration else "")
    )
    return BreadthResult(
        score=_clamp(score),
        leader_mom=leader_mom,
        broad_mom=broad_mom,
        oldecon_mom=oldecon_mom,
        spread=spread,
        divergence_days=divergence_days,
        concentration=concentration,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Signal B — global negative count (계단식 하락)
# --------------------------------------------------------------------------- #
@dataclass
class GlobalBreadthResult:
    score: float                 # 0..1 de-risk pressure
    negative_count: int          # countries with return <= threshold
    total: int
    negative_countries: list[str]
    stepping_up: bool            # count escalated vs prior reading(s)
    reason: str = ""


def global_negative_count(
    country_returns: Mapping[str, float],
    threshold: float = 0.0,
) -> tuple[int, list[str]]:
    """Count markets whose trailing (e.g. 6-month) return is at/below
    `threshold`. Returns (count, sorted list of country keys)."""
    negs = sorted(k for k, v in country_returns.items() if v <= threshold)
    return len(negs), negs


def global_breadth_signal(
    country_returns: Mapping[str, float],
    prior_counts: Iterable[int] = (),
    threshold: float = 0.0,
    warn_count: int = 3,
    danger_count: int = 6,
) -> GlobalBreadthResult:
    """Signal B: the count of red countries climbing in steps is an early
    warning even while a vol-based long signal still says long.

    Args:
        country_returns: {country/etf: trailing return} (e.g. 6-month).
        prior_counts: recent history of this count, oldest..newest, used to
                      detect the stair-step escalation. Empty = no history.
        threshold: a country is "negative" at/below this return.
        warn_count: count where pressure begins to ramp.
        danger_count: count where pressure saturates to 1.0.

    Returns:
        GlobalBreadthResult. `stepping_up` is True when the latest count is
        strictly above the max of the prior readings (the "계단식" climb).
    """
    count, negs = global_negative_count(country_returns, threshold)
    score = _ramp(float(count), float(warn_count), float(danger_count))

    history = list(prior_counts)
    stepping_up = bool(history) and count > max(history)
    if stepping_up:
        # An active step-up is the regime change Kim describes — nudge the
        # score up so a fresh escalation registers before the absolute count
        # reaches the danger band.
        score = _clamp(max(score, _ramp(float(count), float(warn_count - 1), float(danger_count))))

    reason = (
        f"{count}/{len(country_returns)} markets negative"
        + (f" (stepping up from {history[-1]})" if stepping_up and history else "")
        + (f": {', '.join(negs)}" if negs else "")
    )
    return GlobalBreadthResult(
        score=_clamp(score),
        negative_count=count,
        total=len(country_returns),
        negative_countries=negs,
        stepping_up=stepping_up,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Signal C — rate threshold (표면장력을 깨는 마지막 한 방울)
# --------------------------------------------------------------------------- #
@dataclass
class RateResult:
    factor: float                # 0..1 multiplier on the long signal
    current_yield: float
    prior_cycle_high: float
    gap_bp: float                # basis points below prior high (neg = breached)
    breached: bool
    reason: str = ""


def rate_overlay_factor(
    current_yield: float,
    prior_cycle_high: float,
    approach_band_bp: float = 50.0,
    floor: float = 0.2,
) -> RateResult:
    """Signal C: discount the validity of a vol-based long signal as the 10y
    yield approaches and then breaches the prior cycle high.

    Yields are in percent (4.5 = 4.50%). `approach_band_bp` is how far below
    the prior high (in basis points) the discount starts.

    Returns a multiplier:
        1.0      when comfortably below the prior high,
        ramps to `floor` as yield closes the last `approach_band_bp`,
        `floor`  once the prior high is breached (the "last drop").
    """
    band = approach_band_bp / 100.0  # bp -> percentage points
    gap = prior_cycle_high - current_yield        # +ve = still below
    gap_bp = gap * 100.0
    breached = current_yield >= prior_cycle_high

    if breached:
        factor = floor
    else:
        # full factor at gap >= band, ramping to floor at gap == 0.
        approach = _clamp(gap / band) if band > 0 else 1.0
        factor = floor + (1.0 - floor) * approach

    reason = (
        f"10y {current_yield:.2f}% vs prior high {prior_cycle_high:.2f}% "
        f"({gap_bp:+.0f}bp)" + (" — BREACHED" if breached else "")
    )
    return RateResult(
        factor=_clamp(factor),
        current_yield=current_yield,
        prior_cycle_high=prior_cycle_high,
        gap_bp=gap_bp,
        breached=breached,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Composite overlay
# --------------------------------------------------------------------------- #
@dataclass
class ExitOverlayResult:
    risk_budget: float           # 0..1 multiplier for the long weight
    exit_triggered: bool         # risk_budget <= exit_threshold
    de_risk: float               # blended breadth+global pressure (0..1)
    breadth: BreadthResult
    global_breadth: GlobalBreadthResult
    rate: RateResult
    warnings: list[str] = field(default_factory=list)

    def gate(self, raw_long_weight: float) -> float:
        """Apply the overlay to a strategy's raw long weight."""
        return gate_long_weight(raw_long_weight, self)


def composite_exit_overlay(
    breadth: BreadthResult,
    global_breadth: GlobalBreadthResult,
    rate: RateResult,
    breadth_weight: float = 0.6,
    global_weight: float = 0.4,
    exit_threshold: float = 0.35,
) -> ExitOverlayResult:
    """Blend the three signals into one risk budget.

    Breadth (A) and global count (B) are *additive* de-risk pressure — each
    eats into the budget. Rate (C) is a *multiplicative gate*: once yields
    breach the prior cycle high it caps the whole budget regardless of the
    other two, matching Kim's "last drop that breaks the surface tension".

        de_risk     = wB*breadth.score + wG*global.score      (0..1)
        risk_budget = (1 - de_risk) * rate.factor              (0..1)
        exit        = risk_budget <= exit_threshold

    Returns:
        ExitOverlayResult with the budget, the exit flag, and human-readable
        warnings for the signals that are firing.
    """
    de_risk = _clamp(breadth_weight * breadth.score + global_weight * global_breadth.score)
    risk_budget = _clamp((1.0 - de_risk) * rate.factor)
    exit_triggered = risk_budget <= exit_threshold

    warnings: list[str] = []
    if breadth.score >= 0.5:
        warnings.append(f"A/breadth-divergence: {breadth.reason}")
    if global_breadth.score >= 0.5 or global_breadth.stepping_up:
        warnings.append(f"B/global-negative: {global_breadth.reason}")
    if rate.factor < 1.0:
        warnings.append(f"C/rate-threshold: {rate.reason}")

    return ExitOverlayResult(
        risk_budget=risk_budget,
        exit_triggered=exit_triggered,
        de_risk=de_risk,
        breadth=breadth,
        global_breadth=global_breadth,
        rate=rate,
        warnings=warnings,
    )


def gate_long_weight(raw_long_weight: float, result: ExitOverlayResult) -> float:
    """Scale a strategy's raw long weight by the overlay's risk budget.

    A vol-based long signal that wants weight 1.0 gets `risk_budget`; a flat
    (<=0) signal is left untouched. Use inside `get_target_allocation()` after
    the strategy has produced its raw vol-targeted weight."""
    if raw_long_weight <= 0.0:
        return raw_long_weight
    return raw_long_weight * result.risk_budget


# --------------------------------------------------------------------------- #
# Object API — bundles thresholds, mirrors RegimeDetector ergonomics
# --------------------------------------------------------------------------- #
class MarketExitOverlay:
    """Stateful-config wrapper around the three pure signals.

    Hold one instance per market, configure thresholds once, then call
    `evaluate(...)` each bar with freshly-fetched data. The class itself
    performs no I/O — feed it returns/levels from your data layer.
    """

    def __init__(
        self,
        momentum_window: int = 63,
        persistence_window: int = 20,
        spread_threshold: float = 0.03,
        persistence_threshold: int = 8,
        neg_threshold: float = 0.0,
        warn_count: int = 3,
        danger_count: int = 6,
        approach_band_bp: float = 50.0,
        rate_floor: float = 0.2,
        breadth_weight: float = 0.6,
        global_weight: float = 0.4,
        exit_threshold: float = 0.35,
    ):
        self.momentum_window = momentum_window
        self.persistence_window = persistence_window
        self.spread_threshold = spread_threshold
        self.persistence_threshold = persistence_threshold
        self.neg_threshold = neg_threshold
        self.warn_count = warn_count
        self.danger_count = danger_count
        self.approach_band_bp = approach_band_bp
        self.rate_floor = rate_floor
        self.breadth_weight = breadth_weight
        self.global_weight = global_weight
        self.exit_threshold = exit_threshold

    def evaluate(
        self,
        leader_returns: Iterable[float],
        broad_returns: Iterable[float],
        oldecon_returns: Iterable[float],
        country_returns: Mapping[str, float],
        current_yield: float,
        prior_cycle_high: float,
        prior_negative_counts: Iterable[int] = (),
    ) -> ExitOverlayResult:
        breadth = breadth_divergence(
            leader_returns, broad_returns, oldecon_returns,
            momentum_window=self.momentum_window,
            persistence_window=self.persistence_window,
            spread_threshold=self.spread_threshold,
            persistence_threshold=self.persistence_threshold,
        )
        gb = global_breadth_signal(
            country_returns,
            prior_counts=prior_negative_counts,
            threshold=self.neg_threshold,
            warn_count=self.warn_count,
            danger_count=self.danger_count,
        )
        rate = rate_overlay_factor(
            current_yield, prior_cycle_high,
            approach_band_bp=self.approach_band_bp,
            floor=self.rate_floor,
        )
        return composite_exit_overlay(
            breadth, gb, rate,
            breadth_weight=self.breadth_weight,
            global_weight=self.global_weight,
            exit_threshold=self.exit_threshold,
        )
