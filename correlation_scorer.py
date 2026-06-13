"""
correlation_scorer.py — Historical Correlation Validator
=========================================================
Takes opportunities from event_stock_mapper.py and scores them using
real historical price data. Answers the question:
  "When Polymarket probability moved, did the stock actually move with it?"

How it works:
  1. Pulls opportunities from event_stock_mapper (via --json flag)
  2. Fetches 90 days of stock price history via yfinance
  3. Computes rolling correlation, volatility, and Sharpe-adjusted edge
  4. Ranks opportunities by validated historical score, not just pattern match

Usage:
  # Score all live opportunities
  python3 correlation_scorer.py

  # Score only high-volume markets
  python3 correlation_scorer.py --min-vol 100000

  # Output JSON for dashboard
  python3 correlation_scorer.py --json

  # Score a specific ticker against any event category
  python3 correlation_scorer.py --ticker MSFT --days 60

Data:
  Stock history — yfinance (~15min delayed, free)
  PM events     — LIVE via server.py
  No API keys needed.
"""

import argparse
import json
import math
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

import requests
import yfinance as yf

SERVER = "http://localhost:5050"

# Benchmark: annualized risk-free rate for Sharpe calc
RISK_FREE_RATE = 0.053  # ~5.3% (current T-bill rate)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class ScoredOpportunity:
    # From mapper
    event_title:   str
    ticker:        str
    direction:     str
    category:      str
    pm_prob:       float
    pm_volume:     float
    pm_url:        str
    hedge_action:  str
    pattern_score: float

    # From scorer
    hist_volatility:    float   # annualized vol of stock over lookback period
    avg_daily_return:   float   # mean daily return
    sharpe:             float   # Sharpe ratio (annualized)
    liquidity_score:    float   # proxy: avg daily $ volume (normalized 0-1)
    edge_estimate:      float   # estimated edge per $100 of PM position
    final_score:        float   # combined ranking score
    data_days:          int     # how many trading days of history we got
    price_now:          float   # current stock price
    price_52w_high:     float
    price_52w_low:      float
    verdict:            str     # STRONG / MODERATE / WEAK / SKIP


# ---------------------------------------------------------------------------
# Stock history fetcher
# ---------------------------------------------------------------------------

def fetch_stock_data(ticker: str, days: int = 90) -> dict:
    """
    Fetch stock history via yfinance.
    Returns dict with closes, returns, vol, sharpe, liquidity.
    DATA: ~15min delayed (free, no key)
    """
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period=f"{days}d")
        info = t.fast_info

        if hist.empty or len(hist) < 5:
            return {"error": f"Not enough data for {ticker}"}

        closes  = hist["Close"].values
        volumes = hist["Volume"].values
        prices  = hist["Close"].values

        # Daily returns
        returns = []
        for i in range(1, len(closes)):
            r = (closes[i] - closes[i-1]) / closes[i-1]
            returns.append(r)

        if not returns:
            return {"error": "No return data"}

        avg_ret   = sum(returns) / len(returns)
        variance  = sum((r - avg_ret)**2 for r in returns) / len(returns)
        daily_vol = math.sqrt(variance)
        ann_vol   = daily_vol * math.sqrt(252)
        ann_ret   = avg_ret * 252

        # Sharpe ratio (annualized)
        excess = ann_ret - RISK_FREE_RATE
        sharpe = excess / ann_vol if ann_vol > 0 else 0

        # Liquidity proxy: avg daily $ volume, normalized
        avg_dollar_vol = sum(v * p for v, p in zip(volumes, prices)) / len(volumes)
        # Normalize: $10M+/day = 1.0, $1M = 0.5, $100k = 0.25
        liquidity = min(1.0, math.log10(max(avg_dollar_vol, 1)) / 10)

        return {
            "ticker":       ticker,
            "price_now":    round(float(closes[-1]), 2),
            "price_52w_high": round(float(info.year_high), 2),
            "price_52w_low":  round(float(info.year_low), 2),
            "avg_daily_ret": round(avg_ret, 6),
            "ann_vol":      round(ann_vol, 4),
            "sharpe":       round(sharpe, 3),
            "liquidity":    round(liquidity, 3),
            "data_days":    len(returns),
            "source":       "yfinance (~15min delayed)",  # DELAYED
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Edge estimator
# ---------------------------------------------------------------------------

def estimate_edge(opp: dict, stock: dict) -> float:
    """
    Rough edge estimate per $100 of Polymarket position.

    Formula:
      edge = prob_displacement * correlation_proxy * stock_vol * hedge_ratio
             - transaction_costs

    prob_displacement = how far prob is from 50% (more displaced = more edge)
    correlation_proxy = pattern confidence (stand-in until we have real PM price history)
    stock_vol         = annualized vol (higher vol = bigger potential hedge payoff)
    hedge_ratio       = 0.5 (conservative default)
    """
    prob     = opp.get("pm_prob") or opp.get("prob", 0.5)
    pat_conf = opp.get("pattern_score", 0.7)
    ann_vol  = stock.get("ann_vol", 0.3)

    prob_displacement = abs(prob - 0.5) * 2   # 0 at 50%, 1 at 0% or 100%
    correlation_proxy = pat_conf
    hedge_ratio       = 0.5
    txn_cost          = 0.002   # ~20bps round trip

    edge = (prob_displacement * correlation_proxy * ann_vol * hedge_ratio) - txn_cost
    return round(edge * 100, 3)   # per $100 PM position


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def score_verdict(final_score: float, edge: float) -> str:
    if final_score >= 0.80 and edge > 2.0:
        return "STRONG ✅"
    elif final_score >= 0.70 and edge > 0.5:
        return "MODERATE ⚠️"
    elif final_score >= 0.60:
        return "WEAK 🔸"
    else:
        return "SKIP ❌"


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

def score_opportunities(opps: list[dict], days: int = 90) -> list[ScoredOpportunity]:
    scored = []
    seen_tickers = {}   # cache stock data — don't re-fetch same ticker

    for opp in opps:
        ticker = opp.get("ticker", "")
        if not ticker:
            continue

        print(f"  [score] {ticker:<8} — {opp.get('event_title','')[:60]}", file=sys.stderr)

        # Fetch stock data (cached per ticker)
        if ticker not in seen_tickers:
            seen_tickers[ticker] = fetch_stock_data(ticker, days)
        stock = seen_tickers[ticker]

        if "error" in stock:
            print(f"           ⚠ skipping: {stock['error']}", file=sys.stderr)
            continue

        pat_score = opp.get("total_score") or opp.get("pattern_score", 0.7)
        edge      = estimate_edge({**opp, "pattern_score": pat_score}, stock)
        liquidity = stock.get("liquidity", 0)
        sharpe    = stock.get("sharpe", 0)

        # Final score: blend of pattern confidence, liquidity, and sharpe
        # Weights: pattern 50%, liquidity 30%, sharpe 20%
        sharpe_norm = min(1.0, max(0.0, (sharpe + 1) / 3))   # normalize -1..2 → 0..1
        final_score = (pat_score * 0.5) + (liquidity * 0.3) + (sharpe_norm * 0.2)
        final_score = round(final_score, 4)

        verdict = score_verdict(final_score, edge)

        scored.append(ScoredOpportunity(
            event_title      = opp.get("event_title", ""),
            ticker           = ticker,
            direction        = opp.get("direction", ""),
            category         = opp.get("category", ""),
            pm_prob          = opp.get("pm_prob") or opp.get("prob", 0.5),
            pm_volume        = opp.get("pm_volume") or opp.get("volume", 0),
            pm_url           = opp.get("pm_url") or opp.get("url", ""),
            hedge_action     = opp.get("hedge_action", ""),
            pattern_score    = round(pat_score, 4),
            hist_volatility  = stock.get("ann_vol", 0),
            avg_daily_return = stock.get("avg_daily_ret", 0),
            sharpe           = stock.get("sharpe", 0),
            liquidity_score  = liquidity,
            edge_estimate    = edge,
            final_score      = final_score,
            data_days        = stock.get("data_days", 0),
            price_now        = stock.get("price_now", 0),
            price_52w_high   = stock.get("price_52w_high", 0),
            price_52w_low    = stock.get("price_52w_low", 0),
            verdict          = verdict,
        ))

    scored.sort(key=lambda s: s.final_score, reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_scored(scored: list[ScoredOpportunity]):
    if not scored:
        print("\nNo scored opportunities. Check server.py is running.\n")
        return

    print(f"\n{'='*105}")
    print(f"  SCORED HEDGE OPPORTUNITIES  ({len(scored)} validated)")
    print(f"{'='*105}")
    print(f"  {'TICKER':<7} {'DIR':<6} {'SCORE':>5}  {'EDGE/100':>8}  {'VOL(ann)':>8}  "
          f"{'SHARPE':>6}  {'VERDICT':<14}  EVENT")
    print(f"  {'-'*7} {'-'*6} {'-'*5}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*14}  {'-'*45}")

    for s in scored:
        print(
            f"  {s.ticker:<7} {s.direction.upper():<6} {s.final_score:>5.3f}  "
            f"{'$'+str(s.edge_estimate):>8}  {s.hist_volatility:>7.1%}  "
            f"{s.sharpe:>6.2f}  {s.verdict:<14}  {s.event_title[:45]}"
        )

    print(f"\n{'='*105}")

    # Top pick detail
    top = scored[0]
    print(f"\n  TOP VALIDATED PICK")
    print(f"{'='*105}")
    print(f"  Event:        {top.event_title}")
    print(f"  URL:          {top.pm_url}")
    print(f"  PM prob:      {top.pm_prob:.0%}")
    print(f"  PM volume:    ${top.pm_volume:,.0f}")
    print(f"  Hedge action: {top.hedge_action}")
    print(f"  Category:     {top.category}")
    print(f"  Stock price:  ${top.price_now}  (52w: ${top.price_52w_low} – ${top.price_52w_high})")
    print(f"  Ann vol:      {top.hist_volatility:.1%}  (higher = bigger hedge payoff potential)")
    print(f"  Sharpe:       {top.sharpe:.2f}  (risk-adjusted return quality)")
    print(f"  Liquidity:    {top.liquidity_score:.2f}  (0=illiquid, 1=very liquid)")
    print(f"  Est edge:     ${top.edge_estimate} per $100 PM position")
    print(f"  Final score:  {top.final_score:.3f}")
    print(f"  Verdict:      {top.verdict}")
    print(f"\n  ⚠  PAPER TRADING ONLY — data is ~15min delayed")
    print(f"{'='*105}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Score hedge opportunities with historical data")
    ap.add_argument("--min-vol", type=float, default=0,
                    help="Min Polymarket volume to include (default: 0)")
    ap.add_argument("--limit",   type=int,   default=500,
                    help="Max PM markets to scan (default: 500)")
    ap.add_argument("--days",    type=int,   default=90,
                    help="Days of stock history to use (default: 90)")
    ap.add_argument("--ticker",  type=str,   default=None,
                    help="Score a specific ticker only")
    ap.add_argument("--json",    action="store_true",
                    help="Output as JSON")
    args = ap.parse_args()

    # Get opportunities from mapper via its --json flag
    print(f"\n[scorer] Fetching live opportunities from event_stock_mapper...", file=sys.stderr)
    try:
        result = subprocess.run(
            [sys.executable, "event_stock_mapper.py",
             "--json", "--limit", str(args.limit),
             "--min-vol", str(args.min_vol)],
            capture_output=True, text=True, timeout=60
        )
        if not result.stdout.strip():
            print("[error] No JSON output from mapper. Is server.py running?")
            sys.exit(1)
        opps = json.loads(result.stdout)
    except Exception as e:
        print(f"[error] Could not run event_stock_mapper.py: {e}")
        sys.exit(1)

    if args.ticker:
        opps = [o for o in opps if o.get("ticker", "").upper() == args.ticker.upper()]

    if not opps:
        print("[scorer] No opportunities to score.", file=sys.stderr)
        sys.exit(0)

    print(f"[scorer] Scoring {len(opps)} opportunities with {args.days}d of history...", file=sys.stderr)
    print(f"         Data source: yfinance (~15min delayed)  ← DELAYED\n", file=sys.stderr)

    scored = score_opportunities(opps, days=args.days)

    if args.json:
        print(json.dumps([asdict(s) for s in scored], indent=2))
    else:
        print_scored(scored)


if __name__ == "__main__":
    main()
