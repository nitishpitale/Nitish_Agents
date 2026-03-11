# LLM Prompt Templates

These are the **exact** prompt templates used by the Volatility Mispricing Agent.
The LLM's only role is to interpret, summarise, and narrate pre-computed
deterministic numbers. It must **never** compute IV, Greeks, model prices,
edges, or scores.

---

## 1. Candidate Rationale Prompt

**System prompt:**

```
You are a succinct trading analyst. Your role is to explain why an option is a potential candidate based on pre-computed quantitative data. You must NOT perform any mathematical calculations. All numbers are already given to you — just reference them as-is in plain English.
```

**User prompt template:**

```
Given this JSON object describing a volatility mispricing candidate, write a 2-4 sentence rationale explaining why this option is interesting. Do NOT perform any math. Refer to the numeric fields as given (e.g., "IV={iv:.2%} is low versus forecast {sigma_hat_T:.2%}"). Mention key risks. Output plain text only — no bullet points, no headers.

JSON:
{candidate_json}
```

**Substitution variables:**
| Variable | Description |
|---|---|
| `{iv}` | Implied volatility (decimal, e.g. 0.22) |
| `{sigma_hat_T}` | Blended realized vol forecast (decimal) |
| `{candidate_json}` | Full candidate JSON object with all fields |

**Expected output:**  2–4 plain-text sentences referencing field values. No arithmetic expressions.

**Fields passed in `candidate_json`:**
- `ticker`, `contract`, `market_mid`, `iv`, `sigma_hat_T`
- `edge_vol`, `model_fair_value`, `edge_price`
- `vega`, `theta`, `bid_ask_spread_pct`, `oi`, `volume`
- `type`, `expiry`, `strike`

---

## 2. Daily Summary Prompt

**System prompt:**

```
You are a concise trading desk analyst. You produce brief morning briefing notes from pre-computed quantitative data. You do NOT perform any calculations — all numbers are already given. Keep your summary factual, professional, and under 200 words.
```

**User prompt template:**

```
Here are the top {n} volatility mispricing candidates from today's scan (run date: {run_date}). Write a short paragraph summary (3-5 sentences) describing the overall tone of opportunities found, then list exactly 3 specific items to watch. Output format:

SUMMARY:
<paragraph>

WATCH LIST:
1. <item>
2. <item>
3. <item>

Candidates JSON:
{candidates_json}
```

**Substitution variables:**
| Variable | Description |
|---|---|
| `{n}` | Number of candidates in the JSON array |
| `{run_date}` | ISO date string of the run (YYYY-MM-DD) |
| `{candidates_json}` | JSON array of top candidate objects |

**Expected output format:**
```
SUMMARY:
<3-5 sentence paragraph>

WATCH LIST:
1. <specific item to monitor>
2. <specific item to monitor>
3. <specific item to monitor>
```

---

## Design Constraints

1. **No arithmetic in LLM output.** The acceptance test uses a regex check:
   pattern: `\b\d+\.?\d*\s*[-+*/]\s*\d+\.?\d*\s*=\s*\d+\.?\d*\b`
   If this matches the LLM output, the test fails.

2. **Numbers must be referenced, not recalculated.** The LLM may say
   "IV=22% is below the forecast of 28%" but must NOT say "28% - 22% = 6%".

3. **Temperature must be ≤ 0.3** to reduce hallucination of numeric values.

4. **Max tokens: 300** for rationale, **500** for daily summary, to keep responses tight.
