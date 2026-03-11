"""
LLM-based structured feature extraction from news articles.

The LLM receives NormalizedNewsItem data and outputs a JSON object
conforming to the NewsFeatures schema.

STRICT CONSTRAINTS:
  - The LLM must not compute IV, Greeks, prices, or scores.
  - The LLM must not introduce numbers except confidence values (0.0 – 1.0).
  - The LLM must cite URLs only from the provided articles.
  - Output is validated against the NewsFeatures Pydantic schema.
  - Evidence URLs are filtered against the known article URL set
    (prevents hallucinated or injected URLs).
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import List

import structlog

from .sanitizer import filter_evidence_urls
from .schemas import (
    Catalyst,
    MisPricingHypothesis,
    NewsFeatures,
    NormalizedNewsItem,
    RiskFlag,
    Sentiment,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

NEWS_FEATURE_SYSTEM_PROMPT = """\
You are a financial news analyst. Your only job is to read a set of recent news articles about a stock ticker and extract structured signals in JSON format.

STRICT RULES — violating any rule makes the output invalid:
1. Do NOT perform any math, pricing, or financial calculations.
2. Do NOT add any numbers except confidence values in the range [0.0, 1.0].
3. evidence_urls must ONLY be URLs taken verbatim from the provided articles — no other URLs.
4. Output ONLY valid JSON matching the exact schema below — no prose, no markdown fences.
5. Do NOT speculate beyond what the articles say.
"""

NEWS_FEATURE_USER_TEMPLATE = """\
Ticker: {ticker}
Reference date: {ref_date}

Articles (newest first):
{articles_json}

Extract and return a JSON object with this exact schema:
{{
  "ticker": "{ticker}",
  "catalysts": [
    {{
      "type": "<earnings|guidance|macro|legal|product|analyst|ratings|m&a|regulatory|other>",
      "direction": "<positive|negative|mixed|unclear>",
      "confidence": <0.0 to 1.0>,
      "evidence_urls": ["<url from articles only>"]
    }}
  ],
  "sentiment": {{
    "label": "<positive|negative|mixed|neutral>",
    "confidence": <0.0 to 1.0>
  }},
  "risk_flags": [
    {{
      "type": "<earnings_imminent|litigation|sec_inquiry|guidance_cut|macro_shock|high_short_interest|rumor>",
      "severity": "<low|med|high>",
      "evidence_urls": ["<url from articles only>"]
    }}
  ],
  "why_mispriced_hypotheses": [
    {{
      "hypothesis": "<plain-text explanation referencing article content, no math>",
      "confidence": <0.0 to 1.0>,
      "evidence_urls": ["<url from articles only>"]
    }}
  ]
}}

Return ONLY the JSON object, nothing else.
"""

# Detect if LLM output contains arithmetic (the acceptance-test check)
_ARITHMETIC_RE = re.compile(r"\b\d+\.?\d*\s*[-+*/]\s*\d+\.?\d*\s*=\s*\d+\.?\d*\b")


# ---------------------------------------------------------------------------
# Mock extractor (deterministic, for tests)
# ---------------------------------------------------------------------------


def _mock_extract_features(
    ticker: str, articles: List[NormalizedNewsItem]
) -> NewsFeatures:
    """
    Deterministic mock feature extraction without any LLM call.
    Uses the sentiment_hint and catalyst_hint fields in article.raw.
    """
    if not articles:
        return NewsFeatures(
            ticker=ticker,
            sentiment=Sentiment(label="neutral", confidence=0.0),
        )

    # Aggregate sentiment hints
    sentiments = [
        a.raw.get("sentiment_hint", "neutral") for a in articles
        if isinstance(a.raw, dict)
    ]
    pos = sentiments.count("positive")
    neg = sentiments.count("negative")

    if pos > neg:
        label, conf = "positive", min(0.5 + 0.1 * (pos - neg), 0.9)
    elif neg > pos:
        label, conf = "negative", min(0.5 + 0.1 * (neg - pos), 0.9)
    elif pos == neg and pos > 0:
        label, conf = "mixed", 0.5
    else:
        label, conf = "neutral", 0.0

    # Build catalysts from hints
    catalysts: list[Catalyst] = []
    risk_flags: list[RiskFlag] = []
    hypotheses: list[MisPricingHypothesis] = []
    article_urls = {a.url for a in articles if a.url}

    for art in articles[:3]:  # cap to first 3 for mock conciseness
        cat_hint = art.raw.get("catalyst_hint", "other") if isinstance(art.raw, dict) else "other"
        sent_hint = art.raw.get("sentiment_hint", "neutral") if isinstance(art.raw, dict) else "neutral"
        direction = "positive" if sent_hint == "positive" else (
            "negative" if sent_hint == "negative" else "mixed"
        )

        url_list = [art.url] if art.url in article_urls else []

        if cat_hint == "earnings":
            risk_flags.append(RiskFlag(
                type="earnings_imminent",
                severity="med",
                evidence_urls=url_list,
            ))
        elif cat_hint in ("legal", "regulatory"):
            risk_flags.append(RiskFlag(
                type="litigation" if cat_hint == "legal" else "sec_inquiry",
                severity="med",
                evidence_urls=url_list,
            ))

        cat_type = cat_hint if cat_hint in (
            "earnings", "guidance", "macro", "legal", "product",
            "analyst", "ratings", "regulatory"
        ) else "other"
        catalysts.append(Catalyst(
            type=cat_type,  # type: ignore[arg-type]
            direction=direction,  # type: ignore[arg-type]
            confidence=round(conf * 0.8, 2),
            evidence_urls=url_list,
        ))

    # Generate a mispricing hypothesis based on dominant signals
    if neg > pos:
        hypotheses.append(MisPricingHypothesis(
            hypothesis=(
                f"Recent negative news for {ticker} (including: {articles[0].title}) "
                "may be causing the market to underestimate downside vol, "
                "suppressing put premiums relative to realized risk."
            ),
            confidence=round(conf * 0.6, 2),
            evidence_urls=[articles[0].url] if articles and articles[0].url in article_urls else [],
        ))
    elif pos > neg:
        hypotheses.append(MisPricingHypothesis(
            hypothesis=(
                f"Positive catalysts for {ticker} (including: {articles[0].title}) "
                "may not yet be fully reflected in call IV, "
                "creating a potential vol-of-vol underpricing in upside contracts."
            ),
            confidence=round(conf * 0.6, 2),
            evidence_urls=[articles[0].url] if articles and articles[0].url in article_urls else [],
        ))

    return NewsFeatures(
        ticker=ticker,
        catalysts=catalysts[:5],
        sentiment=Sentiment(label=label, confidence=round(conf, 2)),  # type: ignore[arg-type]
        risk_flags=risk_flags[:5],
        why_mispriced_hypotheses=hypotheses[:3],
    )


# ---------------------------------------------------------------------------
# OpenAI extractor
# ---------------------------------------------------------------------------


def _openai_extract_features(
    ticker: str,
    articles: List[NormalizedNewsItem],
    model: str,
    api_key: str,
    ref_date: date,
    max_tokens: int = 800,
    temperature: float = 0.0,
) -> NewsFeatures:
    """Call OpenAI with a strict JSON schema prompt, validate output."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        # Prepare compact article list for the prompt (limit to 10)
        article_dicts = [
            {
                "title": a.title,
                "source": a.source,
                "published_at": a.published_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url": a.url,
                "summary": a.summary or "",
            }
            for a in articles[:10]
        ]

        user_content = NEWS_FEATURE_USER_TEMPLATE.format(
            ticker=ticker,
            ref_date=ref_date.isoformat(),
            articles_json=json.dumps(article_dicts, indent=2),
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": NEWS_FEATURE_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            response_format={"type": "json_object"},
        )

        raw_text = resp.choices[0].message.content or "{}"

        # Safety: reject if arithmetic expressions detected
        if _ARITHMETIC_RE.search(raw_text):
            log.warning("news_features.arithmetic_detected", ticker=ticker)
            return _mock_extract_features(ticker, articles)

        return _parse_and_validate_features(ticker, raw_text, articles)

    except Exception as exc:
        log.error("news_features.openai_error", ticker=ticker, error=str(exc))
        return _mock_extract_features(ticker, articles)


def _parse_and_validate_features(
    ticker: str,
    raw_json: str,
    articles: List[NormalizedNewsItem],
) -> NewsFeatures:
    """
    Parse LLM output, validate against schema, and sanitise evidence URLs.
    Falls back to mock on any validation failure.
    """
    article_urls = {a.url for a in articles if a.url}

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        log.warning("news_features.json_parse_error", ticker=ticker, error=str(exc))
        return _mock_extract_features(ticker, articles)

    # Sanitise all evidence_urls to only include article URLs
    for cat in data.get("catalysts", []):
        cat["evidence_urls"] = filter_evidence_urls(
            cat.get("evidence_urls", []), article_urls
        )
    for rf in data.get("risk_flags", []):
        rf["evidence_urls"] = filter_evidence_urls(
            rf.get("evidence_urls", []), article_urls
        )
    for hyp in data.get("why_mispriced_hypotheses", []):
        hyp["evidence_urls"] = filter_evidence_urls(
            hyp.get("evidence_urls", []), article_urls
        )

    try:
        features = NewsFeatures.model_validate(data)
        features = features.model_copy(update={"ticker": ticker.upper()})
        return features
    except Exception as exc:
        log.warning("news_features.schema_validation_error", ticker=ticker, error=str(exc))
        return _mock_extract_features(ticker, articles)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_news_features(
    ticker: str,
    articles: List[NormalizedNewsItem],
    ref_date: date,
    provider: str = "mock",
    model: str = "gpt-4o-mini",
    api_key: str = "",
    max_tokens: int = 800,
    temperature: float = 0.0,
) -> NewsFeatures:
    """
    Extract structured NewsFeatures from a list of articles.

    Args:
        ticker: Stock ticker symbol.
        articles: Normalized articles from the news provider.
        ref_date: Reference date for the run.
        provider: "openai" or "mock".
        model: LLM model name (for OpenAI).
        api_key: API key (for OpenAI).
        max_tokens: Max response tokens.
        temperature: Sampling temperature (use 0.0 for determinism).

    Returns:
        NewsFeatures object (always — never raises).
    """
    if not articles:
        log.debug("news_features.no_articles", ticker=ticker)
        return NewsFeatures(
            ticker=ticker,
            sentiment=Sentiment(label="neutral", confidence=0.0),
        )

    if provider == "openai" and api_key:
        return _openai_extract_features(
            ticker=ticker,
            articles=articles,
            model=model,
            api_key=api_key,
            ref_date=ref_date,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    return _mock_extract_features(ticker, articles)
