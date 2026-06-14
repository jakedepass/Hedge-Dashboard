"""
pnl_tracker.py — P&L Tracker for Paper Hedge Trades
=====================================================
Reads paper_trades.jsonl, fetches current stock prices and Polymarket
resolution status, and reports unrealized P&L + hedge effectiveness.

How it works:
  1. Reads all real (non-dry-run) trades from paper_trades.jsonl
  2. Fetches current stock prices:
       - From Alpaca positions if the position is still open (live price)
       - From server.py /api/stock/<ticker> as fallback (15min delayed)
  3. Fetches PM resolution status from the Polymarket Gamma API
  4. Calculates unrealized P&L, directional correctness, and hedge status
  5. Prints a full report or exports JSON

Usage:
  python pnl_tracker.py                     # full report
  python pnl_tracker.py --json              # JSON output for dashboard
  python pnl_tracker.py --include-dry-runs  # also show dry-run trades
  python pnl_tracker.py --log paper_trades.jsonl  # explicit log path

Hedge outcome legend:
  CORRECT  — stock moved in the direction the PM probability implied
  WRONG    — stock moved opposite to prediction
  PENDING  — PM market not yet resolved
  OPEN     — PM resolved but not in a tradeable direction (neutral)

Data:
  Stock prices  — Alpaca paper (live) or yfinance (~15min delayed)
  PM resolution — Polymarket Gamma API (live)
"""

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import requests

import db as _db

SERVER   = "http://localhost:5050"
POLY_GAMMA = "https://gamma-api.polymarket.com"

session = requests.Session()
session.headers.update({"User-Agent": "hedge-dashboard-tracker/0.1"})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    # Identity
    timestamp:     str
    ticker:        str
    side:          str        # buy / sell
    direction:     str        # long / short
    qty:           int
    entry_price:   float
    position_usd:  float

    # PM context
    event_title:   str
    pm_url:        str
    pm_prob_entry: float      # PM prob when trade was placed
    category:      str
    edge_estimate: float
    final_score:   float
    verdict:       str

    # Current state
    current_price: float
    price_source:  str        # "alpaca_live" | "yfinance_delayed" | "unavailable"
    stock_pnl:     float      # unrealized P&L in dollars
    stock_pnl_pct: float      # unrealized P&L as % of position

    # PM resolution
    pm_status:     str        # "PENDING" | "RESOLVED_YES" | "RESOLVED_NO" | "CLOSED" | "UNKNOWN"
    pm_close_date: str

    # Outcome
    direction_correct: Optional[bool]   # None if PM unresolved
    hedge_outcome:     str              # CORRECT | WRONG | PENDING | N/A

    # Alpaca order ref
    alpaca_order_id: str


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def load_trades(log_path: str = None, include_dry_runs: bool = False) -> list[dict]:
    return _db.get_trades(include_dry_runs=include_dry_runs, action="trade")


def fetch_alpaca_positions() -> dict[str, dict]:
    """Returns a dict keyed by ticker with Alpaca live position data."""
    try:
        r = requests.get(f"{SERVER}/api/positions", timeout=10)
        r.raise_for_status()
        positions = r.json().get("positions", [])
        return {p["ticker"].upper(): p for p in positions}
    except Exception as e:
        print(f"[warn] Could not fetch Alpaca positions: {e}", file=sys.stderr)
        return {}


def fetch_stock_price(ticker: str) -> tuple[float, str]:
    """Returns (price, source_label)."""
    try:
        r = requests.get(f"{SERVER}/api/stock/{ticker}", timeout=10)
        r.raise_for_status()
        data = r.json()
        if "error" not in data:
            return float(data["price"]), "yfinance_delayed"
    except Exception:
        pass
    return 0.0, "unavailable"


def fetch_pm_resolution(pm_url: str) -> dict:
    """
    Looks up a Polymarket event by its slug (extracted from pm_url)
    and returns resolution info.

    Returns:
      {
        "status":     "PENDING" | "RESOLVED_YES" | "RESOLVED_NO" | "CLOSED" | "UNKNOWN",
        "close_date": ISO string or "",
        "pm_prob_now": float or None,
      }
    """
    default = {"status": "UNKNOWN", "close_date": "", "pm_prob_now": None}

    if not pm_url:
        return default

    # Extract slug: https://polymarket.com/event/{slug}
    try:
        slug = pm_url.rstrip("/").split("/event/")[-1]
    except Exception:
        return default

    try:
        # Try events endpoint first, fall back to markets endpoint
        event    = None
        markets  = []
        end_date = ""

        r = session.get(f"{POLY_GAMMA}/events", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        events = r.json()

        if events:
            event    = events[0]
            markets  = event.get("markets", [])
            end_date = event.get("endDate", "")
            closed   = event.get("closed", False)
            active   = event.get("active", True)
        else:
            # Slug might be a market-level slug — try /markets directly
            r2 = session.get(f"{POLY_GAMMA}/markets", params={"slug": slug}, timeout=10)
            r2.raise_for_status()
            mkt_list = r2.json()
            if mkt_list:
                markets  = mkt_list
                end_date = mkt_list[0].get("endDate", "")
                closed   = mkt_list[0].get("closed", False)
                active   = mkt_list[0].get("active", True)
            else:
                return default

        # Current YES prob from first market
        pm_prob_now = None
        if markets:
            try:
                prices   = json.loads(markets[0].get("outcomePrices") or "[]")
                outcomes = json.loads(markets[0].get("outcomes")      or "[]")
                yes_idx  = [o.lower() for o in outcomes].index("yes") if "yes" in [o.lower() for o in outcomes] else 0
                pm_prob_now = float(prices[yes_idx]) if prices else None
            except Exception:
                pass

        # Resolution check
        if closed or not active:
            for m in markets:
                resolution = m.get("resolution") or m.get("outcome") or ""
                if resolution.lower() in ("yes", "1", "true"):
                    return {"status": "RESOLVED_YES", "close_date": end_date, "pm_prob_now": pm_prob_now}
                if resolution.lower() in ("no", "0", "false"):
                    return {"status": "RESOLVED_NO",  "close_date": end_date, "pm_prob_now": pm_prob_now}
            return {"status": "CLOSED", "close_date": end_date, "pm_prob_now": pm_prob_now}

        return {"status": "PENDING", "close_date": end_date, "pm_prob_now": pm_prob_now}

    except Exception as e:
        print(f"  [warn] PM lookup failed for {pm_url}: {e}", file=sys.stderr)
        return default


# ---------------------------------------------------------------------------
# Outcome logic
# ---------------------------------------------------------------------------

def compute_outcome(trade: dict, stock_pnl: float, pm_status: str) -> tuple[Optional[bool], str]:
    """
    Determines if the hedge worked.

    Direction "long"  (buy stock): stock should go UP when YES resolves.
    Direction "short" (sell stock): stock should go DOWN when YES resolves.

    Profitable stock_pnl means the directional call was correct.
    """
    direction = trade.get("direction", "long")

    if pm_status == "PENDING":
        return None, "PENDING"

    if pm_status not in ("RESOLVED_YES", "RESOLVED_NO"):
        return None, "N/A"

    pm_resolved_yes = (pm_status == "RESOLVED_YES")

    # If PM resolved YES and we went long → stock should have gone up → pnl > 0 = correct
    # If PM resolved YES and we went short → stock should have gone down → pnl > 0 = correct
    # If PM resolved NO  → stock should not have moved much in either direction
    #   We still track directional accuracy for learning purposes.
    if pm_resolved_yes:
        correct = stock_pnl > 0
    else:
        # PM resolved NO: the event didn't happen. The hedge should have been
        # a small loss (we paid to insure against an event that didn't occur).
        # We call it CORRECT if we kept most of the position value (pnl > -5%).
        position_usd = trade.get("position_usd", 1)
        correct = stock_pnl > -(position_usd * 0.05)

    direction_correct = correct
    hedge_outcome = "CORRECT" if correct else "WRONG"
    return direction_correct, hedge_outcome


# ---------------------------------------------------------------------------
# Core tracker
# ---------------------------------------------------------------------------

def track(trades: list[dict]) -> list[TradeResult]:
    alpaca_positions = fetch_alpaca_positions()
    print(f"[tracker] {len(alpaca_positions)} open Alpaca positions found", file=sys.stderr)

    # Cache prices — don't re-fetch same ticker
    price_cache: dict[str, tuple[float, str]] = {}
    pm_cache:    dict[str, dict] = {}

    results = []
    for trade in trades:
        ticker     = trade.get("ticker", "").upper()
        side       = trade.get("side", "buy")
        direction  = trade.get("direction", "long")
        qty        = int(trade.get("qty", 0))
        entry      = float(trade.get("entry_price", 0))
        pos_usd    = float(trade.get("position_usd", 0))
        pm_url     = trade.get("pm_url", "")

        print(f"  [track] {ticker:<7} {side.upper():<5} {qty}sh @ ${entry:.2f}  "
              f"{trade.get('event_title','')[:50]}", file=sys.stderr)

        # --- Current stock price ---
        if ticker in alpaca_positions:
            ap = alpaca_positions[ticker]
            current_price = float(ap["current_price"])
            price_source  = "alpaca_live"
        elif ticker in price_cache:
            current_price, price_source = price_cache[ticker]
        else:
            current_price, price_source = fetch_stock_price(ticker)
            price_cache[ticker] = (current_price, price_source)

        # --- P&L ---
        direction_mult = 1 if side == "buy" else -1
        stock_pnl      = round((current_price - entry) * qty * direction_mult, 2)
        stock_pnl_pct  = round(stock_pnl / pos_usd * 100, 2) if pos_usd else 0.0

        # --- PM resolution ---
        if pm_url not in pm_cache:
            pm_cache[pm_url] = fetch_pm_resolution(pm_url)
        pm_info   = pm_cache[pm_url]
        pm_status = pm_info["status"]
        pm_close  = pm_info.get("close_date", "")

        # --- Outcome ---
        direction_correct, hedge_outcome = compute_outcome(trade, stock_pnl, pm_status)

        results.append(TradeResult(
            timestamp          = trade.get("timestamp", ""),
            ticker             = ticker,
            side               = side,
            direction          = direction,
            qty                = qty,
            entry_price        = entry,
            position_usd       = pos_usd,
            event_title        = trade.get("event_title", ""),
            pm_url             = pm_url,
            pm_prob_entry      = float(trade.get("pm_prob", 0)),
            category           = trade.get("category", ""),
            edge_estimate      = float(trade.get("edge_estimate", 0)),
            final_score        = float(trade.get("final_score", 0)),
            verdict            = trade.get("verdict", ""),
            current_price      = current_price,
            price_source       = price_source,
            stock_pnl          = stock_pnl,
            stock_pnl_pct      = stock_pnl_pct,
            pm_status          = pm_status,
            pm_close_date      = pm_close,
            direction_correct  = direction_correct,
            hedge_outcome      = hedge_outcome,
            alpaca_order_id    = trade.get("alpaca_order_id", ""),
        ))

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_report(results: list[TradeResult]):
    if not results:
        print("\nNo trades found in log. Run paper_trader.py --execute first.\n")
        return

    total_pnl     = sum(r.stock_pnl for r in results)
    total_invested = sum(r.position_usd for r in results)
    resolved       = [r for r in results if r.pm_status.startswith("RESOLVED")]
    correct        = [r for r in resolved if r.direction_correct is True]
    wrong          = [r for r in resolved if r.direction_correct is False]

    print(f"\n{'='*115}")
    print(f"  PAPER TRADE P&L TRACKER  ({len(results)} trades)")
    print(f"{'='*115}")
    print(
        f"  {'TICKER':<7} {'SIDE':<5} {'QTY':>4}  {'ENTRY':>8}  {'NOW':>8}  "
        f"{'P&L $':>9}  {'P&L%':>6}  {'PM STATUS':<16}  {'HEDGE':<8}  EVENT"
    )
    print(
        f"  {'-'*7} {'-'*5} {'-'*4}  {'-'*8}  {'-'*8}  "
        f"{'-'*9}  {'-'*6}  {'-'*16}  {'-'*8}  {'-'*40}"
    )

    for r in results:
        pnl_str    = f"{'+'if r.stock_pnl>=0 else ''}${r.stock_pnl:,.2f}"
        pnl_pct    = f"{'+'if r.stock_pnl_pct>=0 else ''}{r.stock_pnl_pct:.1f}%"
        now_str    = f"${r.current_price:.2f}" if r.current_price else "N/A"
        entry_str  = f"${r.entry_price:.2f}"
        delayed    = " *" if r.price_source == "yfinance_delayed" else "  "
        print(
            f"  {r.ticker:<7} {r.side.upper():<5} {r.qty:>4}  {entry_str:>8}  "
            f"{now_str:>8}{delayed}  {pnl_str:>9}  {pnl_pct:>6}  "
            f"{r.pm_status:<16}  {r.hedge_outcome:<8}  {r.event_title[:40]}"
        )

    print(f"\n  * price is ~15min delayed (yfinance)")
    print(f"{'='*115}")

    print(f"\n  SUMMARY")
    print(f"{'='*115}")
    print(f"  Total invested:     ${total_invested:>12,.2f}")
    print(f"  Total P&L (unreal): ${total_pnl:>+12,.2f}  ({total_pnl/total_invested*100:+.2f}%)" if total_invested else "")
    print(f"  Open trades:        {len(results)}")
    print(f"  PM markets pending: {sum(1 for r in results if r.pm_status == 'PENDING')}")
    print(f"  PM markets resolved:{len(resolved)}")
    if resolved:
        print(f"    Hedge CORRECT:    {len(correct)} / {len(resolved)}")
        print(f"    Hedge WRONG:      {len(wrong)} / {len(resolved)}")
        win_rate = len(correct) / len(resolved) * 100
        print(f"    Win rate:         {win_rate:.0f}%")

    best  = max(results, key=lambda r: r.stock_pnl)
    worst = min(results, key=lambda r: r.stock_pnl)
    print(f"\n  Best trade:   {best.ticker} {best.side.upper()}  {best.stock_pnl:+,.2f}  ({best.event_title[:50]})")
    print(f"  Worst trade:  {worst.ticker} {worst.side.upper()}  {worst.stock_pnl:+,.2f}  ({worst.event_title[:50]})")
    print(f"\n  ⚠  PAPER TRADING ONLY — no real money involved")
    print(f"{'='*115}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Track P&L for paper hedge trades")
    ap.add_argument("--log",              default=None,
                    help="Ignored (kept for backwards compatibility)")
    ap.add_argument("--json",             action="store_true",
                    help="Output as JSON")
    ap.add_argument("--include-dry-runs", action="store_true",
                    help="Include dry-run trades in the report")
    args = ap.parse_args()

    trades = load_trades(include_dry_runs=args.include_dry_runs)
    if not trades:
        print(f"[tracker] No real trades found in {_db.DB_PATH}.")
        print(f"          Run: python paper_trader.py --execute")
        return

    print(f"[tracker] Tracking {len(trades)} trades from {_db.DB_PATH}...", file=sys.stderr)
    results = track(trades)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, default=str))
    else:
        print_report(results)


if __name__ == "__main__":
    main()
