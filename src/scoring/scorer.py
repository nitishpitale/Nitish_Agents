"""
Core scoring engine for the Volatility Mispricing Agent.

Phase 1: Compute IV, Greeks, realized vol forecast, edge, and score.
Phase 2: Optionally apply directional overlay (momentum, earnings).

All computations are DETERMINISTIC — no LLM involvement.

Scoring formula (exact):
    slippage = slippage_factor * mid
    denom = bid_ask_spread_pct + abs(theta) + slippage
    score = (edge_price * vega) / max(denom, 1e-6)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from ..config.settings import FilterConfig as SettingsFilterConfig
from ..config.settings import Phase2Config, ScoringConfig
from ..ingest.models import HistoricalPrices, OptionContract
from ..modeling.black_scholes import bs_greeks, bs_price, implied_volatility
from ..modeling.realized_vol import blend_sigma_hat
from .filters import FilterConfig, apply_filters

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result data class
# ---------------------------------------------------------------------------


@dataclass
class CandidateResult:
    """
    Full scored option candidate.  Matches the required output JSON schema.
    """
    id: str
    run_date: datetime
    ticker: str
    contract: str
    expiry: date
    strike: float
    type: str          # "C" or "P"
    market_mid: float
    bid: float
    ask: float
    iv: float
    delta: float
    vega: float
    theta: float       # per year
    sigma_hat_T: float
    edge_vol: float
    model_fair_value: float
    edge_price: float
    bid_ask_spread_pct: float
    oi: int
    volume: int
    score: float
    rank: int = 0
    # Phase 2 fields
    momentum_percentile: Optional[float] = None
    phase2_score: Optional[float] = None
    # News enrichment fields (populated after Phase 1, before final ranking)
    news_article_count: int = 0
    news_top_headlines: List[str] = field(default_factory=list)
    news_features: Optional[Dict] = None  # serialised NewsFeatures dict
    news_score_multiplier: float = 1.0
    news_adjusted_score: Optional[float] = None
    # LLM fields (populated last)
    reason_summary: str = ""
    risks: List[str] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialise to the required output JSON schema."""
        d = {
            "id": self.id,
            "run_date": self.run_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ticker": self.ticker,
            "contract": self.contract,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "type": self.type,
            "market_mid": round(self.market_mid, 4),
            "bid": round(self.bid, 4),
            "ask": round(self.ask, 4),
            "iv": round(self.iv, 6),
            "delta": round(self.delta, 6),
            "vega": round(self.vega, 6),
            "theta": round(self.theta, 6),
            "sigma_hat_T": round(self.sigma_hat_T, 6),
            "edge_vol": round(self.edge_vol, 6),
            "model_fair_value": round(self.model_fair_value, 4),
            "edge_price": round(self.edge_price, 4),
            "bid_ask_spread_pct": round(self.bid_ask_spread_pct, 6),
            "oi": self.oi,
            "volume": self.volume,
            "score": round(self.score, 6),
            "rank": self.rank,
            "reason_summary": self.reason_summary,
            "risks": self.risks,
            "metadata": self.metadata,
            # News context block
            "news": {
                "article_count": self.news_article_count,
                "top_headlines": self.news_top_headlines,
                "features": self.news_features,
                "score_multiplier": round(self.news_score_multiplier, 4),
            },
        }
        if self.momentum_percentile is not None:
            d["metadata"]["momentum_percentile"] = round(self.momentum_percentile, 4)
        if self.phase2_score is not None:
            d["metadata"]["phase1_score"] = round(self.score, 6)
            d["metadata"]["phase2_score"] = round(self.phase2_score, 6)
        if self.news_adjusted_score is not None:
            d["score"] = round(self.news_adjusted_score, 6)
            d["metadata"]["pre_news_score"] = round(
                self.phase2_score if self.phase2_score is not None else self.score, 6
            )
            d["metadata"]["news_adjusted_score"] = round(self.news_adjusted_score, 6)
        elif self.phase2_score is not None:
            d["score"] = round(self.phase2_score, 6)
        return d


# ---------------------------------------------------------------------------
# Scoring formula (exact implementation from spec)
# ---------------------------------------------------------------------------


def compute_score(
    edge_price: float,
    vega: float,
    bid_ask_spread_pct: float,
    theta: float,
    mid: float,
    slippage_factor: float = 0.001,
) -> float:
    """
    Deterministic scoring formula.

    score = (edge_price * vega) / (bid_ask_spread_pct + |theta| + slippage)

    where slippage = slippage_factor * mid.

    Args:
        edge_price: model_fair_value - market_mid
        vega: option vega (BSM)
        bid_ask_spread_pct: (ask-bid)/mid
        theta: option theta per year (typically negative)
        mid: market mid price
        slippage_factor: configurable constant (default 0.001)

    Returns:
        Score as a float (higher is better).
    """
    slippage = slippage_factor * mid
    denom = bid_ask_spread_pct + abs(theta) + slippage
    if denom <= 0:
        denom = 1e-6
    return (edge_price * vega) / denom


# ---------------------------------------------------------------------------
# Phase 2 directional overlay
# ---------------------------------------------------------------------------


def apply_phase2_overlay(
    candidate: CandidateResult,
    momentum_percentile: Optional[float],
    days_to_earnings: Optional[int],
    sentiment_score: Optional[float],
    cfg: Phase2Config,
    as_of: date,
) -> float:
    """
    Combine base score with directional signals.

    Returns the final Phase 2 score (higher is better).
    """
    w = cfg.weights
    base = candidate.score

    # Momentum bonus: high percentile = bullish momentum → boosts calls, penalises puts
    momentum_adj = 0.0
    if momentum_percentile is not None:
        # Centre around 0.5: range -0.5 to +0.5
        centred = momentum_percentile - 0.5
        if candidate.type == "C":
            momentum_adj = centred * w.momentum
        else:
            momentum_adj = -centred * w.momentum

    # Earnings penalty: if earnings within window, penalise for event risk
    earnings_adj = 0.0
    if days_to_earnings is not None and days_to_earnings <= cfg.earnings_penalty_days:
        # Penalise proportional to how close earnings are
        proximity = 1.0 - (days_to_earnings / cfg.earnings_penalty_days)
        earnings_adj = w.earnings_penalty * proximity

    # Sentiment adjustment (optional)
    sentiment_adj = 0.0
    if cfg.news_sentiment_enabled and sentiment_score is not None:
        # sentiment_score in [-1, +1]
        if candidate.type == "C":
            sentiment_adj = sentiment_score * w.sentiment
        else:
            sentiment_adj = -sentiment_score * w.sentiment

    phase2_score = base * w.base_score + momentum_adj + earnings_adj + sentiment_adj
    return phase2_score


# ---------------------------------------------------------------------------
# Main scoring pipeline
# ---------------------------------------------------------------------------


def score_candidates(
    contracts: List[OptionContract],
    historical_prices: Dict[str, HistoricalPrices],
    run_date: datetime,
    scoring_cfg: ScoringConfig,
    filter_cfg: SettingsFilterConfig,
    realized_vol_cfg,
    risk_free_rate: float = 0.045,
    phase2_cfg: Optional[Phase2Config] = None,
    momentum_data: Optional[Dict[str, float]] = None,
    earnings_data: Optional[Dict[str, Optional[date]]] = None,
) -> List[CandidateResult]:
    """
    Full Phase 1 (+ optional Phase 2) scoring pipeline.

    Steps:
      1. Apply liquidity filters
      2. For each contract: compute IV, Greeks, sigma_hat_T, edge, score
      3. Sort by score descending, assign ranks
      4. Optionally apply Phase 2 directional overlay
      5. Return top N

    Returns:
        Ranked list of CandidateResult objects (up to scoring_cfg.top_n).
    """
    as_of = run_date.date()

    # Build filter config
    flt_cfg = FilterConfig(
        max_bid_ask_spread_pct=filter_cfg.max_bid_ask_spread_pct,
        min_open_interest=filter_cfg.min_open_interest,
        min_days_to_expiry=filter_cfg.min_days_to_expiry,
        max_days_to_expiry=filter_cfg.max_days_to_expiry,
    )

    passed_contracts, filter_stats = apply_filters(contracts, as_of, flt_cfg)
    logger.info("After filtering: %d candidates to score", len(passed_contracts))

    candidates: List[CandidateResult] = []

    for contract in passed_contracts:
        try:
            cand = _score_single_contract(
                contract=contract,
                historical_prices=historical_prices,
                run_date=run_date,
                scoring_cfg=scoring_cfg,
                realized_vol_cfg=realized_vol_cfg,
                risk_free_rate=risk_free_rate,
            )
            if cand is not None:
                candidates.append(cand)
        except Exception as exc:
            logger.warning(
                "Failed to score contract %s: %s", contract.contract_id, exc, exc_info=True
            )

    logger.info("Scored %d candidates", len(candidates))

    # Sort by base score descending
    candidates.sort(key=lambda c: c.score, reverse=True)

    # Phase 2 overlay
    if phase2_cfg and phase2_cfg.enabled:
        for cand in candidates:
            ticker = cand.ticker
            mom_pct = (momentum_data or {}).get(ticker)
            cand.momentum_percentile = mom_pct

            earnings_date = (earnings_data or {}).get(ticker)
            days_to_earn: Optional[int] = None
            if earnings_date is not None:
                days_to_earn = (earnings_date - as_of).days
                if days_to_earn < 0:
                    days_to_earn = None  # past earnings — ignore

            p2_score = apply_phase2_overlay(
                candidate=cand,
                momentum_percentile=mom_pct,
                days_to_earnings=days_to_earn,
                sentiment_score=None,
                cfg=phase2_cfg,
                as_of=as_of,
            )
            cand.phase2_score = p2_score

            # Annotate risks
            if days_to_earn is not None and days_to_earn <= phase2_cfg.earnings_penalty_days:
                cand.risks.append(f"earnings in {days_to_earn} days")

        # Re-sort by Phase 2 score
        candidates.sort(key=lambda c: c.phase2_score or c.score, reverse=True)

    # Assign ranks and return top N
    top_n = candidates[: scoring_cfg.top_n]
    for rank, cand in enumerate(top_n, start=1):
        cand.rank = rank

    logger.info("Returning top %d candidates", len(top_n))
    return top_n


def _score_single_contract(
    contract: OptionContract,
    historical_prices: Dict[str, HistoricalPrices],
    run_date: datetime,
    scoring_cfg: ScoringConfig,
    realized_vol_cfg,
    risk_free_rate: float,
) -> Optional[CandidateResult]:
    """Score a single filtered contract. Returns None if scoring fails."""
    as_of = run_date.date()
    ticker = contract.ticker
    spot = contract.spot
    K = contract.strike
    r = risk_free_rate
    q = contract.dividend_yield
    opt_type = contract.option_type

    # Time to expiry in years
    dte = (contract.expiry - as_of).days
    T = dte / 365.0

    # Mid price (already filtered — bid/ask exist)
    mid = contract.mid  # type: ignore[assignment]
    bid = contract.bid  # type: ignore[assignment]
    ask = contract.ask  # type: ignore[assignment]
    spread_pct = contract.bid_ask_spread_pct  # type: ignore[assignment]

    # --- Implied Volatility ---
    iv = implied_volatility(mid, spot, K, T, r, q, opt_type)
    if iv is None or iv <= 0:
        logger.debug("IV solve failed for %s — skipping", contract.contract_id)
        return None

    # --- Greeks ---
    greeks = bs_greeks(spot, K, T, iv, r, q, opt_type)
    # Theta per year → per day for annotation
    theta_annual = greeks.theta

    # --- Realized vol forecast ---
    hist = historical_prices.get(ticker)
    prices_seq = hist.closes if hist is not None else []

    blend_weights = tuple(realized_vol_cfg.blend_weights)
    sigma_hat = blend_sigma_hat(
        prices=prices_seq,
        T_years=T,
        window_20d=realized_vol_cfg.window_20d,
        window_60d=realized_vol_cfg.window_60d,
        ewma_lambda=realized_vol_cfg.ewma_lambda,
        weights=blend_weights,  # type: ignore[arg-type]
    )

    if sigma_hat is None:
        logger.debug("Insufficient price history for %s — skipping", ticker)
        return None

    # --- Edge ---
    edge_vol = sigma_hat - iv
    model_fair_value = bs_price(spot, K, T, sigma_hat, r, q, opt_type)
    edge_price = model_fair_value - mid

    # --- Score ---
    score = compute_score(
        edge_price=edge_price,
        vega=greeks.vega,
        bid_ask_spread_pct=spread_pct,
        theta=theta_annual,
        mid=mid,
        slippage_factor=scoring_cfg.slippage_factor,
    )

    return CandidateResult(
        id=str(uuid.uuid4()),
        run_date=run_date,
        ticker=ticker,
        contract=contract.contract_id,
        expiry=contract.expiry,
        strike=K,
        type=opt_type,
        market_mid=round(mid, 4),
        bid=round(bid, 4),
        ask=round(ask, 4),
        iv=round(iv, 6),
        delta=round(greeks.delta, 6),
        vega=round(greeks.vega, 6),
        theta=round(theta_annual, 6),
        sigma_hat_T=round(sigma_hat, 6),
        edge_vol=round(edge_vol, 6),
        model_fair_value=round(model_fair_value, 4),
        edge_price=round(edge_price, 4),
        bid_ask_spread_pct=round(spread_pct, 6),
        oi=contract.open_interest,
        volume=contract.volume,
        score=round(score, 6),
        metadata={
            "days_to_expiry": dte,
            "T_years": round(T, 6),
        },
    )
