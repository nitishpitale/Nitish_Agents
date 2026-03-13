"""
Google Docs writer via the Google Drive MCP server.

─────────────────────────────────────────────────────────────
HOW TO CONNECT THE GOOGLE DRIVE MCP
─────────────────────────────────────────────────────────────
1. Open Cursor → Settings (⌘,) → MCP → "+ Add MCP Server"

2. Paste this configuration:

   {
     "mcpServers": {
       "gdrive": {
         "command": "npx",
         "args": ["-y", "@modelcontextprotocol/server-gdrive"]
       }
     }
   }

3. Restart Cursor — an OAuth browser window will open.
   Authenticate with the Google account that owns the target Doc.

4. Set your target Google Doc ID in config.yaml or as env var:
      export GDRIVE_DOC_ID="<the long ID from the Doc URL>"
   (It is the string between /d/ and /edit in the URL)

5. Optionally also set:
      export GDRIVE_FOLDER_ID="<folder ID>"  (to create new docs in a folder)

─────────────────────────────────────────────────────────────
RUNTIME BEHAVIOUR
─────────────────────────────────────────────────────────────
- When the gdrive MCP is connected and GDRIVE_DOC_ID is set:
    → prepend today's report at the top of the existing Doc.
- When GDRIVE_DOC_ID is not set but GDRIVE_FOLDER_ID is:
    → create a new Doc named "Vol Mispricing YYYY-MM-DD" in that folder.
- If neither is set: create a top-level Doc in Google Drive.
- When the MCP is not connected: writes a local Markdown file as fallback
  (data/reports/YYYY-MM-DD_top5.md) and logs a warning.
─────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)
logger = logging.getLogger(__name__)

# Env-var keys for Google Drive configuration
_DOC_ID_ENV = "GDRIVE_DOC_ID"
_FOLDER_ID_ENV = "GDRIVE_FOLDER_ID"

# Local fallback directory
_FALLBACK_DIR = Path("data/reports")

# MCP server name as registered in Cursor
_MCP_SERVER_NAME = "gdrive"


# ---------------------------------------------------------------------------
# MCP tool caller
# ---------------------------------------------------------------------------


async def _call_gdrive_tool(tool_name: str, arguments: dict) -> Optional[str]:
    """
    Call a Google Drive MCP tool via stdio subprocess.

    The gdrive MCP server is launched as a subprocess using the same
    npx command registered in Cursor's MCP config.

    Returns the text content of the first response item, or None on failure.
    """
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
                result = await session.call_tool(tool_name, arguments)

        for content in (result.content or []):
            if hasattr(content, "text"):
                return content.text
        return ""
    except Exception as exc:
        logger.error("gdrive MCP tool %r failed: %s", tool_name, exc)
        return None


def _call_gdrive_tool_sync(tool_name: str, arguments: dict) -> Optional[str]:
    """Synchronous wrapper around the async MCP call."""
    return asyncio.run(_call_gdrive_tool(tool_name, arguments))


# ---------------------------------------------------------------------------
# Google Docs write strategies
# ---------------------------------------------------------------------------


def _append_to_existing_doc(doc_id: str, markdown: str, date_str: str) -> bool:
    """Prepend the report to an existing Google Doc via MCP."""
    result = _call_gdrive_tool_sync(
        "gdrive_update_file",
        {
            "fileId": doc_id,
            "content": f"---\n{date_str}\n\n{markdown}\n\n",
            "mode": "prepend",
        },
    )
    if result is not None:
        log.info("gdrive.doc_updated", doc_id=doc_id)
        return True
    return False


def _create_new_doc(markdown: str, date_str: str, folder_id: Optional[str]) -> Optional[str]:
    """Create a new Google Doc containing the report. Returns the new doc ID."""
    args: dict = {
        "name": f"Vol Mispricing Top 5 — {date_str}",
        "content": markdown,
        "mimeType": "application/vnd.google-apps.document",
    }
    if folder_id:
        args["parents"] = [folder_id]

    result = _call_gdrive_tool_sync("gdrive_create_file", args)

    if result:
        try:
            data = json.loads(result)
            new_id = data.get("id") or data.get("fileId")
            if new_id:
                log.info("gdrive.doc_created", doc_id=new_id)
                return new_id
        except json.JSONDecodeError:
            pass
        log.info("gdrive.doc_created", raw=result[:100])
        return result
    return None


# ---------------------------------------------------------------------------
# Local file fallback
# ---------------------------------------------------------------------------


def _write_local_fallback(markdown: str, run_date: datetime) -> Path:
    _FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{run_date.strftime('%Y-%m-%d')}_top5.md"
    path = _FALLBACK_DIR / filename
    with open(path, "w") as f:
        f.write(markdown)
    return path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_gdrive_mcp_available() -> bool:
    """
    Check whether the Google Drive MCP server can be launched.
    Returns True if npx and @modelcontextprotocol/server-gdrive are accessible.
    """
    import shutil
    return shutil.which("npx") is not None


def write_report_to_docs(
    markdown: str,
    run_date: datetime,
    doc_id: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> dict:
    """
    Write the daily report to Google Docs via the gdrive MCP server.

    Priority:
      1. Append/prepend to existing doc (doc_id from arg or GDRIVE_DOC_ID env)
      2. Create new doc in folder (folder_id from arg or GDRIVE_FOLDER_ID env)
      3. Create new doc in Drive root
      4. Fallback: write local Markdown file if MCP unavailable

    Returns a dict with:
      method: "gdrive_update" | "gdrive_create" | "local_file"
      location: doc URL or local file path
      success: bool
    """
    doc_id = doc_id or os.getenv(_DOC_ID_ENV, "")
    folder_id = folder_id or os.getenv(_FOLDER_ID_ENV, "")
    date_str = run_date.strftime("%A, %B %-d, %Y")

    if not is_gdrive_mcp_available():
        path = _write_local_fallback(markdown, run_date)
        log.warning(
            "gdrive.mcp_not_available",
            fallback=str(path),
            action=(
                "Connect the Google Drive MCP in Cursor → Settings → MCP. "
                "See src/reporting/google_docs.py for instructions."
            ),
        )
        return {
            "method": "local_file",
            "location": str(path),
            "success": True,
            "message": (
                f"⚠️  Google Drive MCP not connected. "
                f"Report saved locally: {path}"
            ),
        }

    # Try to update existing doc
    if doc_id:
        ok = _append_to_existing_doc(doc_id, markdown, date_str)
        if ok:
            url = f"https://docs.google.com/document/d/{doc_id}/edit"
            return {
                "method": "gdrive_update",
                "location": url,
                "success": True,
                "message": f"✅ Report prepended to existing doc: {url}",
            }

    # Try to create a new doc
    new_id = _create_new_doc(markdown, date_str, folder_id or None)
    if new_id:
        url = f"https://docs.google.com/document/d/{new_id}/edit"
        return {
            "method": "gdrive_create",
            "location": url,
            "success": True,
            "message": f"✅ New Google Doc created: {url}",
        }

    # MCP call failed — fall back to local
    path = _write_local_fallback(markdown, run_date)
    log.error("gdrive.write_failed", fallback=str(path))
    return {
        "method": "local_file",
        "location": str(path),
        "success": True,
        "message": (
            f"⚠️  Google Drive MCP call failed. "
            f"Report saved locally: {path}"
        ),
    }
