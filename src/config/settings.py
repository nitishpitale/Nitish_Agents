"""
Application settings loaded from config.yaml with environment variable overrides.
All values are immutable once loaded to guarantee reproducibility within a run.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List

import yaml
from pydantic import BaseModel


_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"


def _load_yaml() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


class EToroConfig(BaseModel):
    base_url: str = "https://public-api.etoro.com"
    api_key: str = ""    # x-api-key  — public API key, identifies the application
    user_key: str = ""   # x-user-key — user key, identifies the account
    timeout_seconds: int = 30
    use_mock: bool = True

    @property
    def credentials_complete(self) -> bool:
        """Both api_key and user_key must be set for live API calls."""
        return bool(self.api_key) and bool(self.user_key)


class UniverseConfig(BaseModel):
    tickers: List[str] = ["AAPL", "MSFT", "TSLA", "SPY", "QQQ"]


class RealizedVolConfig(BaseModel):
    window_20d: int = 20
    window_60d: int = 60
    ewma_lambda: float = 0.94
    blend_weights: List[float] = [0.25, 0.25, 0.50]


class FilterConfig(BaseModel):
    max_bid_ask_spread_pct: float = 0.10
    min_open_interest: int = 200
    min_days_to_expiry: int = 3
    max_days_to_expiry: int = 180


class ScoringConfig(BaseModel):
    top_n: int = 20
    slippage_factor: float = 0.001


class Phase2Weights(BaseModel):
    base_score: float = 0.70
    momentum: float = 0.15
    earnings_penalty: float = -0.10
    sentiment: float = 0.05


class Phase2Config(BaseModel):
    enabled: bool = True
    momentum_window: int = 20
    earnings_penalty_days: int = 14
    news_sentiment_enabled: bool = False
    weights: Phase2Weights = Phase2Weights()


class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    max_tokens: int = 300
    temperature: float = 0.2
    api_key: str = ""


class APIConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1


class SchedulerConfig(BaseModel):
    enabled: bool = True
    cron: str = "30 15 * * 1-5"   # 7:30 AM PST (Mon–Fri)


class PersistenceConfig(BaseModel):
    results_dir: str = "./data/results"
    run_hashes_file: str = "./data/run_hashes.json"


class ReportingConfig(BaseModel):
    top_n: int = 5
    gdrive_doc_id: str = ""
    gdrive_folder_id: str = ""
    local_reports_dir: str = "./data/reports"


class NewsConfig(BaseModel):
    enabled: bool = True
    provider: str = "mock"
    top_k_for_news: int = 50
    lookback_hours: int = 72
    max_articles_per_ticker: int = 20
    cache_dir: str = "./data/news_cache"
    # Score adjustment parameters
    alpha_sentiment: float = 0.15
    alpha_catalyst: float = 0.10
    beta_risk: float = 0.20
    event_risk_mode: str = "avoid"
    # LLM feature extraction
    llm_provider: str = "mock"
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 800
    llm_temperature: float = 0.0
    max_api_calls_per_run: int = 100


class Settings(BaseModel):
    app_name: str = "volatility-mispricing-engine"
    app_version: str = "1.0.0"
    environment: str = "development"
    log_level: str = "INFO"

    risk_free_rate: float = 0.045

    etoro: EToroConfig = EToroConfig()
    universe: UniverseConfig = UniverseConfig()
    realized_vol: RealizedVolConfig = RealizedVolConfig()
    filters: FilterConfig = FilterConfig()
    scoring: ScoringConfig = ScoringConfig()
    phase2: Phase2Config = Phase2Config()
    llm: LLMConfig = LLMConfig()
    api: APIConfig = APIConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    persistence: PersistenceConfig = PersistenceConfig()
    news: NewsConfig = NewsConfig()
    reporting: ReportingConfig = ReportingConfig()

    @classmethod
    def from_yaml_and_env(cls) -> "Settings":
        raw = _load_yaml()

        # Flatten nested yaml sections into constructor kwargs
        etoro_raw = raw.get("etoro", {})
        etoro_raw["api_key"] = os.getenv("ETORO_API_KEY", etoro_raw.get("api_key", ""))
        etoro_raw["user_key"] = os.getenv("ETORO_USER_KEY", etoro_raw.get("user_key", ""))
        etoro_raw["base_url"] = os.getenv("ETORO_BASE_URL", etoro_raw.get("base_url", "https://public-api.etoro.com"))
        use_mock_env = os.getenv("ETORO_USE_MOCK")
        if use_mock_env is not None:
            etoro_raw["use_mock"] = use_mock_env.lower() in ("1", "true", "yes")

        llm_raw = raw.get("llm", {})
        llm_raw["api_key"] = os.getenv("OPENAI_API_KEY", llm_raw.get("api_key", ""))
        llm_provider = os.getenv("LLM_PROVIDER")
        if llm_provider:
            llm_raw["provider"] = llm_provider

        app_raw = raw.get("app", {})

        phase2_raw = raw.get("phase2", {})
        phase2_weights_raw = phase2_raw.pop("weights", {})

        return cls(
            app_name=app_raw.get("name", "volatility-mispricing-engine"),
            app_version=app_raw.get("version", "1.0.0"),
            environment=app_raw.get("environment", "development"),
            log_level=os.getenv("LOG_LEVEL", app_raw.get("log_level", "INFO")),
            risk_free_rate=float(os.getenv("RISK_FREE_RATE", raw.get("risk_free_rate", 0.045))),
            etoro=EToroConfig(**etoro_raw),
            universe=UniverseConfig(**raw.get("universe", {})),
            realized_vol=RealizedVolConfig(**raw.get("realized_vol", {})),
            filters=FilterConfig(**raw.get("filters", {})),
            scoring=ScoringConfig(**raw.get("scoring", {})),
            phase2=Phase2Config(
                **phase2_raw,
                weights=Phase2Weights(**phase2_weights_raw),
            ),
            llm=LLMConfig(**llm_raw),
            api=APIConfig(**raw.get("api", {})),
            scheduler=SchedulerConfig(**raw.get("scheduler", {})),
            persistence=PersistenceConfig(**raw.get("persistence", {})),
            news=_build_news_config(raw, os.getenv("OPENAI_API_KEY", "")),
            reporting=_build_reporting_config(raw),
        )


def _build_news_config(raw: dict, openai_api_key: str = "") -> NewsConfig:
    news_raw = dict(raw.get("news", {}))
    # Override provider-specific API keys from env
    fmp_key = os.getenv("FMP_API_KEY", "")
    finnhub_key = os.getenv("FINNHUB_API_KEY", "")
    fin_datasets_key = os.getenv("FINANCIAL_DATASETS_API_KEY", "")

    # Auto-select provider based on available keys/packages if not explicitly set
    provider = news_raw.get("provider", "yahoo_finance")
    if provider not in ("mock", "yahoo_finance", "yahoo_finance_mcp"):
        # Provider with required key — keep if key is present, else fall back
        key_available = (
            (provider == "fmp" and fmp_key)
            or (provider == "finnhub" and finnhub_key)
            or (provider == "financial_datasets" and fin_datasets_key)
        )
        if not key_available:
            provider = "yahoo_finance"  # graceful degradation
    elif provider == "mock":
        pass  # explicit mock, keep
    # Check yahoo_finance availability (yfinance must be installed)
    if provider == "yahoo_finance":
        try:
            import yfinance  # noqa: F401
        except ImportError:
            provider = "mock"
    if not provider and fmp_key:
        provider = "fmp"
    elif not provider and finnhub_key:
        provider = "finnhub"
    elif not provider and fin_datasets_key:
        provider = "financial_datasets"
    elif not provider:
        provider = "mock"

    news_raw["provider"] = os.getenv("NEWS_PROVIDER", provider)

    # LLM provider for news feature extraction
    llm_prov = news_raw.get("llm_provider", "mock")
    if openai_api_key and llm_prov == "mock":
        llm_prov = "openai"
    news_raw["llm_provider"] = os.getenv("NEWS_LLM_PROVIDER", llm_prov)

    return NewsConfig(**news_raw)


def _build_reporting_config(raw: dict) -> ReportingConfig:
    rep_raw = dict(raw.get("reporting", {}))
    rep_raw["gdrive_doc_id"] = os.getenv("GDRIVE_DOC_ID", rep_raw.get("gdrive_doc_id", ""))
    rep_raw["gdrive_folder_id"] = os.getenv("GDRIVE_FOLDER_ID", rep_raw.get("gdrive_folder_id", ""))
    return ReportingConfig(**rep_raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_yaml_and_env()
