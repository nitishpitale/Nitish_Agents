"""
Main orchestration engine.

Pipeline:
  ingest → Phase 1 scoring → [news enrichment] → Phase 2 overlay → LLM rationale

This module is the single entry point for a run.  The API and scheduler both call
`run_engine(...)`.  All inputs are explicit — no global state.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

import structlog

from .config.settings import Settings, get_settings
from .ingest.etoro_client import EToroClient
from .ingest.mock_client import MockEToroClient
from .ingest.models import HistoricalPrices, OptionContract
from .llm.rationale import LLMProvider, generate_daily_summary, generate_rationale
from .news import (
    NewsCache,
    NewsFeatures,
    NewsRunStats,
    adjustment_summary,
    compute_news_score_adjustment,
    extract_news_features,
    get_provider,
)
from .news.schemas import NormalizedNewsItem
from .scoring.scorer import CandidateResult, score_candidates

log = structlog.get_logger(__name__)

# Type alias for either client
AnyClient = Union[EToroClient, MockEToroClient]


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------


class RunResult:
    def __init__(
        self,
        run_id: str,
        run_date: datetime,
        candidates: List[CandidateResult],
        daily_summary: str,
        stats: dict,
    ) -> None:
        self.run_id = run_id
        self.run_date = run_date
        self.candidates = candidates
        self.daily_summary = daily_summary
        self.stats = stats

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "run_date": self.run_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stats": self.stats,
            "daily_summary": self.daily_summary,
            "candidates": [c.to_dict() for c in self.candidates],
        }


# ---------------------------------------------------------------------------
# Idempotency / deduplication
# ---------------------------------------------------------------------------


def _compute_run_hash(tickers: List[str], as_of: date) -> str:
    """Hash identifying (sorted-tickers, date) — used for deduplication."""
    key = json.dumps({"tickers": sorted(tickers), "date": as_of.isoformat()})
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _load_run_hashes(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_run_hash(path: str, run_hash: str, run_id: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    hashes = _load_run_hashes(path)
    hashes[run_hash] = run_id
    with open(p, "w") as f:
        json.dump(hashes, f, indent=2)


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------


def _persist_results(results_dir: str, result: RunResult) -> Path:
    d = Path(results_dir)
    d.mkdir(parents=True, exist_ok=True)
    date_str = result.run_date.strftime("%Y-%m-%d")
    path = d / f"run_{date_str}_{result.run_id}.json"
    with open(path, "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    return path


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------


def run_engine(
    settings: Optional[Settings] = None,
    tickers: Optional[List[str]] = None,
    run_date: Optional[datetime] = None,
    force_rerun: bool = False,
    client: Optional[AnyClient] = None,
) -> RunResult:
    """
    Execute one full engine run.

    Args:
        settings: Configuration object (uses global default if None).
        tickers: Override the universe of tickers to scan.
        run_date: Override the run timestamp (UTC). Defaults to now.
        force_rerun: If True, skip idempotency check and always run.
        client: Inject a custom client (useful for testing).

    Returns:
        RunResult with candidates and metadata.
    """
    t_start = time.monotonic()

    settings = settings or get_settings()
    tickers = tickers or settings.universe.tickers
    run_date = run_date or datetime.now(timezone.utc)
    as_of = run_date.date()

    run_hash = _compute_run_hash(tickers, as_of)
    run_id = run_hash

    log.info(
        "engine.start",
        run_id=run_id,
        run_date=run_date.isoformat(),
        tickers=tickers,
        use_mock=settings.etoro.use_mock,
    )

    # --- Idempotency check ---
    if not force_rerun:
        hashes = _load_run_hashes(settings.persistence.run_hashes_file)
        if run_hash in hashes:
            log.info("engine.duplicate_run_skipped", run_hash=run_hash)
            # Attempt to load and return prior result
            prior_path = Path(settings.persistence.results_dir) / f"run_{as_of}_{run_hash}.json"
            if prior_path.exists():
                with open(prior_path) as f:
                    prior = json.load(f)
                # Return a minimal RunResult for the API layer
                return RunResult(
                    run_id=run_hash,
                    run_date=run_date,
                    candidates=[],
                    daily_summary=prior.get("daily_summary", ""),
                    stats=prior.get("stats", {"deduplicated": True}),
                )

    # --- Build or use injected client ---
    if client is None:
        if settings.etoro.use_mock:
            client = MockEToroClient(reference_date=as_of)
        elif not settings.etoro.credentials_complete:
            # Missing x-user-key or x-api-key — fall back to mock with a clear warning
            missing = []
            if not settings.etoro.api_key:
                missing.append("ETORO_API_KEY")
            if not settings.etoro.user_key:
                missing.append("ETORO_USER_KEY")
            log.warning(
                "engine.credentials_incomplete",
                missing=missing,
                action="falling back to MockEToroClient",
                hint=(
                    "The eToro API requires both x-api-key (ETORO_API_KEY) and "
                    "x-user-key (ETORO_USER_KEY). "
                    "Generate your User Key at: "
                    "https://api-portal.etoro.com/getting-started/authentication"
                ),
            )
            client = MockEToroClient(reference_date=as_of)
        else:
            client = EToroClient(settings.etoro)

    # --- Ingest ---
    all_contracts: List[OptionContract] = []
    historical_prices: Dict[str, HistoricalPrices] = {}
    ingest_errors: List[str] = []

    for ticker in tickers:
        try:
            # Option chain
            contracts = client.get_option_chain(ticker)
            all_contracts.extend(contracts)
            log.debug("ingest.option_chain", ticker=ticker, count=len(contracts))

            # Historical prices for realized vol
            hist = client.get_historical_prices(ticker)
            if hist is not None:
                historical_prices[ticker] = hist
        except Exception as exc:
            log.error("ingest.error", ticker=ticker, error=str(exc))
            ingest_errors.append(f"{ticker}: {exc}")

    log.info("ingest.complete", total_contracts=len(all_contracts), tickers_with_history=len(historical_prices))

    # --- Phase 2 data ---
    momentum_data: Dict[str, float] = {}
    earnings_data: Dict[str, Optional[date]] = {}

    if settings.phase2.enabled:
        for ticker in tickers:
            try:
                if hasattr(client, "get_momentum_percentile"):
                    mom = client.get_momentum_percentile(ticker)
                    if mom is not None:
                        momentum_data[ticker] = mom
                if hasattr(client, "get_next_earnings_date"):
                    earn = client.get_next_earnings_date(ticker)
                    earnings_data[ticker] = earn
            except Exception as exc:
                log.warning("phase2.data_error", ticker=ticker, error=str(exc))

    # --- Phase 1 Score ---
    candidates = score_candidates(
        contracts=all_contracts,
        historical_prices=historical_prices,
        run_date=run_date,
        scoring_cfg=settings.scoring,
        filter_cfg=settings.filters,
        realized_vol_cfg=settings.realized_vol,
        risk_free_rate=settings.risk_free_rate,
        phase2_cfg=settings.phase2 if settings.phase2.enabled else None,
        momentum_data=momentum_data if settings.phase2.enabled else None,
        earnings_data=earnings_data if settings.phase2.enabled else None,
    )

    # --- News Pipeline (between Phase 1 and final ranking) ---
    news_stats = NewsRunStats(provider=settings.news.provider)
    if settings.news.enabled and candidates:
        candidates, news_stats = _run_news_pipeline(
            candidates=candidates,
            as_of=as_of,
            settings=settings,
            news_provider_override=None,
        )
        # Re-sort by news_adjusted_score (highest wins)
        candidates.sort(
            key=lambda c: c.news_adjusted_score if c.news_adjusted_score is not None
            else (c.phase2_score if c.phase2_score is not None else c.score),
            reverse=True,
        )
        for rank, cand in enumerate(candidates, start=1):
            cand.rank = rank

    # --- LLM rationale (news-aware) ---
    llm_provider = LLMProvider(settings.llm.provider)
    llm_api_key = settings.llm.api_key or os.getenv("OPENAI_API_KEY", "")

    for candidate in candidates:
        try:
            candidate.reason_summary = generate_rationale(
                candidate_data=candidate.to_dict(),
                provider=llm_provider,
                model=settings.llm.model,
                api_key=llm_api_key,
                max_tokens=settings.llm.max_tokens,
                temperature=settings.llm.temperature,
            )
        except Exception as exc:
            log.warning("llm.rationale_error", contract=candidate.contract, error=str(exc))
            candidate.reason_summary = "Rationale unavailable."

    # --- Daily summary ---
    daily_summary = ""
    if candidates:
        try:
            daily_summary = generate_daily_summary(
                candidates=[c.to_dict() for c in candidates],
                run_date=as_of.isoformat(),
                provider=llm_provider,
                model=settings.llm.model,
                api_key=llm_api_key,
            )
        except Exception as exc:
            log.warning("llm.daily_summary_error", error=str(exc))

    elapsed = time.monotonic() - t_start

    stats = {
        "run_id": run_id,
        "run_date": run_date.isoformat(),
        "tickers_scanned": len(tickers),
        "total_contracts_ingested": len(all_contracts),
        "candidates_returned": len(candidates),
        "ingest_errors": ingest_errors,
        "runtime_seconds": round(elapsed, 3),
        "phase2_enabled": settings.phase2.enabled,
        "news_enabled": settings.news.enabled,
        "news_provider": news_stats.provider,
        "news_articles_fetched": news_stats.total_articles,
        "news_cache_hit_ratio": round(news_stats.cache_hit_ratio, 3),
        "news_llm_calls": news_stats.llm_calls,
    }

    log.info(
        "engine.complete",
        **{k: v for k, v in stats.items() if not isinstance(v, list)},
    )

    result = RunResult(
        run_id=run_id,
        run_date=run_date,
        candidates=candidates,
        daily_summary=daily_summary,
        stats=stats,
    )

    # --- Persist ---
    try:
        path = _persist_results(settings.persistence.results_dir, result)
        _save_run_hash(settings.persistence.run_hashes_file, run_hash, run_id)
        log.info("engine.persisted", path=str(path))
    except Exception as exc:
        log.error("engine.persist_error", error=str(exc))

    return result


# ---------------------------------------------------------------------------
# News pipeline helper
# ---------------------------------------------------------------------------


def _run_news_pipeline(
    candidates: List[CandidateResult],
    as_of: date,
    settings: Settings,
    news_provider_override=None,
) -> tuple[List[CandidateResult], NewsRunStats]:
    """
    Fetch news, extract features, apply deterministic score multipliers.

    Steps:
      1. Collect unique tickers from top K Phase-1 candidates.
      2. Fetch and cache articles per ticker.
      3. LLM extracts NewsFeatures (structured JSON, no math).
      4. Deterministic code maps features → score multiplier.
      5. Apply multiplier: news_adjusted_score = pre_news_score * multiplier.

    Returns updated candidates list and NewsRunStats.
    """
    news_cfg = settings.news
    stats = NewsRunStats(provider=news_cfg.provider)
    api_calls_used = 0

    # Build (or reuse injected) provider
    if news_provider_override is not None:
        provider = news_provider_override
    else:
        try:
            provider_kwargs: dict = {}
            if news_cfg.provider == "fmp":
                provider_kwargs["api_key"] = os.getenv("FMP_API_KEY", "")
            elif news_cfg.provider == "finnhub":
                provider_kwargs["api_key"] = os.getenv("FINNHUB_API_KEY", "")
            elif news_cfg.provider == "financial_datasets":
                provider_kwargs["api_key"] = os.getenv("FINANCIAL_DATASETS_API_KEY", "")
            provider = get_provider(news_cfg.provider, **provider_kwargs)
        except Exception as exc:
            log.error("news.provider_init_error", error=str(exc))
            return candidates, stats

    cache = NewsCache(cache_dir=news_cfg.cache_dir)

    # Collect unique tickers from top K candidates
    top_k_candidates = candidates[: news_cfg.top_k_for_news]
    unique_tickers = list(dict.fromkeys(c.ticker for c in top_k_candidates))

    # Fetch and extract features per ticker
    ticker_articles: Dict[str, List[NormalizedNewsItem]] = {}
    ticker_features: Dict[str, NewsFeatures] = {}

    for ticker in unique_tickers:
        if api_calls_used >= news_cfg.max_api_calls_per_run:
            log.warning("news.rate_limit_reached", ticker=ticker)
            stats.errors.append(f"rate limit reached before {ticker}")
            break

        # Check cache first
        cached = cache.get(ticker, as_of, news_cfg.lookback_hours)
        if cached is not None:
            articles = cached
            stats.cache_hits += 1
        else:
            try:
                articles = provider.fetch_news(
                    ticker=ticker,
                    as_of=as_of,
                    lookback_hours=news_cfg.lookback_hours,
                    max_articles=news_cfg.max_articles_per_ticker,
                )
                cache.set(ticker, as_of, news_cfg.lookback_hours, articles)
                stats.cache_misses += 1
                api_calls_used += 1
            except Exception as exc:
                log.error("news.fetch_error", ticker=ticker, error=str(exc))
                stats.errors.append(f"fetch {ticker}: {exc}")
                articles = []
                stats.cache_misses += 1

        ticker_articles[ticker] = articles
        stats.total_articles += len(articles)
        stats.tickers_fetched += 1

        # Extract features via LLM (or mock)
        try:
            llm_api_key = os.getenv("OPENAI_API_KEY", settings.llm.api_key or "")
            features = extract_news_features(
                ticker=ticker,
                articles=articles,
                ref_date=as_of,
                provider=news_cfg.llm_provider,
                model=news_cfg.llm_model,
                api_key=llm_api_key,
                max_tokens=news_cfg.llm_max_tokens,
                temperature=news_cfg.llm_temperature,
            )
            ticker_features[ticker] = features
            if news_cfg.llm_provider == "openai" and llm_api_key:
                stats.llm_calls += 1
        except Exception as exc:
            log.error("news.features_error", ticker=ticker, error=str(exc))
            stats.errors.append(f"features {ticker}: {exc}")

    # Apply news multipliers to candidates
    for candidate in candidates:
        ticker = candidate.ticker
        articles = ticker_articles.get(ticker, [])
        features = ticker_features.get(ticker)

        candidate.news_article_count = len(articles)
        candidate.news_top_headlines = [a.title for a in articles[:3]]

        if features is not None:
            candidate.news_features = features.model_dump(mode="json")

            multiplier = compute_news_score_adjustment(
                features=features,
                option_type=candidate.type,
                alpha_sentiment=news_cfg.alpha_sentiment,
                alpha_catalyst=news_cfg.alpha_catalyst,
                beta_risk=news_cfg.beta_risk,
                event_risk_mode=news_cfg.event_risk_mode,
            )
            candidate.news_score_multiplier = round(multiplier, 4)

            # Compute final score: apply multiplier to phase2 (or phase1) score
            pre_score = candidate.phase2_score if candidate.phase2_score is not None else candidate.score
            candidate.news_adjusted_score = round(pre_score * multiplier, 6)

            # Add adjustment metadata
            candidate.metadata.update(
                adjustment_summary(features, multiplier, candidate.type)
            )

            # Propagate earnings risk into the risks list (avoid duplication)
            if features.has_earnings_risk():
                earn_risk = "earnings risk flagged in news"
                if earn_risk not in candidate.risks:
                    candidate.risks.append(earn_risk)

    log.info(
        "news.pipeline_complete",
        tickers=len(unique_tickers),
        total_articles=stats.total_articles,
        cache_hit_ratio=round(stats.cache_hit_ratio, 3),
        llm_calls=stats.llm_calls,
        errors=len(stats.errors),
    )

    return candidates, stats
