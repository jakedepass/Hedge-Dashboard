"""
paper_trader.py — Auto-Paper-Trade Hedge Opportunities
=======================================================
Pulls scored opportunities from correlation_scorer.py, filters to the best
ones, and places paper trades on Alpaca. Dry-run by default.

How it works:
  1. Runs correlation_scorer.py --json to get fully scored opportunities
  2. Filters by min score, min verdict, min Polymarket volume
  3. Fetches account buying power + open positions (skips tickers already held)
  4. Sizes each trade: floor((buying_power * size_pct) / stock_price) shares
  5. Places orders via server.py POST /api/order (Alpaca paper)
  6. Appends every decision (trade or skip) to paper_trades.jsonl

Usage:
  python paper_trader.py                       # dry run — shows what would trade
  python paper_trader.py --execute             # actually place paper orders
  python paper_trader.py --min-score 0.80      # STRONG picks only
  python paper_trader.py --min-vol 100000      # high-volume PM markets only
  python paper_trader.py --max-trades 3        # cap at 3 new positions per run
  python paper_trader.py --size-pct 0.01       # 1% of buying power per trade (default)
  python paper_trader.py --min-vol 50000 --execute --max-trades 5

Output:
  paper_trades.jsonl — append-only trade log (one JSON object per line)
                        read by pnl_tracker.py to track P&L vs PM resolutions

Data:
  Opportunities  — from correlation_scorer.py (live PM + yfinance ~15min delayed)
  Account data   — Alpaca paper (live)
  Stock prices   — server.py /api/stock/<ticker> (yfinance ~15min delayed)

*** PAPER TRADING ONLY — no real money at risk ***
"""

import argparse
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests
import yfinance as yf
from dotenv import load_dotenv

import db as _db

load_dotenv()

ALPACA_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
ALPACA_URL    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")


def _alpaca_headers() -> dict:
    if not ALPACA_KEY or not ALPACA_SECRET:
        print("[error] ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type":        "application/json",
    }

# Verdicts that qualify for auto-trading (in order of confidence)
TRADEABLE_VERDICTS = {"STRONG ✅", "MODERATE ⚠️"}

# Hard cap on dollars per position regardless of size_pct
MAX_POSITION_USD = 2000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_account() -> dict:
    try:
        r = requests.get(f"{ALPACA_URL}/v2/account", headers=_alpaca_headers(), timeout=10)
        r.raise_for_status()
        a = r.json()
        return {"buying_power": float(a["buying_power"]), "equity": float(a["equity"])}
    except Exception as e:
        print(f"[error] Could not fetch Alpaca account: {e}", file=sys.stderr)
        sys.exit(1)


def get_positions() -> list[dict]:
    try:
        r = requests.get(f"{ALPACA_URL}/v2/positions", headers=_alpaca_headers(), timeout=10)
        r.raise_for_status()
        return [{"ticker": p["symbol"], "side": p["side"]} for p in r.json()]
    except Exception as e:
        print(f"[error] Could not fetch Alpaca positions: {e}", file=sys.stderr)
        sys.exit(1)


def get_stock_price(ticker: str) -> float | None:
    try:
        return round(float(yf.Ticker(ticker).fast_info.last_price), 2)
    except Exception as e:
        print(f"  [warn] Could not get price for {ticker}: {e}", file=sys.stderr)
        return None


def place_order(ticker: str, side: str, qty: int, opp: dict, dry_run: bool) -> dict | None:
    if dry_run:
        return {"status": "dry_run", "id": "dry_run"}
    pm_url = opp.get("pm_url", "")
    body = {
        "symbol":        ticker,
        "qty":           str(qty),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
        "client_order_id": f"pm_{pm_url[-32:]}" if pm_url else None,
    }
    body = {k: v for k, v in body.items() if v is not None}
    try:
        r = requests.post(f"{ALPACA_URL}/v2/orders", headers=_alpaca_headers(),
                          json=body, timeout=15)
        r.raise_for_status()
        order = r.json()
        return {"id": order.get("id"), "status": order.get("status")}
    except Exception as e:
        print(f"  [error] Order failed for {ticker}: {e}", file=sys.stderr)
        return None


def log_trade(record: dict):
    _db.log_trade(record)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def fetch_scored_opps(min_vol: float, limit: int) -> list[dict]:
    print(f"[trader] Fetching scored opportunities from correlation_scorer...", file=sys.stderr)
    try:
        result = subprocess.run(
            [sys.executable, "correlation_scorer.py",
             "--json", "--min-vol", str(min_vol), "--limit", str(limit)],
            capture_output=True, text=True, timeout=120,
            cwd=__import__("os").path.dirname(__file__) or ".",
        )
        if not result.stdout.strip():
            print("[error] No output from correlation_scorer.py", file=sys.stderr)
            sys.exit(1)
        return json.loads(result.stdout)
    except Exception as e:
        print(f"[error] Could not run correlation_scorer.py: {e}", file=sys.stderr)
        sys.exit(1)


def filter_opps(opps: list[dict], min_score: float) -> list[dict]:
    """Keep only tradeable, high-confidence opportunities."""
    return [
        o for o in opps
        if o.get("final_score", 0) >= min_score
        and o.get("verdict", "") in TRADEABLE_VERDICTS
    ]


def run(args):
    dry_run = not args.execute

    if dry_run:
        print(f"\n{'='*60}")
        print(f"  DRY RUN — pass --execute to place real paper orders")
        print(f"{'='*60}\n")

    # 1. Scored opportunities
    opps = fetch_scored_opps(args.min_vol, args.limit)
    print(f"[trader] {len(opps)} total scored opportunities")

    qualified = filter_opps(opps, args.min_score)
    print(f"[trader] {len(qualified)} qualify (score >= {args.min_score}, verdict STRONG/MODERATE)")

    if not qualified:
        print("[trader] Nothing to trade. Lower --min-score or --min-vol.")
        return

    # 2. Account state
    account   = get_account()
    positions = get_positions()

    buying_power   = float(account.get("buying_power", 0))
    held_tickers   = {p["ticker"].upper() for p in positions}

    print(f"[trader] Buying power: ${buying_power:,.2f}")
    print(f"[trader] Open positions: {held_tickers or 'none'}\n")

    # 3. Work through qualified opps, up to max_trades
    traded   = 0
    skipped  = 0

    for opp in qualified:
        if traded >= args.max_trades:
            break

        ticker    = opp.get("ticker", "").upper()
        direction = opp.get("direction", "long")
        side      = "buy" if direction == "long" else "sell"
        score     = opp.get("final_score", 0)
        verdict   = opp.get("verdict", "")
        edge      = opp.get("edge_estimate", 0)
        event     = opp.get("event_title", "")

        # Skip tickers we already hold (don't double-up on same exposure)
        if ticker in held_tickers:
            print(f"  SKIP  {ticker:<7}  already holding this ticker")
            skipped += 1
            log_trade({
                "timestamp":   ts(),
                "action":      "skip",
                "reason":      "already_holding",
                "ticker":      ticker,
                "direction":   direction,
                "event_title": event,
                "pm_url":      opp.get("pm_url", ""),
                "score":       score,
                "verdict":     verdict,
                "dry_run":     dry_run,
            })
            continue

        # Get current price for sizing
        price = get_stock_price(ticker)
        if not price or price <= 0:
            print(f"  SKIP  {ticker:<7}  could not get price")
            skipped += 1
            continue

        # Size: allocate size_pct of buying power, cap at MAX_POSITION_USD
        alloc_usd = min(buying_power * args.size_pct, MAX_POSITION_USD)
        qty       = math.floor(alloc_usd / price)

        if qty < 1:
            print(f"  SKIP  {ticker:<7}  position too small (${alloc_usd:.0f} / ${price:.2f} < 1 share)")
            skipped += 1
            continue

        actual_usd = qty * price

        # Print trade summary
        action_str = "EXECUTE" if not dry_run else "DRY RUN"
        print(
            f"  {action_str}  {ticker:<7}  {side.upper():<5}  {qty:>4} shares @ ${price:.2f}"
            f"  (${actual_usd:,.0f})  score={score:.3f}  {verdict}"
        )
        print(f"           event: {event[:70]}")
        print(f"           edge:  ${edge:.2f} per $100 PM | PM url: {opp.get('pm_url','')[:60]}")

        # Place order
        order_result = place_order(ticker, side, qty, opp, dry_run)

        if order_result is None:
            print(f"           *** ORDER FAILED — skipping log ***")
            skipped += 1
            continue

        # Log trade
        record = {
            "timestamp":      ts(),
            "action":         "trade",
            "dry_run":        dry_run,
            "ticker":         ticker,
            "side":           side,
            "direction":      direction,
            "qty":            qty,
            "entry_price":    price,
            "position_usd":   round(actual_usd, 2),
            "event_title":    event,
            "pm_url":         opp.get("pm_url", ""),
            "pm_prob":        opp.get("pm_prob", 0),
            "pm_volume":      opp.get("pm_volume", 0),
            "category":       opp.get("category", ""),
            "edge_estimate":  edge,
            "final_score":    score,
            "pattern_score":  opp.get("pattern_score", 0),
            "verdict":        verdict,
            "alpaca_order_id": order_result.get("id", ""),
            "alpaca_status":   order_result.get("status", ""),
        }
        log_trade(record)

        traded += 1
        if not dry_run:
            held_tickers.add(ticker)  # prevent double-trade in same run
        print()

    # 4. Summary
    print(f"\n{'='*60}")
    print(f"  {'DRY RUN ' if dry_run else ''}SUMMARY")
    print(f"{'='*60}")
    print(f"  Trades placed:  {traded}")
    print(f"  Skipped:        {skipped}")
    print(f"  DB:             {_db.DB_PATH}")
    if dry_run:
        print(f"\n  Run with --execute to place real paper orders.")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Auto-paper-trade hedge opportunities")
    ap.add_argument("--execute",    action="store_true",
                    help="Actually place paper orders (default: dry run)")
    ap.add_argument("--min-score",  type=float, default=0.75,
                    help="Minimum final_score to trade (default: 0.75)")
    ap.add_argument("--min-vol",    type=float, default=50000,
                    help="Min Polymarket volume to consider (default: 50000)")
    ap.add_argument("--limit",      type=int,   default=500,
                    help="Max PM markets to scan (default: 500)")
    ap.add_argument("--max-trades", type=int,   default=5,
                    help="Max new positions per run (default: 5)")
    ap.add_argument("--size-pct",   type=float, default=0.01,
                    help="Fraction of buying power per trade (default: 0.01 = 1%%)")
    args = ap.parse_args()

    run(args)


if __name__ == "__main__":
    main()
