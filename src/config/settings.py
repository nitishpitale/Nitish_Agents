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
    api_key: str = ""
    timeout_seconds: int = 30
    use_mock: bool = True


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
    enabled: bool = False
    cron: str = "0 18 * * 1-5"


class PersistenceConfig(BaseModel):
    results_dir: str = "./data/results"
    run_hashes_file: str = "./data/run_hashes.json"


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

    @classmethod
    def from_yaml_and_env(cls) -> "Settings":
        raw = _load_yaml()

        # Flatten nested yaml sections into constructor kwargs
        etoro_raw = raw.get("etoro", {})
        etoro_raw["api_key"] = os.getenv("ETORO_API_KEY", etoro_raw.get("api_key", ""))
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
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_yaml_and_env()
