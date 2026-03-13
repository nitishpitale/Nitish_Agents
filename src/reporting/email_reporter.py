"""
Email reporter — sends the daily top-5 report via Gmail SMTP.

Uses a Gmail App Password which NEVER expires (until you revoke it).
No OAuth flow, no token refresh, no Google Cloud project needed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP (2 minutes)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Turn on 2-Step Verification in your Google Account (if not already):
   https://myaccount.google.com/security

2. Create a Gmail App Password:
   https://myaccount.google.com/apppasswords
   → Select App: "Mail"  |  Device: "Other" → name it "Vol Engine"
   → Copy the 16-character password shown (e.g.  abcd efgh ijkl mnop)

3. Add to Cursor Secrets (Dashboard → Cloud Agents → Secrets):
     GMAIL_APP_PASSWORD   = abcdefghijklmnop    (no spaces)
     GMAIL_FROM           = you@gmail.com
     GMAIL_TO             = you@gmail.com        (or any address)

That's it. The password never expires. The daily 7:30 AM PST report
will arrive in your inbox every weekday morning.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import os
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import structlog

log = structlog.get_logger(__name__)
logger = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587


# ---------------------------------------------------------------------------
# HTML conversion — turns Markdown into a clean HTML email body
# ---------------------------------------------------------------------------

def _markdown_to_html(markdown: str) -> str:
    """Convert the report Markdown to a clean HTML email."""
    import re

    lines = markdown.split("\n")
    html_lines = ["<html><body style='font-family:Arial,sans-serif;max-width:700px;margin:0 auto;color:#222'>"]

    for line in lines:
        line = line.rstrip()

        if line.startswith("# "):
            html_lines.append(
                f"<h1 style='color:#1a1a2e;border-bottom:3px solid #4CAF50;padding-bottom:8px'>{line[2:]}</h1>"
            )
        elif line.startswith("## "):
            html_lines.append(f"<h2 style='color:#2c3e50;margin-top:24px'>{line[3:]}</h2>")
        elif line.startswith("### "):
            text = line[4:]
            # Colour stars
            text = text.replace("★", "<span style='color:#f39c12'>★</span>")
            text = text.replace("☆", "<span style='color:#ccc'>☆</span>")
            html_lines.append(
                f"<h3 style='color:#1a5276;background:#eaf2ff;padding:8px 12px;"
                f"border-left:4px solid #2980b9;margin-top:24px'>{text}</h3>"
            )
        elif line.startswith("| ") and "---" not in line:
            if "| # |" in line or "| Rank" in line.lower() or "| #" in line:
                # Table header
                cells = [c.strip() for c in line.split("|")[1:-1]]
                row = "".join(
                    f"<th style='background:#2c3e50;color:white;padding:8px 12px;text-align:left'>{c}</th>"
                    for c in cells
                )
                html_lines.append("<table style='width:100%;border-collapse:collapse;margin:12px 0'>")
                html_lines.append(f"<tr>{row}</tr>")
            elif "|---|" not in line:
                cells = [c.strip() for c in line.split("|")[1:-1]]
                row = "".join(
                    f"<td style='padding:8px 12px;border-bottom:1px solid #eee'>{_inline_md(c)}</td>"
                    for c in cells
                )
                html_lines.append(f"<tr>{row}</tr>")
        elif line == "---" or line.startswith("═") or line.startswith("─"):
            html_lines.append("<hr style='border:none;border-top:1px solid #ddd;margin:16px 0'>")
        elif line.startswith("- "):
            html_lines.append(f"<li style='margin:4px 0'>{_inline_md(line[2:])}</li>")
        elif line.startswith("> "):
            html_lines.append(
                f"<blockquote style='border-left:3px solid #4CAF50;padding:8px 16px;"
                f"background:#f9f9f9;margin:8px 0;color:#555'>{_inline_md(line[2:])}</blockquote>"
            )
        elif line.startswith("**") and line.endswith("**"):
            html_lines.append(f"<p><strong>{line[2:-2]}</strong></p>")
        elif line == "":
            html_lines.append("<br>")
        elif line.startswith("_") and line.endswith("_"):
            html_lines.append(f"<p style='color:#888;font-size:12px'><em>{line[1:-1]}</em></p>")
        else:
            html_lines.append(f"<p style='margin:4px 0'>{_inline_md(line)}</p>")

    html_lines.append("</body></html>")
    return "\n".join(html_lines)


def _inline_md(text: str) -> str:
    """Convert inline Markdown (bold, code, stars) to HTML."""
    import re
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code style='background:#f4f4f4;padding:2px 5px;border-radius:3px'>\1</code>", text)
    text = text.replace("★", "<span style='color:#f39c12'>★</span>")
    text = text.replace("☆", "<span style='color:#ccc'>☆</span>")
    text = text.replace("📞 CALL", "📞 <span style='color:#27ae60'><strong>CALL</strong></span>")
    text = text.replace("📉 PUT", "📉 <span style='color:#c0392b'><strong>PUT</strong></span>")
    text = text.replace("⚠️", "⚠️")
    return text


# ---------------------------------------------------------------------------
# Send email
# ---------------------------------------------------------------------------

def send_daily_report(
    markdown: str,
    plain_text: str,
    run_date: datetime,
    gmail_from: Optional[str] = None,
    gmail_to: Optional[str] = None,
    app_password: Optional[str] = None,
) -> dict:
    """
    Send the daily report via Gmail SMTP using an App Password.

    Args:
        markdown: Full Markdown report (converted to HTML for email body).
        plain_text: Plain-text fallback.
        run_date: The run datetime (used in subject line).
        gmail_from: Sender Gmail address (or GMAIL_FROM env var).
        gmail_to: Recipient address (or GMAIL_TO env var).
        app_password: Gmail App Password (or GMAIL_APP_PASSWORD env var).

    Returns:
        dict with success, method, location (recipient), message.
    """
    gmail_from = gmail_from or os.getenv("GMAIL_FROM", "")
    gmail_to = gmail_to or os.getenv("GMAIL_TO", gmail_from)
    app_password = app_password or os.getenv("GMAIL_APP_PASSWORD", "")

    if not gmail_from or not app_password:
        return {
            "method": "email_skipped",
            "location": "",
            "success": False,
            "message": (
                "⚠️  Email not configured. Set GMAIL_FROM and GMAIL_APP_PASSWORD in Cursor Secrets.\n"
                "    See src/reporting/email_reporter.py for 2-minute setup instructions."
            ),
        }

    date_str = run_date.strftime("%A %b %-d, %Y")
    subject = f"📈 Vol Mispricing Top 5 — {date_str}"

    # Build MIME message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Vol Engine <{gmail_from}>"
    msg["To"] = gmail_to

    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(_markdown_to_html(markdown), "html"))

    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(gmail_from, app_password)
            smtp.sendmail(gmail_from, gmail_to.split(","), msg.as_string())

        log.info("email.sent", to=gmail_to, subject=subject)
        return {
            "method": "email",
            "location": gmail_to,
            "success": True,
            "message": f"✅  Report emailed to {gmail_to}",
        }

    except smtplib.SMTPAuthenticationError:
        msg_text = (
            "❌  Gmail authentication failed.\n"
            "    Make sure GMAIL_APP_PASSWORD is a 16-char App Password\n"
            "    (not your regular Gmail password).\n"
            "    Get one at: https://myaccount.google.com/apppasswords"
        )
        logger.error("email.auth_failed")
        return {"method": "email", "location": gmail_to, "success": False, "message": msg_text}

    except Exception as exc:
        logger.error("email.send_failed: %s", exc)
        return {
            "method": "email",
            "location": gmail_to,
            "success": False,
            "message": f"❌  Email failed: {exc}",
        }


def is_email_configured() -> bool:
    return bool(os.getenv("GMAIL_FROM") and os.getenv("GMAIL_APP_PASSWORD"))
