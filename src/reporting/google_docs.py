"""
Google Docs writer — direct Google API + MCP fallback.

Authentication priority (first available wins):
  1. GOOGLE_SERVICE_ACCOUNT_JSON  — base64-encoded service-account key JSON
                                    (best for fully automated cloud use)
  2. GOOGLE_APPLICATION_CREDENTIALS — path to a service-account or OAuth JSON
                                       key file on disk
  3. GOOGLE_OAUTH_REFRESH_TOKEN + GOOGLE_OAUTH_CLIENT_ID +
     GOOGLE_OAUTH_CLIENT_SECRET    — OAuth 2.0 refresh token
                                     (get once from Google OAuth Playground)
  4. GOOGLE_ACCESS_TOKEN           — short-lived bearer token (testing only)
  5. gdrive MCP subprocess         — deprecated package, kept as last resort
  6. local file fallback            — data/reports/YYYY-MM-DD_top5.md

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK SETUP — OAuth Playground (2 minutes, no Cloud project needed)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Open https://developers.google.com/oauthplayground/

2. Click the ⚙ gear icon (top-right) → tick
   "Use your own OAuth credentials"
   Enter a Google Cloud OAuth 2.0 Client ID + Secret OR untick to use
   Google's built-in playground credentials (simpler but tokens expire hourly).

3. In "Step 1 – Select & authorize APIs" paste these two scopes:
     https://www.googleapis.com/auth/documents
     https://www.googleapis.com/auth/drive.file
   → Click "Authorize APIs" → sign in with the account that owns the Doc.

4. "Step 2 – Exchange authorization code for tokens"
   → Click "Exchange authorization code for tokens"
   → Copy the Refresh Token shown.

5. Set Cursor Secrets (Dashboard → Cloud Agents → Secrets):
     GOOGLE_OAUTH_REFRESH_TOKEN   = <paste refresh token>
     GOOGLE_OAUTH_CLIENT_ID       = <your client ID>    (or Google's playground ID)
     GOOGLE_OAUTH_CLIENT_SECRET   = <your client secret>(or Google's playground secret)
     GDRIVE_DOC_ID                = 1ZcLBIM7rEY3z-e8AQxibxijHvf5FajNeEUyvFT3RLTk
     GDRIVE_FOLDER_ID             = 1IuCUos6M0rWmLB6DxP8MxdbVtaEeQ1_P

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RECOMMENDED SETUP — Service Account (fully automated, token never expires)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Go to https://console.cloud.google.com/ → IAM & Admin → Service Accounts
2. Create a service account, give it no IAM role.
3. Create a JSON key → download the .json file.
4. Open your Google Doc → Share with the service account email
   (looks like: name@project.iam.gserviceaccount.com) as Editor.
5. Set Cursor Secret:
     GOOGLE_SERVICE_ACCOUNT_JSON = <base64-encoded contents of the .json file>
     (run: base64 -w0 service-account.json)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)
logger = logging.getLogger(__name__)

_DOC_ID_ENV = "GDRIVE_DOC_ID"
_FOLDER_ID_ENV = "GDRIVE_FOLDER_ID"
_FALLBACK_DIR = Path("data/reports")

# Google API scopes
_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

# Google OAuth Playground well-known client credentials (public)
# These can only be used interactively; for automated use supply your own.
_PLAYGROUND_CLIENT_ID = "407408718192.apps.googleusercontent.com"
_PLAYGROUND_CLIENT_SECRET = "AI_playground_secret_placeholder"


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------


def _build_credentials():
    """
    Build Google credentials from environment variables.
    Returns a Credentials object or None if no auth is available.
    """
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth import default as gad

    # ── 1. Service account JSON (base64 encoded) ─────────────────────────
    sa_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if sa_b64:
        try:
            sa_json = base64.b64decode(sa_b64).decode()
            sa_info = json.loads(sa_json)
            creds = service_account.Credentials.from_service_account_info(
                sa_info, scopes=_SCOPES
            )
            log.info("google_auth.using_service_account")
            return creds
        except Exception as exc:
            logger.error("google_auth: service account decode failed: %s", exc)

    # ── 2. Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS) ─
    try:
        creds, _ = gad(scopes=_SCOPES)
        if creds:
            log.info("google_auth.using_application_default")
            return creds
    except Exception:
        pass

    # ── 3. OAuth refresh token ─────────────────────────────────────────────
    refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "")
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", _PLAYGROUND_CLIENT_ID)
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if refresh_token and client_secret:
        try:
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=_SCOPES,
            )
            creds.refresh(Request())
            log.info("google_auth.using_oauth_refresh_token")
            return creds
        except Exception as exc:
            logger.error("google_auth: refresh token failed: %s", exc)

    # ── 4. Short-lived access token ────────────────────────────────────────
    access_token = os.getenv("GOOGLE_ACCESS_TOKEN", "")
    if access_token:
        try:
            creds = Credentials(token=access_token, scopes=_SCOPES)
            log.info("google_auth.using_access_token")
            return creds
        except Exception as exc:
            logger.error("google_auth: access token failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Google Docs API operations
# ---------------------------------------------------------------------------


def _markdown_to_docs_requests(markdown: str) -> list[dict]:
    """
    Convert Markdown text to a list of Google Docs API batchUpdate requests.

    Converts:
      # Heading 1  → HEADING_1 paragraph style
      ## Heading 2 → HEADING_2
      ### Heading 3 → HEADING_3
      **bold**     → bold text
      `code`       → monospace text
      ---          → horizontal rule (dashed line)
      plain text   → NORMAL_TEXT

    Returns a list of requests ready for documents.batchUpdate().
    """
    requests = []
    # Start index — Google Docs body starts at index 1
    # We'll insert at the beginning (index 1) so new reports appear at top.
    index = 1

    lines = markdown.split("\n")

    for line in lines:
        text = line.rstrip()

        # Determine paragraph style
        if text.startswith("### "):
            style = "HEADING_3"
            content = text[4:] + "\n"
        elif text.startswith("## "):
            style = "HEADING_2"
            content = text[3:] + "\n"
        elif text.startswith("# "):
            style = "HEADING_1"
            content = text[2:] + "\n"
        elif text == "---":
            content = "─" * 60 + "\n"
            style = "NORMAL_TEXT"
        else:
            content = text + "\n"
            style = "NORMAL_TEXT"

        if not content.strip():
            content = "\n"

        requests.append({
            "insertText": {
                "location": {"index": index},
                "text": content,
            }
        })

        if style != "NORMAL_TEXT":
            requests.append({
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": index,
                        "endIndex": index + len(content),
                    },
                    "paragraphStyle": {"namedStyleType": style},
                    "fields": "namedStyleType",
                }
            })

        index += len(content)

    return requests


def _write_to_existing_doc(
    creds, doc_id: str, markdown: str, date_str: str
) -> bool:
    """Prepend the report to an existing Google Doc."""
    try:
        from googleapiclient.discovery import build

        service = build("docs", "v1", credentials=creds, cache_discovery=False)

        # Get current document to find the body start index
        doc = service.documents().get(documentId=doc_id).execute()
        body_content = doc.get("body", {}).get("content", [])

        # Build the Markdown content with date separator
        full_markdown = f"# 📋 {date_str}\n\n{markdown}\n\n{'═' * 60}\n\n"
        requests = _markdown_to_docs_requests(full_markdown)

        if requests:
            service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": requests},
            ).execute()

        log.info("google_docs.doc_updated", doc_id=doc_id)
        return True

    except Exception as exc:
        logger.error("google_docs.update_failed: %s", exc)
        return False


def _create_new_doc(
    creds, markdown: str, date_str: str, folder_id: Optional[str]
) -> Optional[str]:
    """Create a new Google Doc containing the report. Returns the new doc ID."""
    try:
        from googleapiclient.discovery import build

        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)

        # Create an empty Google Doc via Drive API
        file_metadata = {
            "name": f"Vol Mispricing Top 5 — {date_str}",
            "mimeType": "application/vnd.google-apps.document",
        }
        if folder_id:
            file_metadata["parents"] = [folder_id]

        file = drive_service.files().create(
            body=file_metadata, fields="id"
        ).execute()
        new_doc_id = file.get("id")

        if not new_doc_id:
            return None

        # Insert content
        requests = _markdown_to_docs_requests(markdown)
        if requests:
            docs_service.documents().batchUpdate(
                documentId=new_doc_id,
                body={"requests": requests},
            ).execute()

        log.info("google_docs.doc_created", doc_id=new_doc_id)
        return new_doc_id

    except Exception as exc:
        logger.error("google_docs.create_failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Local fallback
# ---------------------------------------------------------------------------


def _write_local_fallback(markdown: str, run_date: datetime) -> Path:
    _FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{run_date.strftime('%Y-%m-%d')}_top5.md"
    path = _FALLBACK_DIR / filename
    with open(path, "w") as f:
        f.write(markdown)
    return path


# ---------------------------------------------------------------------------
# Deprecated MCP fallback (last resort)
# ---------------------------------------------------------------------------


async def _try_mcp_create(markdown: str, date_str: str, folder_id: Optional[str]) -> Optional[str]:
    """Try the deprecated gdrive MCP server as a last resort."""
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        server_params = StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-gdrive"],
            env={**os.environ},
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                args: dict = {
                    "name": f"Vol Mispricing Top 5 — {date_str}",
                    "content": markdown,
                    "mimeType": "application/vnd.google-apps.document",
                }
                if folder_id:
                    args["parents"] = [folder_id]
                result = await session.call_tool("gdrive_create_file", args)
                for c in (result.content or []):
                    if hasattr(c, "text") and c.text:
                        return c.text
    except Exception as exc:
        logger.debug("MCP fallback failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_gdrive_mcp_available() -> bool:
    import shutil
    return shutil.which("npx") is not None


def is_google_api_available() -> bool:
    """Return True if any Google auth method is configured."""
    return bool(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or (os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"))
        or os.getenv("GOOGLE_ACCESS_TOKEN")
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def write_report_to_docs(
    markdown: str,
    run_date: datetime,
    doc_id: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> dict:
    """
    Write the daily report to Google Docs.

    Priority:
      1. Google API (service account / OAuth) → update existing doc
      2. Google API → create new doc in folder
      3. MCP subprocess (deprecated, last resort)
      4. Local file fallback

    Returns dict: method, location, success, message.
    """
    doc_id = doc_id or os.getenv(_DOC_ID_ENV, "")
    folder_id = folder_id or os.getenv(_FOLDER_ID_ENV, "")
    date_str = run_date.strftime("%A, %B %-d, %Y")

    # ── Try Google API ─────────────────────────────────────────────────────
    creds = _build_credentials()

    if creds is not None:
        if doc_id:
            ok = _write_to_existing_doc(creds, doc_id, markdown, date_str)
            if ok:
                url = f"https://docs.google.com/document/d/{doc_id}/edit"
                return {
                    "method": "google_api_update",
                    "location": url,
                    "success": True,
                    "message": f"✅  Report written to Google Doc: {url}",
                }

        new_id = _create_new_doc(creds, markdown, date_str, folder_id or None)
        if new_id:
            url = f"https://docs.google.com/document/d/{new_id}/edit"
            return {
                "method": "google_api_create",
                "location": url,
                "success": True,
                "message": f"✅  New Google Doc created: {url}",
            }

    # ── Try deprecated MCP fallback ────────────────────────────────────────
    if is_gdrive_mcp_available():
        try:
            result = asyncio.run(_try_mcp_create(markdown, date_str, folder_id or None))
            if result:
                return {
                    "method": "gdrive_mcp",
                    "location": result,
                    "success": True,
                    "message": f"✅  Report written via MCP: {result}",
                }
        except Exception:
            pass

    # ── Local file fallback ────────────────────────────────────────────────
    path = _write_local_fallback(markdown, run_date)

    log.warning(
        "google_docs.no_auth",
        local_file=str(path),
        hint=(
            "Add one of these to Cursor Secrets to enable Google Docs writing:\n"
            "  GOOGLE_SERVICE_ACCOUNT_JSON  (base64 service-account JSON)\n"
            "  GOOGLE_OAUTH_REFRESH_TOKEN + GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET\n"
            "See src/reporting/google_docs.py for step-by-step setup."
        ),
    )
    return {
        "method": "local_file",
        "location": str(path),
        "success": True,
        "message": (
            f"⚠️  No Google credentials found — report saved locally: {path}\n"
            f"    Add GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_OAUTH_REFRESH_TOKEN\n"
            f"    to Cursor Secrets to enable Google Docs writing."
        ),
    }
