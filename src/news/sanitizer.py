"""
Security sanitizer for all MCP and external news provider outputs.

MCP server responses are treated as UNTRUSTED per the security spec:
  - Validate all URLs against an http/https allowlist
  - Strip HTML/script tags from text content
  - Never execute or eval returned content
  - Enforce a per-string length cap to prevent prompt injection
  - Keep an allowlist of known safe MCP servers (name, pinned version)

This module is the single choke-point through which all external
provider content must pass before it reaches the rest of the system.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# MCP server allowlist — pinned to reviewed versions
# ---------------------------------------------------------------------------

MCP_SERVER_ALLOWLIST: Dict[str, str] = {
    "mcp-server-financialmodelingprep": ">=0.1.0",
    "financial-datasets-mcp": ">=0.1.0",
    "finance-news-mcp": ">=0.1.0",
    "yahoo-finance-news": ">=1.0.0",   # local MCP server (mcp_servers/yahoo_finance_news)
    # Add verified servers here; anything outside this list is rejected.
}

# Max character lengths to prevent prompt injection via oversized fields
_MAX_TITLE_LEN = 500
_MAX_SUMMARY_LEN = 2000
_MAX_URL_LEN = 2048

# Strip HTML tags pattern
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
# Strip script/style blocks entirely (not just tags)
_SCRIPT_BLOCK_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
# Detect possible prompt-injection keywords in article text
_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+previous\s+instructions|system\s*:|<\|im_start\||</s>|\[INST\])",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public sanitisation helpers
# ---------------------------------------------------------------------------


def validate_mcp_server(name: str, version: Optional[str] = None) -> bool:
    """
    Return True if the MCP server is on the allowlist.
    Logs a warning but does not raise — callers decide how to handle failures.
    """
    if name not in MCP_SERVER_ALLOWLIST:
        return False
    # Version check is advisory (full semver would require packaging dep)
    return True


def sanitize_url(url: str) -> Optional[str]:
    """
    Validate a URL from an untrusted source.

    Rules:
      - Must use http or https scheme
      - Must not exceed _MAX_URL_LEN
      - Must parse cleanly
      - No javascript: / data: / file: schemes

    Returns the cleaned URL or None if invalid.
    """
    if not url or len(url) > _MAX_URL_LEN:
        return None

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    if not parsed.netloc:
        return None

    # Reject obvious injection attempts in URL
    if _INJECTION_PATTERNS.search(url):
        return None

    return url


def sanitize_text(text: str, max_len: int = _MAX_SUMMARY_LEN) -> str:
    """
    Strip HTML tags, script blocks, decode HTML entities, and cap length.
    Flags prompt-injection attempts by truncating to an empty string.
    """
    if not text:
        return ""

    # Remove script/style blocks first
    cleaned = _SCRIPT_BLOCK_RE.sub(" ", text)
    # Remove remaining HTML tags
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    # Decode HTML entities (e.g., &amp; → &)
    cleaned = html.unescape(cleaned)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Hard-cap length
    cleaned = cleaned[:max_len]

    # Flag potential prompt injection — replace entire content if detected
    if _INJECTION_PATTERNS.search(cleaned):
        return "[content removed: possible injection attempt]"

    return cleaned


def sanitize_title(title: str) -> str:
    return sanitize_text(title, max_len=_MAX_TITLE_LEN)


def sanitize_article_dict(raw: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """
    Sanitise a raw article dict from a news provider.
    Returns a new dict with all string fields cleaned.
    """
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, str):
            if "url" in k.lower() or "link" in k.lower():
                cleaned = sanitize_url(v)
                out[k] = cleaned or ""
            elif k.lower() in ("title", "headline"):
                out[k] = sanitize_title(v)
            else:
                out[k] = sanitize_text(v)
        elif isinstance(v, (int, float, bool)):
            out[k] = v
        elif isinstance(v, list):
            out[k] = [sanitize_text(str(i)) if isinstance(i, str) else i for i in v]
        elif v is None:
            out[k] = None
        # Discard nested dicts from raw — flattened by provider clients
    return out


def filter_evidence_urls(
    urls: List[str], allowed_urls: set[str]
) -> List[str]:
    """
    Filter a list of evidence URLs from LLM output to only include URLs
    that were actually present in the provided articles.
    This prevents the LLM from hallucinating or injecting URLs.
    """
    result = []
    for url in urls:
        safe = sanitize_url(url)
        if safe and safe in allowed_urls:
            result.append(safe)
    return result


def check_no_arithmetic_news(text: str) -> bool:
    """
    Return True if the text appears to contain arithmetic expressions
    (e.g. "0.28 - 0.22 = 0.06").  Used in acceptance tests.
    Shared with the main llm.rationale module's checker.
    """
    arithmetic_re = re.compile(
        r"\b\d+\.?\d*\s*[-+*/]\s*\d+\.?\d*\s*=\s*\d+\.?\d*\b"
    )
    return bool(arithmetic_re.search(text))
