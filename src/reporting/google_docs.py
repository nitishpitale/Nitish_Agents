"""
Google Docs writer — structured table format.

Output layout (matches user template):
  ┌─────────────────────────────────────────────────────┐
  │  Date header + Summary paragraph  (font size 11)    │
  ├─────────────┬──────────────┬────────┬───────────────┤
  │ Ticker name │ Call/Put +   │ Strike │  Volatility   │  ...
  │             │ Expiry date  │ Price  │               │
  ├─────────────┼──────────────┼────────┼───────────────┤
  │   5 rows of candidate data (font size 8)            │
  └─────────────────────────────────────────────────────┘

Authentication priority (first available wins):
  1. GOOGLE_SERVICE_ACCOUNT_JSON  — base64 service-account JSON key
  2. GOOGLE_APPLICATION_CREDENTIALS — path to key file on disk
  3. GOOGLE_OAUTH_REFRESH_TOKEN + GOOGLE_OAUTH_CLIENT_ID + CLIENT_SECRET
  4. GOOGLE_ACCESS_TOKEN or data/.google_token.json  (access-token file)
  5. Local markdown file fallback

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PERMANENT SETUP OPTIONS (no token refresh ever needed)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Option A — Service Account (recommended, 5 min):
  1. console.cloud.google.com → IAM → Service Accounts → Create
  2. Create JSON key → download
  3. Share your Google Doc with the service account email (Editor)
  4. Add to Cursor Secrets:
       GOOGLE_SERVICE_ACCOUNT_JSON = $(base64 -w0 key.json)

Option B — Gmail App Password (email delivery, 2 min):
  See src/reporting/email_reporter.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import structlog

log = structlog.get_logger(__name__)
logger = logging.getLogger(__name__)

_DOC_ID_ENV = "GDRIVE_DOC_ID"
_FOLDER_ID_ENV = "GDRIVE_FOLDER_ID"
_FALLBACK_DIR = Path("data/reports")
_TOKEN_FILE = Path(os.getenv("GOOGLE_TOKEN_FILE", "data/.google_token.json"))

_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

_PLAYGROUND_CLIENT_ID = "407408718192.apps.googleusercontent.com"

# Table column headers (matches template)
_TABLE_HEADERS = [
    "Ticker\nname",
    "Call/Put +\nExpiry date",
    "Strike\nPrice",
    "Volatility",
    "Pricing",
    "Liquidity",
    "Final score\nand Sentiment",
]

_TABLE_COLS = len(_TABLE_HEADERS)
_TABLE_ROWS = 6  # 1 header + 5 data rows


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _build_credentials():
    """Build Google credentials — first available method wins."""
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth import default as gad

    # 1. Service account (base64 JSON env var)
    sa_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if sa_b64:
        try:
            sa_info = json.loads(base64.b64decode(sa_b64).decode())
            creds = service_account.Credentials.from_service_account_info(sa_info, scopes=_SCOPES)
            log.info("google_auth.service_account")
            return creds
        except Exception as exc:
            logger.error("google_auth: SA decode failed: %s", exc)

    # 2. Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS file)
    try:
        creds, _ = gad(scopes=_SCOPES)
        if creds:
            log.info("google_auth.application_default")
            return creds
    except Exception:
        pass

    # 3. OAuth refresh token
    refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "")
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", _PLAYGROUND_CLIENT_ID)
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if refresh_token and client_secret:
        try:
            creds = Credentials(
                token=None, refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id, client_secret=client_secret, scopes=_SCOPES,
            )
            creds.refresh(Request())
            log.info("google_auth.oauth_refresh_token")
            return creds
        except Exception as exc:
            logger.error("google_auth: refresh token failed: %s", exc)

    # 4. Access token (env var or token file)
    access_token = os.getenv("GOOGLE_ACCESS_TOKEN", "")
    if not access_token and _TOKEN_FILE.exists():
        try:
            data = json.loads(_TOKEN_FILE.read_text())
            access_token = data.get("access_token", "")
            stored_refresh = data.get("refresh_token", "")
            stored_client_id = data.get("client_id", _PLAYGROUND_CLIENT_ID)
            stored_client_secret = data.get("client_secret", "")
            # Try to auto-refresh if secret is available
            if stored_refresh and stored_client_secret:
                creds = Credentials(
                    token=access_token or None,
                    refresh_token=stored_refresh,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=stored_client_id,
                    client_secret=stored_client_secret,
                    scopes=_SCOPES,
                )
                if not creds.valid:
                    creds.refresh(Request())
                    data["access_token"] = creds.token
                    _TOKEN_FILE.write_text(json.dumps(data, indent=2))
                log.info("google_auth.token_file_refreshed")
                return creds
        except Exception as exc:
            logger.warning("google_auth: token file error: %s", exc)

    if access_token:
        try:
            creds = Credentials(token=access_token, scopes=_SCOPES)
            log.info("google_auth.access_token")
            return creds
        except Exception as exc:
            logger.error("google_auth: access token failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Data formatting helpers
# ---------------------------------------------------------------------------

def _stars(score: float) -> str:
    if score >= 20: return "★★★★★"
    if score >= 10: return "★★★★☆"
    if score >= 3:  return "★★★☆☆"
    if score >= 1:  return "★★☆☆☆"
    return "★☆☆☆☆"


def _extract_summary(daily_summary: str) -> str:
    if not daily_summary:
        return ""
    if "SUMMARY:" in daily_summary:
        text = daily_summary.split("SUMMARY:")[-1]
        if "WATCH LIST:" in text:
            text = text.split("WATCH LIST:")[0]
        return text.strip()
    return daily_summary.strip()[:400]


def _format_candidate_row(candidate) -> List[str]:
    """Format one CandidateResult into a 7-cell data row."""
    d = candidate.to_dict()
    opt_type = "CALL" if d["type"] == "C" else "PUT"

    volatility = (
        f"IV: {d['iv']*100:.2f}%\n"
        f"Forecast: {d['sigma_hat_T']*100:.2f}%\n"
        f"Edge Vol: {d['edge_vol']*100:+.2f}%"
    )
    pricing = (
        f"Mid: ${d['market_mid']:.2f}\n"
        f"Fair: ${d['model_fair_value']:.2f}\n"
        f"Edge: {d['edge_price']:+.2f}"
    )
    liquidity = (
        f"OI: {d['oi']:,}\n"
        f"Vol: {d['volume']:,}\n"
        f"Spread: {d['bid_ask_spread_pct']*100:.2f}%"
    )
    score = d.get("score", 0)
    news_feat = (d.get("news") or {}).get("features") or {}
    sentiment = (news_feat.get("sentiment") or {}).get("label", "neutral").capitalize()
    final = f"Score: {score:.2f}\n{_stars(score)}\n{sentiment}"

    return [
        d["ticker"],
        f"{opt_type}\n{d['expiry']}",
        f"${d['strike']:.2f}",
        volatility,
        pricing,
        liquidity,
        final,
    ]


# ---------------------------------------------------------------------------
# Google Docs API table builder
# ---------------------------------------------------------------------------

def _find_table_cell_indices(doc: dict) -> List[List[int]]:
    """
    Navigate the document JSON and return cell start indices as
    cells[row][col].  Only the first table in the document is used.
    """
    cells: List[List[int]] = []
    for element in doc.get("body", {}).get("content", []):
        if "table" not in element:
            continue
        for row in element["table"].get("tableRows", []):
            row_cells = []
            for cell in row.get("tableCells", []):
                cell_start = None
                for para in cell.get("content", []):
                    if "paragraph" in para:
                        els = para["paragraph"].get("elements", [])
                        cell_start = els[0]["startIndex"] if els else para.get("startIndex", 0) + 1
                        break
                row_cells.append(cell_start if cell_start is not None
                                  else cell.get("startIndex", 0) + 2)
            cells.append(row_cells)
        break  # Only first table
    return cells


def _write_structured_report_to_doc(
    creds,
    doc_id: str,
    candidates: list,
    daily_summary: str,
    date_str: str,
) -> bool:
    """
    Write the structured report to Google Docs in the user-defined format:
      - Summary paragraph at font size 11
      - 6×7 table at font size 8 (1 header row + 5 candidate rows)
    """
    from googleapiclient.discovery import build

    service = build("docs", "v1", credentials=creds, cache_discovery=False)

    try:
        # ── Step 1: Clear the document body ──────────────────────────────
        doc = service.documents().get(documentId=doc_id).execute()
        body_content = doc.get("body", {}).get("content", [])
        end_idx = body_content[-1].get("endIndex", 2) - 1 if body_content else 1

        if end_idx > 1:
            service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_idx}
                }}]},
            ).execute()

        # ── Step 2: Build summary block ───────────────────────────────────
        summary_text = _extract_summary(daily_summary)
        header_line = f"📈 Top 5 Volatility Mispricing — {date_str}\n"
        body_text = f"{summary_text}\n\n" if summary_text else "\n"
        full_header = header_line + body_text

        init_requests: List[dict] = [
            # Insert date header + summary
            {"insertText": {"location": {"index": 1}, "text": full_header}},
            # Summary body: font size 11, normal weight
            {
                "updateTextStyle": {
                    "range": {"startIndex": 1, "endIndex": 1 + len(full_header)},
                    "textStyle": {"fontSize": {"magnitude": 11, "unit": "PT"}},
                    "fields": "fontSize",
                }
            },
            # Header line: bold + size 12
            {
                "updateTextStyle": {
                    "range": {"startIndex": 1, "endIndex": 1 + len(header_line)},
                    "textStyle": {
                        "bold": True,
                        "fontSize": {"magnitude": 12, "unit": "PT"},
                    },
                    "fields": "bold,fontSize",
                }
            },
            # Insert 6×7 table immediately after the summary block
            {
                "insertTable": {
                    "rows": _TABLE_ROWS,
                    "columns": _TABLE_COLS,
                    "location": {"index": 1 + len(full_header)},
                }
            },
        ]
        service.documents().batchUpdate(
            documentId=doc_id, body={"requests": init_requests}
        ).execute()

        # ── Step 3: Fetch updated doc and locate all cell indices ─────────
        doc = service.documents().get(documentId=doc_id).execute()
        cells = _find_table_cell_indices(doc)

        if not cells or len(cells) < _TABLE_ROWS:
            logger.error("google_docs: could not locate table cells (found %d rows)", len(cells))
            return False

        # ── Step 4: Build rows ────────────────────────────────────────────
        rows_data: List[List[str]] = [_TABLE_HEADERS]
        for c in candidates[:5]:
            rows_data.append(_format_candidate_row(c))

        # Pad with empty rows if fewer than 5 candidates
        while len(rows_data) < _TABLE_ROWS:
            rows_data.append([""] * _TABLE_COLS)

        # Collect (cell_index, text, is_header) sorted HIGH → LOW
        # so later insertions don't shift earlier cell indices
        cell_fills: List[tuple] = []
        for row_i, row in enumerate(rows_data):
            if row_i >= len(cells):
                break
            for col_i, text in enumerate(row):
                if col_i >= len(cells[row_i]) or not text:
                    continue
                cell_fills.append((cells[row_i][col_i], text, row_i == 0))

        cell_fills.sort(key=lambda x: x[0], reverse=True)

        # ── Step 5: Fill all cells in one batchUpdate ─────────────────────
        fill_requests: List[dict] = []
        for cell_idx, text, is_header in cell_fills:
            fill_requests.append({
                "insertText": {
                    "location": {"index": cell_idx},
                    "text": text,
                }
            })
            end = cell_idx + len(text)
            style: dict = {"fontSize": {"magnitude": 8, "unit": "PT"}}
            fields = "fontSize"
            if is_header:
                style["bold"] = True
                fields = "bold,fontSize"
            fill_requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": cell_idx, "endIndex": end},
                    "textStyle": style,
                    "fields": fields,
                }
            })

        if fill_requests:
            service.documents().batchUpdate(
                documentId=doc_id, body={"requests": fill_requests}
            ).execute()

        log.info("google_docs.structured_report_written", doc_id=doc_id)
        return True

    except Exception as exc:
        logger.error("google_docs.structured_write_failed: %s", exc, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Local fallback
# ---------------------------------------------------------------------------

def _write_local_fallback(markdown: str, run_date: datetime) -> Path:
    _FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    path = _FALLBACK_DIR / f"{run_date.strftime('%Y-%m-%d')}_top5.md"
    path.write_text(markdown)
    return path


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_gdrive_mcp_available() -> bool:
    import shutil
    return shutil.which("npx") is not None


def is_google_api_available() -> bool:
    return bool(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or (os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"))
        or os.getenv("GOOGLE_ACCESS_TOKEN")
        or _TOKEN_FILE.exists()
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def write_report_to_docs(
    markdown: str,
    run_date: datetime,
    doc_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    candidates: Optional[list] = None,
    daily_summary: str = "",
) -> dict:
    """
    Write the daily report to Google Docs in the structured table format.

    When `candidates` is supplied the full structured layout (summary + table)
    is written directly via the Docs API.  Falls back to local markdown file
    if no credentials are available.

    Returns dict: method, location, success, message.
    """
    doc_id = doc_id or os.getenv(_DOC_ID_ENV, "")
    folder_id = folder_id or os.getenv(_FOLDER_ID_ENV, "")
    date_str = run_date.strftime("%A, %B %-d, %Y")

    creds = _build_credentials()

    if creds is not None and candidates is not None and doc_id:
        # Structured table format (preferred)
        ok = _write_structured_report_to_doc(
            creds, doc_id, candidates, daily_summary, date_str
        )
        if ok:
            url = f"https://docs.google.com/document/d/{doc_id}/edit"
            return {
                "method": "google_api_structured",
                "location": url,
                "success": True,
                "message": f"✅  Structured report written to Google Doc: {url}",
            }

    if creds is not None and doc_id:
        # Fallback: write raw markdown to the doc
        from .formatter import _build_docs_markdown
        try:
            from googleapiclient.discovery import build
            service = build("docs", "v1", credentials=creds, cache_discovery=False)
            doc = service.documents().get(documentId=doc_id).execute()
            end_idx = doc["body"]["content"][-1].get("endIndex", 2) - 1
            reqs: List[dict] = []
            if end_idx > 1:
                reqs.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_idx}}})
            reqs.append({"insertText": {"location": {"index": 1}, "text": markdown}})
            service.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()
            url = f"https://docs.google.com/document/d/{doc_id}/edit"
            return {
                "method": "google_api_markdown",
                "location": url,
                "success": True,
                "message": f"✅  Report written to Google Doc: {url}",
            }
        except Exception as exc:
            logger.error("google_docs.markdown_write_failed: %s", exc)

    # Local file fallback
    path = _write_local_fallback(markdown, run_date)
    log.warning(
        "google_docs.no_auth_fallback",
        local_file=str(path),
        hint="Add GOOGLE_SERVICE_ACCOUNT_JSON to Cursor Secrets for permanent access.",
    )
    return {
        "method": "local_file",
        "location": str(path),
        "success": True,
        "message": (
            f"⚠️  No Google credentials — saved locally: {path}\n"
            f"    Add GOOGLE_SERVICE_ACCOUNT_JSON or GMAIL_APP_PASSWORD to Cursor Secrets."
        ),
    }
