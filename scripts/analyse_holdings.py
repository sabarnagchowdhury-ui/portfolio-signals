#!/usr/bin/env python3
"""
scripts/analyse_holdings.py
============================
Production signal engine — runs weekly via GitHub Actions.
Scores all US holdings using the 9-component model + market regime filter.
Writes output to data/processed/stock_analysis.json

Usage:
  python scripts/analyse_holdings.py

Requirements:
  pip install yfinance pandas numpy

Output (data/processed/stock_analysis.json):
  {
    "generated_at": "2026-05-13T12:00:00",
    "regime": { "call": "STRONG BULL", "spy_vs_200ma": 8.4, "vix_proxy": 16.2, ... },
    "holdings": [
      { "sym": "NVDA", "score": 91, "action": "BUY", "regime": "BULL",
        "rsi": 58, "v200": 12.4, "rs_vs_spy": 8.2, "rp52": 85,
        "stop_loss": 185.18, "trim_target": 140.00, "runner_target": 147.00,
        "components": { "vs200": 12, "cross": 8, ... },
        "warnings": [] },
      ...
    ],
    "screener_candidates": [],    // any extra tickers passed as CLI args
    "summary": { "buy": 5, "hold": 6, "reduce": 1 }
  }
"""
import json, warnings, sys, os
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance", "-q"])
    import yfinance as yf

# ── Current US holdings (keep in sync with portfolio-v4-may2026.html) ──
US_HOLDINGS = [
    {"sym": "GOOG",  "name": "Alphabet C",        "qty": 3,  "buy": 310.190},
    {"sym": "AMZN",  "name": "Amazon",             "qty": 10, "buy": 249.726},
    {"sym": "AVGO",  "name": "Broadcom",           "qty": 4,  "buy": 341.695},
    {"sym": "GLW",   "name": "Corning Inc.",        "qty": 6,  "buy": 182.170},
    {"sym": "GEV",   "name": "GE Vernova",          "qty": 1,  "buy": 1097.14},
    {"sym": "MU",    "name": "Micron Technology",   "qty": 1,  "buy": 492.000},
    {"sym": "MSFT",  "name": "Microsoft",           "qty": 6,  "buy": 416.065},
    {"sym": "MP",    "name": "MP Materials",        "qty": 10, "buy": 53.000 },
    {"sym": "RKLB",  "name": "Rocket Lab",          "qty": 15, "buy": 80.910 },
    {"sym": "TTE",   "name": "TotalEnergies ADR",   "qty": 8,  "buy": 82.840 },
    {"sym": "EWY",   "name": "iShares Korea ETF",   "qty": 11, "buy": 138.222},
    {"sym": "VOOG",  "name": "Vanguard Growth ETF", "qty": 18, "buy": 73.576 },
]

BENCHMARK = "SPY"
LOOKBACK  = "2y"     # ~2 years of daily data

# ── Signal engine ─────────────────────────────────────────────────────────────

def rsi(s: pd.Series, period: int = 14) -> float:
    if len(s) < period + 1:
        return 50.0
    d   = s.diff()
    g   = d.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    lo  = -d.clip(upper=0).ewm(com=period-1, min_periods=period).mean()
    rs  = g / lo.replace(0, np.nan)
    r   = 100 - 100 / (1 + rs)
    return float(r.iloc[-1]) if not np.isnan(r.iloc[-1]) else 50.0


def score_ticker(hist: pd.DataFrame, bench_hist: pd.DataFrame) -> dict:
    if hist is None or len(hist) < 30:
        return {"score": 50, "action": "HOLD", "components": {}, "rsi": 50,
                "v200": 0, "rs": 0, "regime": "BULL", "rp": 50, "vs1yr": 0,
                "ma200": 0, "ma50": 0, "hi52": 0, "lo52": 0, "warnings": []}

    c   = hist["Close"].squeeze()
    n   = len(c)
    px  = float(c.iloc[-1])
    sc  = {}

    # 1. Price vs 200MA (12 pts)
    ma200  = float(c.rolling(min(200, n)).mean().iloc[-1])
    ma50   = float(c.rolling(min(50,  n)).mean().iloc[-1])
    v200   = (px - ma200) / ma200 * 100
    sc["vs200"] = 12 if v200 > 5 else (6 if v200 > -5 else 0)

    # 2. Golden/Death cross (8 pts)
    sc["cross"] = 8 if ma50 > ma200 else 0

    # 3. Weekly 10wk MA (10 pts)
    wk    = c.resample("W").last().dropna()
    ma10w = float(wk.rolling(10).mean().iloc[-1]) if len(wk) >= 10 else px
    sc["weekly"] = 10 if float(wk.iloc[-1]) > ma10w else 0

    # 4. RSI-14 (12 pts)
    r_val = rsi(c, 14)
    sc["rsi"] = (12 if 40 <= r_val <= 65
                 else 10 if r_val < 30
                 else 2  if r_val > 75
                 else 6  if r_val < 40
                 else 8)

    # 5. MACD vs signal (10 pts)
    macd_line   = c.ewm(span=12).mean() - c.ewm(span=26).mean()
    macd_signal = macd_line.ewm(span=9).mean()
    sc["macd"] = 10 if float(macd_line.iloc[-1]) > float(macd_signal.iloc[-1]) else 0

    # 6. Volume trend — up-day vs down-day avg vol (8 pts)
    if len(hist) >= 20:
        up_vol = hist[hist["Close"] >= hist["Open"]]["Volume"].tail(20).mean()
        dn_vol = hist[hist["Close"] <  hist["Open"]]["Volume"].tail(20).mean()
        sc["vol"] = 8 if float(up_vol) > float(dn_vol) else 0
    else:
        sc["vol"] = 4

    # 7. Relative strength vs SPY 60-day (15 pts)
    rs_val = 0.0
    if bench_hist is not None and len(bench_hist) >= 60 and n >= 60:
        bc  = bench_hist["Close"].squeeze()
        sr  = float(c.iloc[-1]) / float(c.iloc[-60]) - 1
        br  = float(bc.iloc[-1]) / float(bc.iloc[-60]) - 1
        rs_val = sr - br
        sc["rs"] = 15 if rs_val > 0.05 else (8 if rs_val > -0.05 else 0)
    else:
        sc["rs"] = 7

    # 8. 52-week range position (8 pts)
    yr   = c.tail(min(252, n))
    hi52 = float(yr.max())
    lo52 = float(yr.min())
    rp   = (px - lo52) / (hi52 - lo52) if hi52 > lo52 else 0.5
    sc["range"] = 8 if rp > 0.7 else (5 if rp > 0.4 else 2)

    # 9. Valuation proxy — price vs 1yr mean (10 pts)
    mean1yr = float(yr.mean())
    vs1yr   = (px - mean1yr) / mean1yr * 100 if mean1yr > 0 else 0
    sc["valuation"] = 10 if vs1yr < 10 else (6 if vs1yr < 20 else 2)

    # Market regime (SPY vs 200MA)
    regime = "BULL"
    if bench_hist is not None:
        bc = bench_hist["Close"].squeeze()
        spy_ma200 = float(bc.rolling(min(200, len(bc))).mean().iloc[-1])
        regime    = "BULL" if float(bc.iloc[-1]) > spy_ma200 else "BEAR"

    total = sum(sc.values())
    if regime == "BEAR":
        total = int(total * 0.75)
    final_score = min(100, int(total / 93 * 100))
    action = "BUY" if final_score >= 65 else ("HOLD" if final_score >= 40 else "REDUCE")

    # Warnings
    warns = []
    if r_val > 73:
        warns.append(f"RSI {r_val:.0f} — overbought, consider waiting for pullback")
    if v200 > 30:
        warns.append(f"{v200:.1f}% above 200MA — overextended, wait for consolidation")
    if rp > 0.9:
        warns.append(f"At {rp*100:.0f}% of 52wk range — near highs, momentum risk")
    if action == "REDUCE":
        warns.append("REDUCE signal — position review recommended")

    # Stop loss & targets
    stop_loss = max(ma200, lo52 * 1.02)

    return {
        "score":       final_score,
        "action":      action,
        "regime":      regime,
        "components":  sc,
        "rsi":         round(r_val, 1),
        "v200":        round(v200, 1),
        "rs_vs_spy":   round(rs_val * 100, 1),
        "rp52":        round(rp * 100, 0),
        "vs1yr":       round(vs1yr, 1),
        "ma200":       round(ma200, 2),
        "ma50":        round(ma50, 2),
        "hi52":        round(hi52, 2),
        "lo52":        round(lo52, 2),
        "stop_loss":   round(stop_loss, 2),
        "trim_target": round(hi52, 2),
        "runner_target": round(hi52 * 1.05, 2),
        "warnings":    warns,
    }


def compute_regime(bench_hist: pd.DataFrame) -> dict:
    """Build market regime summary from SPY history."""
    bc  = bench_hist["Close"].squeeze()
    n   = len(bc)
    px  = float(bc.iloc[-1])

    ma200 = float(bc.rolling(min(200, n)).mean().iloc[-1])
    ma50  = float(bc.rolling(min(50,  n)).mean().iloc[-1])
    golden_cross = ma50 > ma200
    above_ma200  = px > ma200
    v200_pct     = (px - ma200) / ma200 * 100

    # Realised vol as VIX proxy (20-day annualised)
    log_rets = np.log(bc / bc.shift(1)).dropna().tail(20)
    vix_proxy = float(log_rets.std() * np.sqrt(252) * 100)

    if golden_cross and above_ma200 and vix_proxy < 22:
        call, verdict = "STRONG BULL", "✅ BUY DIPS"
    elif above_ma200:
        call, verdict = "BULL", "✅ OK TO BUY"
    elif not above_ma200 and vix_proxy > 30:
        call, verdict = "BEAR", "⛔ AVOID NEW POSITIONS"
    else:
        call, verdict = "CHOPPY", "⚠ SELECTIVE / WAIT"

    return {
        "call":           call,
        "verdict":        verdict,
        "spy_price":      round(px, 2),
        "spy_vs_200ma":   round(v200_pct, 1),
        "golden_cross":   golden_cross,
        "vix_proxy":      round(vix_proxy, 1),
    }


def download(tickers: list, period: str = "2y") -> dict:
    """Download history for multiple tickers in one batch call."""
    syms = list(set(tickers))
    print(f"  Downloading {len(syms)} tickers…", flush=True)
    try:
        raw = yf.download(syms, period=period, auto_adjust=True, progress=False, group_by="ticker")
    except Exception as e:
        print(f"  Batch download failed: {e}", flush=True)
        raw = None

    out = {}
    if raw is not None and not raw.empty:
        for sym in syms:
            try:
                if len(syms) == 1:
                    df = raw
                else:
                    df = raw[sym] if sym in raw.columns.get_level_values(0) else None
                if df is not None and not df.empty and "Close" in df.columns:
                    out[sym] = df.dropna(subset=["Close"])
            except Exception:
                pass

    # Fallback: individual downloads for any that failed
    missing = [s for s in syms if s not in out]
    for sym in missing:
        try:
            df = yf.download(sym, period=period, auto_adjust=True, progress=False)
            if not df.empty and "Close" in df.columns:
                out[sym] = df.dropna(subset=["Close"])
        except Exception as e:
            print(f"  ⚠ {sym}: {e}", flush=True)

    return out


def main():
    extra_tickers = [t.upper() for t in sys.argv[1:] if t.isalnum()]
    all_syms  = [h["sym"] for h in US_HOLDINGS] + [BENCHMARK] + extra_tickers

    print(f"[analyse_holdings] {datetime.now().strftime('%Y-%m-%d %H:%M')} · {len(US_HOLDINGS)} holdings + {len(extra_tickers)} screener tickers", flush=True)

    histories = download(all_syms)
    bench     = histories.get(BENCHMARK)

    if bench is None:
        print("  ⚠ Could not download SPY (benchmark). Proceeding without regime filter.", flush=True)

    # ── Regime ──
    regime_data = compute_regime(bench) if bench is not None else {"call": "UNKNOWN", "verdict": "N/A"}
    print(f"  Regime: {regime_data['call']} — {regime_data.get('verdict','')}", flush=True)

    # ── Score holdings ──
    holdings_out = []
    counts = {"BUY": 0, "HOLD": 0, "REDUCE": 0}
    for pos in US_HOLDINGS:
        sym  = pos["sym"]
        hist = histories.get(sym)
        res  = score_ticker(hist, bench)
        row  = {**pos, **res}
        holdings_out.append(row)
        counts[res["action"]] += 1
        icon = "▲" if res["action"]=="BUY" else ("■" if res["action"]=="HOLD" else "▼")
        warn_str = f" ⚠ {res['warnings'][0]}" if res["warnings"] else ""
        print(f"  {icon} {sym:6s} score={res['score']:3d} action={res['action']}{warn_str}", flush=True)

    # ── Score screener candidates ──
    screener_out = []
    for sym in extra_tickers:
        hist = histories.get(sym)
        res  = score_ticker(hist, bench)
        screener_out.append({"sym": sym, **res})
        icon = "▲" if res["action"]=="BUY" else ("■" if res["action"]=="HOLD" else "▼")
        print(f"  {icon} {sym:6s} [screener] score={res['score']:3d} action={res['action']}", flush=True)

    # ── Write output ──
    output = {
        "generated_at":        datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regime":              regime_data,
        "holdings":            holdings_out,
        "screener_candidates": screener_out,
        "summary":             counts,
    }

    # Resolve output dir: look for repo root (has .git) or fall back to cwd
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root  = script_dir
    for _ in range(3):  # walk up max 3 levels
        if os.path.isdir(os.path.join(repo_root, ".git")):
            break
        repo_root = os.path.dirname(repo_root)
    else:
        repo_root = os.getcwd()

    out_dir  = os.path.join(repo_root, "data", "processed")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "stock_analysis.json")

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n  ✓ Written to {out_path}", flush=True)
    print(f"  Summary: BUY={counts['BUY']} HOLD={counts['HOLD']} REDUCE={counts['REDUCE']}", flush=True)

    # Alerts
    reduces = [h["sym"] for h in holdings_out if h["action"] == "REDUCE"]
    if reduces:
        print(f"\n  🔴 REDUCE ALERT: {', '.join(reduces)}", flush=True)
        print("  Consider reviewing these positions!", flush=True)


if __name__ == "__main__":
    main()
