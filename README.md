# Sabarna · DBG Portfolio Dashboard

Live portfolio tracker + signal engine for US holdings.

## Files

| File | Purpose |
|---|---|
| `index.html` | Main dashboard — Overview, US Holdings, India, Transactions, **Signals** |
| `scripts/analyse_holdings.py` | Weekly signal scorer — runs via GitHub Actions, writes `stock_analysis.json` |
| `scripts/model_comparison.py` | Backtester — compares Buy&Hold vs Signal-Change vs REDUCE-Only strategies |
| `.github/workflows/analysis.yml` | Auto-runs every Monday 9am NY + manual trigger |
| `data/processed/stock_analysis.json` | Latest signal output (auto-updated by Actions) |

## Signal Model — 9 Components (0–100 score)

| Component | Points |
|---|---|
| Price vs 200MA | 12 |
| Golden/Death Cross (50/200) | 8 |
| Weekly trend vs 10-week MA | 10 |
| RSI-14 (EWM) | 12 |
| MACD vs Signal | 10 |
| Volume (up-day vs down-day) | 8 |
| Relative Strength vs SPY (60d) | 15 |
| 52-week range position | 8 |
| Valuation vs 1yr mean | 10 |

**BUY ≥ 65 · HOLD 40–64 · REDUCE < 40**  
Bear market regime (SPY < 200MA) applies ×0.75 multiplier.  
2-week stability filter required before acting on any signal.

## Three Strategies Tested

- **A — Buy & Hold**: enter once, never trade
- **B — Signal-Change**: exit every REDUCE, re-enter every BUY (2-wk confirm each)  
- **C — REDUCE-Only**: exit once on first stable REDUCE, stay out (no re-entry)

Run `python scripts/model_comparison.py` to test all three on your transaction history.

## Pre-Entry Screener

Open `index.html` → click **📡 Signals** tab → type any US ticker → press ANALYZE.

Gives: GO / WAIT / NO-GO · position size · stop loss · trim target · earnings warning.
