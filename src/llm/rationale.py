"""
LLM rationale generation for option candidates.

IMPORTANT: The LLM is ONLY used for:
  1. Writing concise 2-4 sentence rationale text for each candidate.
  2. Producing human-readable daily summary notes.

The LLM must NOT compute IV, Greeks, model prices, edges, or scores.
All numeric fields passed to the LLM are pre-computed by deterministic code.

Prompt templates are defined here and also documented in llm/prompt_templates.md.
"""

from __future__ import annotations

import json
import logging
import re
from enum import Enum
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM Provider abstraction
# ---------------------------------------------------------------------------


class LLMProvider(str, Enum):
    OPENAI = "openai"
    MOCK = "mock"


# ---------------------------------------------------------------------------
# Prompt templates (also documented in llm/prompt_templates.md)
# ---------------------------------------------------------------------------

RATIONALE_SYSTEM_PROMPT = """\
You are a succinct trading analyst. Your role is to explain why an option is a potential candidate based on pre-computed quantitative data. You must NOT perform any mathematical calculations. All numbers are already given to you — just reference them as-is in plain English.\
"""

RATIONALE_USER_TEMPLATE = """\
Given this JSON object describing a volatility mispricing candidate, write a 2-4 sentence rationale explaining why this option is interesting. Do NOT perform any math. Refer to the numeric fields as given (e.g., "IV={iv:.2%} is low versus forecast {sigma_hat_T:.2%}"). Mention key risks. Output plain text only — no bullet points, no headers.

JSON:
{candidate_json}
"""

DAILY_SUMMARY_SYSTEM_PROMPT = """\
You are a concise trading desk analyst. You produce brief morning briefing notes from pre-computed quantitative data. You do NOT perform any calculations — all numbers are already given. Keep your summary factual, professional, and under 200 words.\
"""

DAILY_SUMMARY_USER_TEMPLATE = """\
Here are the top {n} volatility mispricing candidates from today's scan (run date: {run_date}). Write a short paragraph summary (3-5 sentences) describing the overall tone of opportunities found, then list exactly 3 specific items to watch. Output format:

SUMMARY:
<paragraph>

WATCH LIST:
1. <item>
2. <item>
3. <item>

Candidates JSON:
{candidates_json}
"""


# ---------------------------------------------------------------------------
# Mock LLM (for tests and offline development)
# ---------------------------------------------------------------------------


def _mock_rationale(candidate_data: Dict[str, Any]) -> str:
    """
    Generate a deterministic mock rationale without calling any LLM.
    Used in tests to validate that the rationale references numeric fields
    without performing arithmetic.
    """
    ticker = candidate_data.get("ticker", "N/A")
    iv = candidate_data.get("iv", 0)
    sigma_hat = candidate_data.get("sigma_hat_T", 0)
    edge_vol = candidate_data.get("edge_vol", 0)
    edge_price = candidate_data.get("edge_price", 0)
    vega = candidate_data.get("vega", 0)
    spread = candidate_data.get("bid_ask_spread_pct", 0)
    oi = candidate_data.get("oi", 0)
    opt_type = "call" if candidate_data.get("type") == "C" else "put"
    contract = candidate_data.get("contract", "")

    return (
        f"{ticker} {opt_type} ({contract}): market IV={iv:.2%} is below the realized "
        f"vol forecast of {sigma_hat:.2%}, representing an edge_vol of {edge_vol:.2%}. "
        f"The model fair value suggests an edge_price of {edge_price:.2f} per contract, "
        f"with vega={vega:.3f} amplifying the vol-based advantage. "
        f"Bid-ask spread of {spread:.2%} is manageable given OI of {oi:,}."
    )


def _mock_daily_summary(candidates: List[Dict[str, Any]], run_date: str) -> str:
    n = len(candidates)
    tickers = list({c.get("ticker", "") for c in candidates})
    return (
        f"SUMMARY:\n"
        f"Today's scan ({run_date}) identified {n} volatility mispricing candidates across "
        f"{len(tickers)} underlying(s) ({', '.join(sorted(tickers)[:5])}). "
        f"The top-ranked contracts show meaningful positive edge_vol, suggesting the market "
        f"is underpricing near-term realized volatility. Liquidity filters passed across "
        f"all candidates, with bid-ask spreads and open interest within acceptable bounds.\n\n"
        f"WATCH LIST:\n"
        f"1. Monitor IV movement in top-ranked candidates as the market digests macro data.\n"
        f"2. Track earnings dates for individual equity names — event risk may compress premiums.\n"
        f"3. Review delta exposure across the full book to manage directional bias."
    )


# ---------------------------------------------------------------------------
# Arithmetic-in-LLM-output checker (for tests)
# ---------------------------------------------------------------------------


def check_no_arithmetic(text: str) -> bool:
    """
    Return True if the text appears to contain arithmetic expressions
    that look like the LLM re-computed a value (e.g. "0.28 - 0.22 = 0.06").
    Used in the acceptance test: LLM output must reference numbers but not recompute.
    """
    # Match patterns like: <number> <operator> <number> = <number>
    arithmetic_re = re.compile(
        r"\b\d+\.?\d*\s*[-+*/]\s*\d+\.?\d*\s*=\s*\d+\.?\d*\b"
    )
    return bool(arithmetic_re.search(text))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_rationale(
    candidate_data: Dict[str, Any],
    provider: LLMProvider = LLMProvider.MOCK,
    model: str = "gpt-4o-mini",
    api_key: str = "",
    max_tokens: int = 300,
    temperature: float = 0.2,
) -> str:
    """
    Generate a 2-4 sentence rationale for a single candidate.

    The LLM receives pre-computed numeric fields and must NOT perform
    any calculations. Prompt template is defined in RATIONALE_USER_TEMPLATE.

    Args:
        candidate_data: Dictionary matching the output JSON schema.
        provider: LLM provider to use (openai or mock).
        model: Model name (for OpenAI provider).
        api_key: API key (for OpenAI provider).
        max_tokens: Max response tokens.
        temperature: Sampling temperature.

    Returns:
        Plain text rationale string.
    """
    if provider == LLMProvider.MOCK or not api_key:
        return _mock_rationale(candidate_data)

    if provider == LLMProvider.OPENAI:
        return _openai_rationale(
            candidate_data, model, api_key, max_tokens, temperature
        )

    return _mock_rationale(candidate_data)


def _openai_rationale(
    candidate_data: Dict[str, Any],
    model: str,
    api_key: str,
    max_tokens: int,
    temperature: float,
) -> str:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        # Prepare a clean subset of fields for the prompt
        prompt_fields = {
            k: candidate_data.get(k)
            for k in [
                "ticker", "contract", "market_mid", "iv", "sigma_hat_T",
                "edge_vol", "model_fair_value", "edge_price", "vega", "theta",
                "bid_ask_spread_pct", "oi", "volume", "type", "expiry", "strike",
            ]
        }

        user_content = RATIONALE_USER_TEMPLATE.format(
            iv=prompt_fields.get("iv", 0),
            sigma_hat_T=prompt_fields.get("sigma_hat_T", 0),
            candidate_json=json.dumps(prompt_fields, indent=2),
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": RATIONALE_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()

    except Exception as exc:
        logger.error("OpenAI rationale failed: %s — falling back to mock", exc)
        return _mock_rationale(candidate_data)


def generate_daily_summary(
    candidates: List[Dict[str, Any]],
    run_date: str,
    provider: LLMProvider = LLMProvider.MOCK,
    model: str = "gpt-4o-mini",
    api_key: str = "",
    max_tokens: int = 500,
    temperature: float = 0.2,
) -> str:
    """
    Generate a human-readable daily summary for the top N candidates.

    The LLM receives a JSON array of candidates and must NOT perform
    any calculations. Prompt template is DAILY_SUMMARY_USER_TEMPLATE.

    Returns:
        Multi-line summary string with SUMMARY and WATCH LIST sections.
    """
    if provider == LLMProvider.MOCK or not api_key:
        return _mock_daily_summary(candidates, run_date)

    if provider == LLMProvider.OPENAI:
        return _openai_daily_summary(candidates, run_date, model, api_key, max_tokens, temperature)

    return _mock_daily_summary(candidates, run_date)


def _openai_daily_summary(
    candidates: List[Dict[str, Any]],
    run_date: str,
    model: str,
    api_key: str,
    max_tokens: int,
    temperature: float,
) -> str:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        # Trim to top 10 for summary to keep prompt compact
        top_candidates = candidates[:10]

        user_content = DAILY_SUMMARY_USER_TEMPLATE.format(
            n=len(top_candidates),
            run_date=run_date,
            candidates_json=json.dumps(top_candidates, indent=2),
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": DAILY_SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()

    except Exception as exc:
        logger.error("OpenAI daily summary failed: %s — falling back to mock", exc)
        return _mock_daily_summary(candidates, run_date)
