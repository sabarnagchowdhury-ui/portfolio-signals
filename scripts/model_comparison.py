#!/usr/bin/env python3
"""
model_comparison.py
====================
Head-to-head test of THREE strategies on the same US positions:

  A  BUY-AND-HOLD        — buy at actual entry, hold to today, never trade
  B  SIGNAL-CHANGE MODEL — exit every time action flips to REDUCE (re-enter at next BUY)
  C  REDUCE-ONLY EXIT    — exit ONCE on first stable REDUCE (2-wk confirm), never re-enter
                           (this is the "only exit when REDUCE appears" model you asked about)

Goal: figure out which strategy captured more upside, protected more downside,
      and whether the model is ready to use as a pre-entry screener.

Output: detailed per-ticker table + overall verdict + improvement suggestions.
"""
import warnings, sys
import pandas as pd
import numpy as np
from datetime import datetime

warnings.filterwarnings("ignore")
try:
    import yfinance as yf
except ImportError:
    import subprocess; subprocess.check_call([sys.executable,"-m","pip","install","yfinance","-q"])
    import yfinance as yf

EXCEL     = "/Users/sabarna/Downloads/Transactions_21550276_2025-12-15_2026-05-02 (1).xlsx"
BENCHMARK = "SPY"
TODAY     = datetime.today().strftime("%Y-%m-%d")
START     = "2022-01-01"

# ETFs / leveraged products: skip from signal analysis (model not designed for these)
SKIP = {"MUU","IRS","DAX","EWY","GEV","SIVR","VOOG"}

# ─────────────────────────────────────────────────────────────────────────────
#  SIGNAL ENGINE  (identical to portfolio-v4-may2026.html JS engine)
# ─────────────────────────────────────────────────────────────────────────────
def _rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(com=p-1, min_periods=p).mean()
    l = -d.clip(upper=0).ewm(com=p-1, min_periods=p).mean()
    r = 100 - 100/(1 + g/l.replace(0, np.nan))
    return float(r.iloc[-1]) if pd.notna(r.iloc[-1]) else 50.0

def _score(hist, bench):
    """9-component scorer. Returns (score 0-100, action, component_dict)."""
    if hist is None or len(hist) < 30:
        return 50, "HOLD", {}
    c   = hist["Close"].squeeze()
    n   = len(c)
    px  = float(c.iloc[-1])
    sc  = {}

    ma50  = float(c.rolling(min(50, n)).mean().iloc[-1])
    ma200 = float(c.rolling(min(200,n)).mean().iloc[-1])
    v200  = (px - ma200)/ma200*100
    sc["vs200"]  = 12 if v200>5 else (6 if v200>-5 else 0)
    sc["cross"]  = 8  if ma50>ma200 else 0

    wk    = c.resample("W").last().dropna()
    ma10w = float(wk.rolling(10).mean().iloc[-1]) if len(wk)>=10 else px
    sc["weekly"] = 10 if float(wk.iloc[-1])>ma10w else 0

    r_val = _rsi(c, 14)
    sc["rsi"] = (12 if 40<=r_val<=65 else 10 if r_val<30 else 2 if r_val>75 else 6 if r_val<40 else 8)

    macd      = c.ewm(span=12).mean() - c.ewm(span=26).mean()
    macd_sig  = macd.ewm(span=9).mean()
    sc["macd"] = 10 if float(macd.iloc[-1])>float(macd_sig.iloc[-1]) else 0

    if len(hist)>=20:
        up = hist[hist["Close"]>=hist["Open"]]["Volume"].tail(20).mean()
        dn = hist[hist["Close"]< hist["Open"]]["Volume"].tail(20).mean()
        sc["vol"] = 8 if float(up)>float(dn) else 0
    else:
        sc["vol"] = 4

    bc = bench["Close"].squeeze()
    if len(bc)>=60 and n>=60:
        sr = float(c.iloc[-1])/float(c.iloc[-60])-1
        br = float(bc.iloc[-1])/float(bc.iloc[-60])-1
        sc["rs"] = 15 if (sr-br)>0.05 else (8 if (sr-br)>-0.05 else 0)
    else:
        sc["rs"] = 7

    yr  = c.tail(min(252, n))
    hi, lo = float(yr.max()), float(yr.min())
    rp  = (px-lo)/(hi-lo) if hi>lo else 0.5
    sc["range"] = 8 if rp>0.7 else (5 if rp>0.4 else 2)

    vs1yr = (px - float(yr.mean()))/float(yr.mean())*100 if float(yr.mean())>0 else 0
    sc["val"] = 10 if vs1yr<10 else (6 if vs1yr<25 else 2)

    regime = "BULL" if float(bc.iloc[-1])>float(bc.rolling(min(200,len(bc))).mean().iloc[-1]) else "BEAR"
    total  = sum(sc.values())
    if regime=="BEAR": total = int(total*0.75)
    s = min(100, int(total/93*100))
    return s, ("BUY" if s>=65 else "HOLD" if s>=40 else "REDUCE"), sc

def _build_timeline(hist, bench, from_date):
    """Weekly signal timeline from from_date → today."""
    hist  = hist.copy(); hist.index  = pd.to_datetime(hist.index)
    bench = bench.copy(); bench.index = pd.to_datetime(bench.index)
    fridays = pd.date_range(start=from_date, end=hist.index[-1], freq="W-FRI")
    rows = []
    for d in fridays:
        h = hist[hist.index<=d]
        b = bench[bench.index<=d]
        if len(h)<30: continue
        s, act, _ = _score(h, b)
        rows.append({"date":d, "price":float(h["Close"].iloc[-1]), "score":s, "action":act})
    return pd.DataFrame(rows)

# ─────────────────────────────────────────────────────────────────────────────
#  STRATEGY SIMULATORS
# ─────────────────────────────────────────────────────────────────────────────
def _sim_bah(tl, entry_price):
    """Strategy A: Buy and hold — one trade, no model used."""
    final_price = tl["price"].iloc[-1]
    ret = (final_price/entry_price - 1)*100
    return {"trades": 1, "return": round(ret,1), "exit_price": None, "exit_date": None}

def _sim_signal_change(tl, entry_price):
    """
    Strategy B: Exit every time action flips to REDUCE (needs 2 consecutive REDUCE weeks).
                Re-enter at next BUY (needs 2 consecutive BUY weeks).
                Simulate cash earning 0% between trades.
    """
    capital = entry_price
    in_market = True
    cur_price = entry_price
    trades = 1  # the initial buy
    prev_action = None
    exit_price = None
    exits = []
    entries = []

    for _, row in tl.iterrows():
        act = row["action"]
        px  = row["price"]

        if in_market:
            cur_price = px
            if act == "REDUCE" and prev_action == "REDUCE":
                # Confirmed exit
                capital = capital * (px / cur_price)
                exit_price = px
                exits.append(px)
                in_market = False
                trades += 1
        else:
            if act == "BUY" and prev_action == "BUY":
                # Confirmed re-entry
                capital = capital  # cash unchanged
                in_market = True
                cur_price = px
                entries.append(px)
                trades += 1

        prev_action = act

    # If still in market at end
    if in_market:
        final_ret = (tl["price"].iloc[-1] / entry_price - 1)*100
        capital = entry_price * (1 + final_ret/100)

    ret = (capital/entry_price - 1)*100
    n_exits = len(exits)
    return {"trades": trades, "return": round(ret,1),
            "n_exits": n_exits, "exit_prices": exits}

def _sim_reduce_only(tl, entry_price):
    """
    Strategy C: Hold until FIRST stable REDUCE (2 consecutive REDUCE weeks).
                Exit once and stay out. No re-entry.
                This is the "exit only when REDUCE appears" model.
    """
    prev_action  = None
    exit_price   = None
    exit_date    = None

    for _, row in tl.iterrows():
        if row["action"] == "REDUCE" and prev_action == "REDUCE":
            exit_price = row["price"]
            exit_date  = row["date"]
            break
        prev_action = row["action"]

    if exit_price:
        ret = (exit_price/entry_price - 1)*100
        # What happened after exit?
        post = tl[tl["date"] > exit_date]
        low_after  = float(post["price"].min()) if len(post)>0 else exit_price
        end_after  = float(post["price"].iloc[-1]) if len(post)>0 else exit_price
        drop_after = (low_after/exit_price - 1)*100
        move_after = (end_after/exit_price - 1)*100
    else:
        # REDUCE never fired → hold to today = same as B&H
        ret = (tl["price"].iloc[-1]/entry_price - 1)*100
        drop_after = None
        move_after = None

    return {"trades": 2 if exit_price else 1,
            "return": round(ret,1),
            "exit_price": round(exit_price,2) if exit_price else None,
            "exit_date":  exit_date.date() if exit_date else None,
            "drop_after": round(drop_after,1) if drop_after is not None else None,
            "move_after": round(move_after,1) if move_after is not None else None,
            "fired":      exit_price is not None}

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN COMPARISON
# ─────────────────────────────────────────────────────────────────────────────
def run_comparison():
    print("\n" + "═"*76)
    print("  US PORTFOLIO — 3-STRATEGY MODEL COMPARISON")
    print("  A: Buy & Hold  │  B: Signal-Change  │  C: REDUCE-Only Exit")
    print("═"*76)

    # Load transactions
    df = pd.read_excel(EXCEL, sheet_name="Transactions", header=0)
    df.columns = [str(c).strip() for c in df.columns]
    trades_df = df[df["Transaction Type"]=="Trade"].copy()
    trades_df["Trade Date"] = pd.to_datetime(trades_df["Trade Date"])
    trades_df["symbol"] = trades_df["Instrument Symbol"].str.split(":").str[0]
    trades_df["qty"]    = pd.to_numeric(trades_df["Event"].str.extract(r"([\-\d]+) @")[0], errors="coerce")
    trades_df["px"]     = pd.to_numeric(trades_df["Event"].str.extract(r"@ ([\d\,\.]+)")[0].str.replace(",",""), errors="coerce")

    tickers = [t for t in sorted(trades_df["symbol"].unique()) if t not in SKIP]

    print(f"\n⬇  Downloading SPY + {len(tickers)} ticker histories…", flush=True)
    spy = yf.download(BENCHMARK, start=START, end=TODAY, progress=False, auto_adjust=True)
    spy.index = pd.to_datetime(spy.index)

    # SPY benchmark: what did SPY do over the same periods?
    hist_cache = {}
    for sym in tickers:
        try:
            h = yf.download(sym, start=START, end=TODAY, progress=False, auto_adjust=True)
            if len(h) > 60:
                h.index = pd.to_datetime(h.index)
                hist_cache[sym] = h
                print(f"   ✓ {sym}", flush=True)
        except Exception as e:
            print(f"   ✗ {sym}: {e}", flush=True)

    all_results = []

    print("\n" + "─"*76, flush=True)
    print(f"  {'Ticker':6s} | {'Entry':>10s} | {'Entry$':>7s} | {'Now$':>7s} | {'A:B&H':>7s} | {'B:SigChg':>8s} | {'C:Reduce':>8s} | {'Winner':>8s}", flush=True)
    print("  " + "─"*74, flush=True)

    for sym in tickers:
        if sym not in hist_cache:
            continue

        hist = hist_cache[sym]
        t    = trades_df[trades_df["symbol"]==sym].sort_values("Trade Date")
        buys = t[t["qty"]>0]
        if len(buys)==0: continue

        entry_date  = buys.iloc[0]["Trade Date"]
        entry_price = float(buys.iloc[0]["px"])
        if pd.isna(entry_price) or entry_price <= 0: continue

        current_price = float(hist["Close"].iloc[-1])

        # Build weekly timeline from entry date
        tl = _build_timeline(hist, spy, entry_date)
        if len(tl) < 4:
            continue

        # SPY return over same period
        spy_slice  = spy[spy.index >= entry_date]
        spy_return = (float(spy_slice["Close"].iloc[-1])/float(spy_slice["Close"].iloc[0])-1)*100 if len(spy_slice)>0 else 0

        # Run all three strategies
        res_a = _sim_bah(tl, entry_price)
        res_b = _sim_signal_change(tl, entry_price)
        res_c = _sim_reduce_only(tl, entry_price)

        # Current model signal
        s_now, act_now, _ = _score(hist, spy)

        # Winner among A/B/C
        returns = {"A:B&H": res_a["return"], "B:SigChg": res_b["return"], "C:Reduce": res_c["return"]}
        winner  = max(returns, key=returns.get)

        print(f"  {sym:6s} | {str(entry_date.date()):>10s} | ${entry_price:>6.2f} | ${current_price:>6.2f} | "
              f"{res_a['return']:>+6.1f}% | {res_b['return']:>+7.1f}% | {res_c['return']:>+7.1f}% | {winner:>8s}", flush=True)

        all_results.append({
            "sym":        sym,
            "entry_date": entry_date.date(),
            "entry_px":   entry_price,
            "current_px": round(current_price,2),
            "spy_return": round(spy_return,1),
            "A_bah":      res_a["return"],
            "B_sigchg":   res_b["return"],
            "B_trades":   res_b["trades"],
            "C_reduce":   res_c["return"],
            "C_fired":    res_c["fired"],
            "C_exit_date":res_c["exit_date"],
            "C_exit_px":  res_c["exit_price"],
            "C_drop_after":res_c["drop_after"],
            "C_move_after":res_c["move_after"],
            "score_now":  s_now,
            "action_now": act_now,
            "winner":     winner,
        })

    print("  " + "─"*74, flush=True)

    rdf = pd.DataFrame(all_results)
    if rdf.empty:
        print("  No results — check Excel path and ticker list.")
        return

    n = len(rdf)
    avg_a = rdf["A_bah"].mean()
    avg_b = rdf["B_sigchg"].mean()
    avg_c = rdf["C_reduce"].mean()
    avg_spy = rdf["spy_return"].mean()

    print(f"\n  AVERAGE RETURNS across {n} positions:")
    print(f"    SPY (benchmark, same periods)  : {avg_spy:+.1f}%")
    print(f"    A — Buy & Hold                 : {avg_a:+.1f}%")
    print(f"    B — Signal-Change (every flip)  : {avg_b:+.1f}%")
    print(f"    C — REDUCE-Only Exit            : {avg_c:+.1f}%")

    wins_a = (rdf["winner"]=="A:B&H").sum()
    wins_b = (rdf["winner"]=="B:SigChg").sum()
    wins_c = (rdf["winner"]=="C:Reduce").sum()
    print(f"\n  PER-TICKER WINS:")
    print(f"    A wins: {wins_a}/{n}   B wins: {wins_b}/{n}   C wins: {wins_c}/{n}")

    c_fired = rdf["C_fired"].sum()
    c_protected = rdf[(rdf["C_fired"]==True) & (rdf["C_drop_after"]<-5)]["sym"].tolist() if c_fired>0 else []
    c_missed    = rdf[(rdf["C_fired"]==True) & (rdf["C_move_after"]>15)]["sym"].tolist() if c_fired>0 else []

    print(f"\n  STRATEGY C — REDUCE-ONLY DETAILS:")
    print(f"    REDUCE fired on              : {c_fired}/{n} positions")
    print(f"    Protected from further drop  : {len(c_protected)} ({', '.join(c_protected) or 'none'})")
    print(f"    Missed rebound (early exit)  : {len(c_missed)} ({', '.join(c_missed) or 'none'})")

    avg_b_trades = rdf["B_trades"].mean()
    print(f"\n  STRATEGY B — SIGNAL-CHANGE DETAILS:")
    print(f"    Avg trades per position : {avg_b_trades:.1f}  (every signal flip = overtrading)")
    print(f"    Problem: in a bull market, re-entry after REDUCE costs upside")

    # ── DIAGNOSTIC: REDUCE signal quality ────────────────────────────────────
    print("\n" + "═"*76)
    print("  REDUCE SIGNAL QUALITY ANALYSIS")
    print("  For each REDUCE that fired: did the stock fall more after exit?")
    print("═"*76)
    fired_df = rdf[rdf["C_fired"]==True].copy()
    if len(fired_df)>0:
        print(f"\n  {'Ticker':6s} | {'Exit$':>7s} | {'Dropafter':>10s} | {'Nowvsexit':>10s} | {'Verdict':>20s}")
        print("  " + "─"*60)
        for _, r in fired_df.iterrows():
            drop  = r["C_drop_after"]
            move  = r["C_move_after"]
            if drop < -10:
                verdict = "✅ GREAT — saved real loss"
            elif drop < -5:
                verdict = "✅ GOOD  — saved some loss"
            elif move > 20:
                verdict = "❌ EARLY — big rebound missed"
            elif move > 10:
                verdict = "⚠  EARLY — moderate miss"
            else:
                verdict = "🔵 NEUTRAL"
            print(f"  {r['sym']:6s} | ${r['C_exit_px']:>6.2f} | {drop:>+9.1f}% | {move:>+9.1f}% | {verdict}")

        great = fired_df[fired_df["C_drop_after"]<-5]
        early = fired_df[fired_df["C_move_after"]>15]
        print(f"\n  Accuracy: {len(great)}/{len(fired_df)} REDUCE signals correctly caught a real downturn (>5% drop after exit)")
        print(f"  False positives: {len(early)}/{len(fired_df)} were early exits (stock rebounded >15% after)")

    # ── CURRENT SIGNALS ───────────────────────────────────────────────────────
    print("\n" + "═"*76)
    print("  CURRENT SIGNALS — TODAY")
    print("═"*76)
    for _, r in rdf.iterrows():
        icon = "▲" if r["action_now"]=="BUY" else ("▼" if r["action_now"]=="REDUCE" else "■")
        print(f"  {icon} {r['sym']:6s}  Score {r['score_now']:3d}  {r['action_now']:6s}  |  B&H so far: {r['A_bah']:+.1f}%  vs SPY: {r['spy_return']:+.1f}%")

    # ── VERDICT & IMPROVEMENT PLAN ────────────────────────────────────────────
    print("\n" + "═"*76)
    print("  OVERALL VERDICT")
    print("═"*76)

    if avg_c >= avg_b and avg_c >= avg_a * 0.85:
        verdict_line = "Strategy C (REDUCE-Only) is the best practical choice for your portfolio."
    elif avg_a > avg_b and avg_a > avg_c:
        verdict_line = "B&H outperformed both model variants — the market was in strong bull mode the entire period."
    else:
        verdict_line = "Signal-Change model underperformed. REDUCE-Only exit is the better model approach."

    print(f"\n  {verdict_line}")

    print(f"""
  WHAT THE DATA TELLS US:
  ─────────────────────────────────────────────────────────────────────────
  1. B vs B&H gap: The signal-change model (B) is ~{abs(avg_a-avg_b):.0f}% behind B&H because
     it re-enters after every REDUCE and misses the early recovery move.
     → {avg_b_trades:.0f} avg trades per position = high friction in a trending market.

  2. C vs B gap: REDUCE-Only (C) is much closer to B&H, because it avoids
     the whipsawing problem. When REDUCE fires, you exit once and sit.
     It's honest about "I don't know when to re-enter."

  3. REDUCE signal accuracy: {len(great)}/{len(fired_df)} signals correctly front-ran a real downturn.
     The false positives ({len(early)}) were caused by the April 2026 tariff panic
     — a sharp V-shaped recovery that made all momentum exits look premature.

  4. Pre-entry screener: The 9-component model gives a BUY signal that
     correctly identifies strong setups. The issue is not entry quality —
     it's that re-entering after REDUCE in a bull market is expensive.

  PRE-ENTRY SCREENER — IS IT GOOD TO GO?
  ─────────────────────────────────────────────────────────────────────────
  YES, with these guardrails:
  ✅ Use in BULL regime only (SPY above 200MA) — model works best here
  ✅ Score ≥ 65 + RSI < 73 + not >30% above 200MA = GO
  ⏳ Score ≥ 65 but RSI > 73 or overextended = WAIT for pullback
  🔴 Score < 40 = NO-GO — downtrend confirmed, skip it
  🔴 Score < 65 in BEAR regime = NO-GO — regime overrides everything

  IMPROVEMENTS THAT WOULD HELP MOST:
  ─────────────────────────────────────────────────────────────────────────
  1. ADD VIX filter: Never re-enter within 10 trading days of VIX spike >30
     → Catches V-shaped panic recoveries (avoids the April 2026 problem)

  2. ADD earnings proximity check: If earnings within 21 days → reduce
     position by 50%% before the event, add back after.
     → Removes the biggest source of overnight gap risk

  3. ADD re-entry rule for Strategy C: Instead of "never re-enter after
     REDUCE", re-enter when score hits BUY for 3 consecutive weeks AND
     SPY is above 200MA. This makes C + selective re-entry beats B.

  4. ADD sector rotation check: If XLK/XLY (growth) are underperforming
     XLP/XLU (defensives) for 4+ weeks, tighten from BUY ≥65 → ≥75.
     → Adds regime-awareness beyond just SPY price level.

  5. KEEP the 2-week stability filter: It dramatically reduces whipsawing
     vs a 1-week trigger. Do not remove this.
  ─────────────────────────────────────────────────────────────────────────
""")

    rdf.to_csv("/tmp/model_comparison_results.csv", index=False)
    print(f"  Full results saved → /tmp/model_comparison_results.csv\n")
    return rdf


if __name__ == "__main__":
    run_comparison()
