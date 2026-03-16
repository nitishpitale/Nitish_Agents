"""
Free, permanent delivery channels — no token refresh ever needed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OPTION 1 — TELEGRAM BOT  (30 seconds, phone notification)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Open Telegram → search @BotFather → send /newbot
2. Choose a name (e.g. "My Vol Picks") and username (e.g. volpicks_bot)
3. Copy the bot token (looks like: 7412345678:AAFxxx...)
4. Start a chat with your new bot (search it, press Start)
5. Visit this URL to get your chat ID:
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   Look for "chat":{"id": <NUMBER>}
6. Add to GitHub Secrets (or Cursor Secrets):
     TELEGRAM_BOT_TOKEN = 7412345678:AAFxxx...
     TELEGRAM_CHAT_ID   = 123456789

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OPTION 2 — DISCORD WEBHOOK  (1 minute, rich formatted report)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Open Discord → your server → any channel → Edit Channel
2. Integrations → Webhooks → New Webhook → Copy Webhook URL
3. Add to GitHub Secrets:
     DISCORD_WEBHOOK_URL = https://discord.com/api/webhooks/...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OPTION 3 — GITHUB GIST  (2 minutes, versioned markdown file)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Go to github.com → Settings → Developer settings →
   Personal access tokens → Tokens (classic) → Generate new token
2. Select only the "gist" scope → Generate → Copy token
3. Add to GitHub Secrets:
     GITHUB_GIST_TOKEN = ghp_xxxxxxxxxxxx
     GITHUB_GIST_ID    = (optional — leave blank to auto-create on first run,
                          then add the returned ID to re-use the same Gist)
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

_TG_API = "https://api.telegram.org/bot{token}/{method}"
_TG_MAX = 4096  # Telegram message length limit


def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def _sign(v: float) -> str:
    return f"+{v:.2f}" if v >= 0 else f"{v:.2f}"


def _stars(score: float) -> str:
    if score >= 20: return "★★★★★"
    if score >= 10: return "★★★★☆"
    if score >= 3:  return "★★★☆☆"
    return "★★☆☆☆"


def _tg_format(candidates: list, run_date: datetime, daily_summary: str) -> str:
    """Format the top-5 as a Telegram HTML message."""
    date_str = run_date.strftime("%a %b %-d, %Y")
    lines = [
        f"<b>📈 Vol Mispricing Top 5 — {date_str}</b>\n",
    ]

    # Short summary (first 200 chars)
    if daily_summary:
        summary = daily_summary
        if "SUMMARY:" in summary:
            summary = summary.split("SUMMARY:")[-1].split("WATCH LIST:")[0].strip()
        lines.append(f"<i>{summary[:250]}</i>\n")

    lines.append("─" * 35)

    for c in candidates[:5]:
        d = c.to_dict()
        score = d.get("score", 0)
        opt = "📞 CALL" if d["type"] == "C" else "📉 PUT"
        news_feat = (d.get("news") or {}).get("features") or {}
        sentiment = (news_feat.get("sentiment") or {}).get("label", "neutral").capitalize()

        lines += [
            f"\n<b>#{d['rank']} {d['ticker']} {opt} {d['expiry']} @${d['strike']:.0f}</b>",
            f"  IV {_pct(d['iv'])} → σ̂ {_pct(d['sigma_hat_T'])}  EdgeVol {_sign(d['edge_vol'])}",
            f"  Mid ${d['market_mid']:.2f} → Fair ${d['model_fair_value']:.2f}  Edge {_sign(d['edge_price'])}",
            f"  OI {d['oi']:,}  Spread {_pct(d['bid_ask_spread_pct'])}",
            f"  Score <b>{score:.2f}</b> {_stars(score)}  {sentiment}",
        ]
        if d.get("risks"):
            lines.append(f"  ⚠️ {', '.join(d['risks'])}")

    lines += [
        "\n" + "─" * 35,
        "<i>Not financial advice.</i>",
    ]
    return "\n".join(lines)


def send_telegram(
    candidates: list,
    run_date: datetime,
    daily_summary: str = "",
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> dict:
    bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        return {
            "method": "telegram_skipped",
            "success": False,
            "message": (
                "⚠️  Telegram not configured.\n"
                "    Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to GitHub Secrets.\n"
                "    Setup takes 30 seconds — see src/reporting/free_delivery.py"
            ),
        }

    text = _tg_format(candidates, run_date, daily_summary)
    url = _TG_API.format(token=bot_token, method="sendMessage")

    # Send in chunks if over limit
    chunks = [text[i:i+_TG_MAX] for i in range(0, len(text), _TG_MAX)]
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            result = resp.json()
            if not result.get("ok"):
                raise ValueError(result.get("description", "Unknown error"))
        except Exception as exc:
            log.error("telegram.send_failed: %s", exc)
            return {
                "method": "telegram",
                "success": False,
                "message": f"❌  Telegram failed: {exc}",
            }

    chat_link = f"https://t.me/{chat_id}" if not str(chat_id).startswith("-") else "Telegram group"
    log.info("telegram.sent", chat_id=chat_id, chunks=len(chunks))
    return {
        "method": "telegram",
        "location": chat_link,
        "success": True,
        "message": f"✅  Report sent to Telegram (chat {chat_id})",
    }


def is_telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

_SCORE_COLOURS = {5: 0x27AE60, 4: 0x2ECC71, 3: 0xF39C12, 2: 0xE67E22, 1: 0xE74C3C}


def _discord_embeds(candidates: list, run_date: datetime, daily_summary: str) -> list:
    """Build Discord embed objects for the top-5 report."""
    date_str = run_date.strftime("%A, %B %-d, %Y")

    # Summary embed
    summary_text = ""
    if daily_summary:
        summary_text = daily_summary
        if "SUMMARY:" in summary_text:
            summary_text = summary_text.split("SUMMARY:")[-1].split("WATCH LIST:")[0].strip()

    embeds = [{
        "title": f"📈 Volatility Mispricing Top 5 — {date_str}",
        "description": summary_text[:2048] if summary_text else "Daily scan complete.",
        "color": 0x2C3E50,
        "footer": {"text": "Not financial advice · Powered by eToro live data"},
        "timestamp": run_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }]

    # One embed per candidate
    for c in candidates[:5]:
        d = c.to_dict()
        score = d.get("score", 0)
        star_count = 5 if score >= 20 else 4 if score >= 10 else 3 if score >= 3 else 2
        colour = _SCORE_COLOURS.get(star_count, 0x95A5A6)
        opt = "📞 CALL" if d["type"] == "C" else "📉 PUT"
        news_feat = (d.get("news") or {}).get("features") or {}
        sentiment = (news_feat.get("sentiment") or {}).get("label", "neutral").capitalize()

        fields = [
            {"name": "Type / Expiry", "value": f"{opt}\n{d['expiry']}", "inline": True},
            {"name": "Strike", "value": f"${d['strike']:.2f}", "inline": True},
            {"name": "Score", "value": f"{score:.2f}  {_stars(score)}\n{sentiment}", "inline": True},
            {"name": "Volatility", "value": (
                f"IV: **{_pct(d['iv'])}**\n"
                f"Forecast: **{_pct(d['sigma_hat_T'])}**\n"
                f"Edge Vol: **{_sign(d['edge_vol'])}**"
            ), "inline": True},
            {"name": "Pricing", "value": (
                f"Mid: ${d['market_mid']:.2f}\n"
                f"Fair: ${d['model_fair_value']:.2f}\n"
                f"Edge: **{_sign(d['edge_price'])}**"
            ), "inline": True},
            {"name": "Liquidity", "value": (
                f"OI: {d['oi']:,}\n"
                f"Vol: {d['volume']:,}\n"
                f"Spread: {_pct(d['bid_ask_spread_pct'])}"
            ), "inline": True},
        ]
        if d.get("risks"):
            fields.append({"name": "⚠️ Risks", "value": "\n".join(d["risks"]), "inline": False})

        embeds.append({
            "title": f"#{d['rank']} {d['ticker']} {opt} @${d['strike']:.2f}",
            "color": colour,
            "fields": fields,
        })

    return embeds


def send_discord(
    candidates: list,
    run_date: datetime,
    daily_summary: str = "",
    webhook_url: Optional[str] = None,
) -> dict:
    webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")

    if not webhook_url:
        return {
            "method": "discord_skipped",
            "success": False,
            "message": (
                "⚠️  Discord not configured.\n"
                "    Add DISCORD_WEBHOOK_URL to GitHub Secrets.\n"
                "    Setup takes 1 minute — see src/reporting/free_delivery.py"
            ),
        }

    embeds = _discord_embeds(candidates, run_date, daily_summary)

    # Discord allows max 10 embeds per message — send in batches
    try:
        for i in range(0, len(embeds), 10):
            batch = embeds[i:i + 10]
            resp = requests.post(
                webhook_url,
                json={"embeds": batch, "username": "Vol Mispricing Engine"},
                timeout=15,
            )
            if resp.status_code not in (200, 204):
                raise ValueError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        log.error("discord.send_failed: %s", exc)
        return {"method": "discord", "success": False, "message": f"❌  Discord failed: {exc}"}

    log.info("discord.sent", embeds=len(embeds))
    return {
        "method": "discord",
        "location": "Discord channel",
        "success": True,
        "message": "✅  Report sent to Discord channel",
    }


def is_discord_configured() -> bool:
    return bool(os.getenv("DISCORD_WEBHOOK_URL"))


# ---------------------------------------------------------------------------
# GitHub Gist
# ---------------------------------------------------------------------------

_GIST_API = "https://api.github.com/gists"


def write_github_gist(
    markdown: str,
    run_date: datetime,
    gist_token: Optional[str] = None,
    gist_id: Optional[str] = None,
) -> dict:
    """
    Create or update a GitHub Gist with the daily report Markdown.
    On first run (no gist_id) a new Gist is created; the returned ID
    should be stored as GITHUB_GIST_ID for future updates.
    """
    gist_token = gist_token or os.getenv("GITHUB_GIST_TOKEN", "")
    gist_id = gist_id or os.getenv("GITHUB_GIST_ID", "")

    if not gist_token:
        return {
            "method": "gist_skipped",
            "success": False,
            "message": (
                "⚠️  GitHub Gist not configured.\n"
                "    Add GITHUB_GIST_TOKEN to GitHub Secrets.\n"
                "    Setup takes 2 minutes — see src/reporting/free_delivery.py"
            ),
        }

    date_str = run_date.strftime("%Y-%m-%d")
    filename = "vol_mispricing_picks.md"
    headers = {
        "Authorization": f"Bearer {gist_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "description": f"Volatility Mispricing Top 5 — {date_str}",
        "public": False,
        "files": {filename: {"content": markdown}},
    }

    try:
        if gist_id:
            resp = requests.patch(f"{_GIST_API}/{gist_id}", json=payload, headers=headers, timeout=15)
        else:
            resp = requests.post(_GIST_API, json=payload, headers=headers, timeout=15)

        resp.raise_for_status()
        data = resp.json()
        gist_url = data.get("html_url", "")
        returned_id = data.get("id", "")

        log.info("gist.written", gist_id=returned_id, url=gist_url)
        msg = f"✅  Report written to GitHub Gist: {gist_url}"
        if not gist_id and returned_id:
            msg += f"\n    ℹ️  Save this Gist ID to reuse: GITHUB_GIST_ID={returned_id}"

        return {
            "method": "github_gist",
            "location": gist_url,
            "gist_id": returned_id,
            "success": True,
            "message": msg,
        }
    except Exception as exc:
        log.error("gist.write_failed: %s", exc)
        return {"method": "github_gist", "success": False, "message": f"❌  Gist failed: {exc}"}


def is_gist_configured() -> bool:
    return bool(os.getenv("GITHUB_GIST_TOKEN"))


# ---------------------------------------------------------------------------
# Dispatch all configured free channels
# ---------------------------------------------------------------------------

def dispatch_free_channels(
    candidates: list,
    run_date: datetime,
    markdown: str,
    daily_summary: str = "",
) -> list[dict]:
    """
    Send to all configured free delivery channels.
    Returns list of result dicts (one per attempted channel).
    """
    results = []

    if is_telegram_configured():
        results.append(send_telegram(candidates, run_date, daily_summary))

    if is_discord_configured():
        results.append(send_discord(candidates, run_date, daily_summary))

    if is_gist_configured():
        results.append(write_github_gist(markdown, run_date))

    return results
