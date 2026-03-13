"""
Daily report formatter.

Converts a list of CandidateResult objects into structured text suitable
for Google Docs (Markdown-flavoured) and a plain console summary.

The report is deliberately human-readable — numbers already computed by
deterministic code, prose written by the LLM rationale generator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from ..scoring.scorer import CandidateResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def _sign(v: float) -> str:
    return f"+{v:.4f}" if v >= 0 else f"{v:.4f}"


def _stars(score: float) -> str:
    """Simple visual indicator: 1-5 stars based on score percentile."""
    if score >= 20:
        return "★★★★★"
    if score >= 10:
        return "★★★★☆"
    if score >= 3:
        return "★★★☆☆"
    if score >= 1:
        return "★★☆☆☆"
    return "★☆☆☆☆"


# ---------------------------------------------------------------------------
# Core formatter
# ---------------------------------------------------------------------------


def format_daily_report(
    candidates: List[CandidateResult],
    run_date: datetime,
    top_n: int = 5,
    daily_summary: str = "",
) -> dict[str, str]:
    """
    Produce multiple representations of the daily top-N report.

    Returns a dict with keys:
      google_docs_markdown  — for pasting / writing to Google Docs
      console_text          — for terminal / log output
      plain_text            — minimal plain text (email / SMS fallback)
    """
    top = candidates[:top_n]
    date_str = run_date.strftime("%A, %B %-d, %Y")
    time_str = run_date.strftime("%I:%M %p UTC")
    pst_label = "7:30 AM PST"

    return {
        "google_docs_markdown": _build_docs_markdown(top, date_str, pst_label, daily_summary),
        "console_text": _build_console(top, date_str, time_str, daily_summary),
        "plain_text": _build_plain(top, date_str),
    }


def _build_docs_markdown(
    candidates: List[CandidateResult],
    date_str: str,
    pst_label: str,
    daily_summary: str,
) -> str:
    lines: List[str] = []

    # ── Header ──────────────────────────────────────────────────────────────
    lines += [
        f"# 📈 Volatility Mispricing — Daily Top 5",
        f"**{date_str}  |  {pst_label}**",
        "",
        "---",
        "",
    ]

    # ── AI Summary ──────────────────────────────────────────────────────────
    if daily_summary:
        lines += [
            "## Market Summary",
            "",
            daily_summary.strip(),
            "",
            "---",
            "",
        ]

    # ── Top 5 table ─────────────────────────────────────────────────────────
    lines += [
        "## Top 5 Candidates",
        "",
        "| # | Contract | Type | IV | Vol Forecast | Edge Vol | Edge Price | Score | Rating |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in candidates:
        d = c.to_dict()
        score = d.get("score", 0)
        lines.append(
            f"| {d['rank']} "
            f"| `{d['contract']}` "
            f"| {'📞 CALL' if d['type'] == 'C' else '📉 PUT'} "
            f"| {_pct(d['iv'])} "
            f"| {_pct(d['sigma_hat_T'])} "
            f"| {_sign(d['edge_vol'])} "
            f"| {_sign(d['edge_price'])} "
            f"| {score:.3f} "
            f"| {_stars(score)} |"
        )

    lines += ["", "---", ""]

    # ── Individual candidate cards ───────────────────────────────────────────
    lines.append("## Candidate Details")
    lines.append("")

    for c in candidates:
        d = c.to_dict()
        score = d.get("score", 0)
        news = d.get("news", {})
        risk_list = d.get("risks", [])
        headlines = news.get("top_headlines", [])
        news_feat = news.get("features") or {}
        sentiment = (news_feat.get("sentiment") or {}).get("label", "neutral")

        lines += [
            f"### {d['rank']}. `{d['contract']}`  {_stars(score)}",
            "",
            f"**Ticker:** {d['ticker']}  "
            f"| **Type:** {'Call' if d['type'] == 'C' else 'Put'}  "
            f"| **Expiry:** {d['expiry']}  "
            f"| **Strike:** ${d['strike']:.2f}",
            "",
            "**Volatility:**",
            f"- Market IV: **{_pct(d['iv'])}**  →  Realized Vol Forecast: **{_pct(d['sigma_hat_T'])}**",
            f"- Edge Vol: **{_sign(d['edge_vol'])}**  (positive = market underpricing vol)",
            "",
            "**Pricing:**",
            f"- Market Mid: **${d['market_mid']:.2f}**  (bid ${d['bid']:.2f} / ask ${d['ask']:.2f})",
            f"- Model Fair Value: **${d['model_fair_value']:.2f}**",
            f"- Edge Price: **{_sign(d['edge_price'])}**",
            f"- Bid-Ask Spread: **{_pct(d['bid_ask_spread_pct'])}**",
            "",
            "**Greeks:**",
            f"- Delta: `{d['delta']:.4f}`  |  Vega: `{d['vega']:.4f}`  |  Theta: `{d['theta']:.4f}` /yr",
            "",
            "**Liquidity:**",
            f"- Open Interest: **{d['oi']:,}**  |  Volume: **{d['volume']:,}**",
            "",
            f"**Final Score:** `{score:.4f}`  |  **News Sentiment:** {sentiment.title()}  "
            f"|  **News Multiplier:** `{news.get('score_multiplier', 1.0):.3f}`",
            "",
        ]

        if d.get("reason_summary"):
            lines += [
                "**Analyst Note:**",
                f"> {d['reason_summary']}",
                "",
            ]

        if risk_list:
            lines += [
                "**⚠️ Risks:**",
                *[f"- {r}" for r in risk_list],
                "",
            ]

        if headlines:
            lines += [
                "**Recent Headlines:**",
                *[f"- {h}" for h in headlines[:3]],
                "",
            ]

        lines += ["---", ""]

    # ── Footer ───────────────────────────────────────────────────────────────
    lines += [
        "_Generated by Volatility Mispricing Engine — for informational purposes only._",
        "_Not financial advice. Always verify before trading._",
    ]

    return "\n".join(lines)


def _build_console(
    candidates: List[CandidateResult],
    date_str: str,
    time_str: str,
    daily_summary: str,
) -> str:
    sep = "═" * 80
    thin = "─" * 80
    lines = [
        sep,
        f"  VOLATILITY MISPRICING — DAILY TOP 5    {date_str}",
        sep,
    ]

    if daily_summary:
        # Extract the SUMMARY block if present
        summary_text = daily_summary
        if "SUMMARY:" in daily_summary:
            summary_text = daily_summary.split("SUMMARY:")[-1].split("WATCH LIST:")[0].strip()
        lines += ["", f"  {summary_text[:200]}", ""]

    for c in candidates:
        d = c.to_dict()
        score = d.get("score", 0)
        lines += [
            thin,
            f"  #{d['rank']}  {d['contract']:45s}  {_stars(score)}",
            thin,
            f"  {'CALL' if d['type']=='C' else 'PUT ':4s}  "
            f"Strike ${d['strike']:.2f}   Expiry {d['expiry']}",
            f"  IV {_pct(d['iv'])}  →  σ̂ {_pct(d['sigma_hat_T'])}  "
            f"  EdgeVol {_sign(d['edge_vol'])}   EdgePrice {_sign(d['edge_price'])}",
            f"  Mid ${d['market_mid']:.2f}   Fair ${d['model_fair_value']:.2f}   "
            f"Spread {_pct(d['bid_ask_spread_pct'])}   OI {d['oi']:,}",
            f"  Score {score:.4f}   Delta {d['delta']:.4f}   Vega {d['vega']:.4f}",
        ]
        if d.get("risks"):
            lines.append(f"  ⚠  {', '.join(d['risks'])}")
        if d.get("reason_summary"):
            # Wrap at 74 chars
            note = d["reason_summary"][:148]
            lines.append(f"  {note[:74]}")
            if len(note) > 74:
                lines.append(f"  {note[74:]}")
        lines.append("")

    lines += [sep, f"  Run at {time_str}  |  Not financial advice.", sep]
    return "\n".join(lines)


def _build_plain(candidates: List[CandidateResult], date_str: str) -> str:
    lines = [
        f"VOLATILITY MISPRICING — TOP 5  ({date_str})",
        "=" * 50,
    ]
    for c in candidates:
        d = c.to_dict()
        score = d.get("score", 0)
        lines.append(
            f"#{d['rank']} {d['contract']}  "
            f"IV={_pct(d['iv'])} σ̂={_pct(d['sigma_hat_T'])} "
            f"edge={_sign(d['edge_price'])} score={score:.3f}"
        )
    lines.append("")
    lines.append("Not financial advice.")
    return "\n".join(lines)
