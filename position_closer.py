"""
position_closer.py — Close Paper Positions When PM Markets Resolve
==================================================================
Reads paper_trades.jsonl, identifies open positions whose Polymarket
event has resolved, and closes them on Alpaca paper.

How it works:
  1. Reads paper_trades.jsonl — finds all opened (non-closed) trades
  2. Checks Polymarket Gamma API for resolution status of each event
  3. For positions where PM has resolved (YES or NO):
       - Closes the stock position on Alpaca (sell to close long, buy to close short)
       - Logs a "close" record to paper_trades.jsonl
  4. Reports realized P&L for each closed position

Usage:
  python position_closer.py               # dry run — show what would close
  python position_closer.py --execute     # actually close positions
  python position_closer.py --all         # close ALL open positions regardless of PM resolution
  python position_closer.py --ticker SPY  # close only a specific ticker

Output:
  paper_trades.jsonl — appended with "close" records for each position closed
  Terminal summary of realized P&L

*** PAPER TRADING ONLY — no real money ***
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import requests
import yfinance as yf

import db as _db

SERVER     = "http://localhost:5050"
POLY_GAMMA = "https://gamma-api.polymarket.com"

session = requests.Session()
session.headers.update({"User-Agent": "hedge-dashboard-closer/0.1"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_open_trades(log_path: str = None) -> list[dict]:
    return _db.get_open_trades(include_dry_runs=False)


def check_pm_resolution(pm_url: str) -> str:
    """
    Returns "RESOLVED_YES", "RESOLVED_NO", "CLOSED", or "PENDING".
    """
    if not pm_url:
        return "PENDING"
    try:
        slug = pm_url.rstrip("/").split("/event/")[-1]
        r    = session.get(f"{POLY_GAMMA}/events", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        events = r.json()

        if not events:
            # Try markets endpoint
            r2 = session.get(f"{POLY_GAMMA}/markets", params={"slug": slug}, timeout=10)
            r2.raise_for_status()
            markets = r2.json()
        else:
            markets = events[0].get("markets", [])

        if not markets:
            return "PENDING"

        mkt    = markets[0]
        closed = mkt.get("closed", False)
        active = mkt.get("active", True)

        if closed or not active:
            resolution = mkt.get("resolution") or mkt.get("outcome") or ""
            if resolution.lower() in ("yes", "1", "true"):
                return "RESOLVED_YES"
            if resolution.lower() in ("no", "0", "false"):
                return "RESOLVED_NO"
            return "CLOSED"

        return "PENDING"
    except Exception as e:
        print(f"  [warn] PM lookup failed for {pm_url}: {e}", file=sys.stderr)
        return "PENDING"


def get_current_price(ticker: str) -> float | None:
    try:
        return round(float(yf.Ticker(ticker).fast_info.last_price), 2)
    except Exception as e:
        print(f"  [warn] Could not get price for {ticker}: {e}", file=sys.stderr)
        return None


def close_on_alpaca(ticker: str, qty: int, side: str, open_order_id: str,
                    dry_run: bool) -> dict | None:
    """
    Places the closing order on Alpaca.
    side: "buy" (to close a short) or "sell" (to close a long)
    """
    if dry_run:
        return {"id": "dry_run", "status": "dry_run"}
    try:
        r = requests.post(
            f"{SERVER}/api/order",
            json={
                "ticker":      ticker,
                "qty":         qty,
                "side":        side,
                "order_type":  "market",
                "note":        f"close: {open_order_id}",
            },
            timeout=15,
        )
        r.raise_for_status()
        resp = r.json()
        return resp.get("alpaca_order", {})
    except Exception as e:
        print(f"  [error] Close order failed for {ticker}: {e}", file=sys.stderr)
        return None


def log_close(record: dict, log_path: str = None):
    _db.log_trade(record)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def run(args):
    dry_run = not args.execute

    if dry_run:
        print(f"\n{'='*60}")
        print(f"  DRY RUN — pass --execute to actually close positions")
        print(f"{'='*60}\n")

    open_trades = load_open_trades()

    if args.ticker:
        open_trades = [t for t in open_trades if t["ticker"].upper() == args.ticker.upper()]

    print(f"[closer] {len(open_trades)} open trade(s) found in {_db.DB_PATH}")

    if not open_trades:
        print("[closer] Nothing to close.")
        return

    closed_count  = 0
    skipped_count = 0
    total_realized = 0.0

    for trade in open_trades:
        ticker    = trade["ticker"]
        side_open = trade["side"]          # "buy" or "sell"
        qty       = int(trade["qty"])
        entry     = float(trade["entry_price"])
        pm_url    = trade.get("pm_url", "")
        open_id   = trade.get("alpaca_order_id", "")

        # Determine close side
        side_close = "sell" if side_open == "buy" else "buy"

        # Check PM resolution
        if args.all:
            pm_status = "FORCE_CLOSE"
        else:
            pm_status = check_pm_resolution(pm_url)

        should_close = pm_status in ("RESOLVED_YES", "RESOLVED_NO", "CLOSED", "FORCE_CLOSE")

        # Get current price for P&L calculation
        current_price = get_current_price(ticker)
        if current_price:
            direction_mult = 1 if side_open == "buy" else -1
            realized_pnl   = round((current_price - entry) * qty * direction_mult, 2)
        else:
            realized_pnl = 0.0

        print(
            f"  {ticker:<7} {side_open.upper():<5} {qty}sh "
            f"entry=${entry:.2f} now=${current_price or '?'}  "
            f"P&L={'+'if realized_pnl>=0 else ''}${realized_pnl:.2f}  "
            f"PM={pm_status}"
        )

        if not should_close:
            print(f"           → PM still PENDING, skipping")
            skipped_count += 1
            continue

        action = "DRY RUN" if dry_run else "CLOSE"
        print(f"           → {action}: {side_close.upper()} {qty} {ticker}")

        order = close_on_alpaca(ticker, qty, side_close, open_id, dry_run)
        if order is None:
            print(f"           *** CLOSE ORDER FAILED ***")
            skipped_count += 1
            continue

        # Log close record
        close_record = {
            "timestamp":        ts(),
            "action":           "close",
            "dry_run":          dry_run,
            "ticker":           ticker,
            "side":             side_close,
            "qty":              qty,
            "close_price":      current_price or 0.0,
            "realized_pnl":     realized_pnl,
            "entry_price":      entry,
            "open_timestamp":   trade.get("timestamp", ""),
            "closes_order_id":  open_id,
            "pm_url":           pm_url,
            "pm_status":        pm_status,
            "event_title":      trade.get("event_title", ""),
            "alpaca_close_id":  order.get("id", ""),
            "alpaca_status":    order.get("status", ""),
        }
        log_close(close_record)

        total_realized += realized_pnl
        closed_count += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"  {'DRY RUN ' if dry_run else ''}CLOSE SUMMARY")
    print(f"{'='*60}")
    print(f"  Positions closed:  {closed_count}")
    print(f"  Skipped (pending): {skipped_count}")
    pnl_color = "+" if total_realized >= 0 else ""
    print(f"  Realized P&L:      {pnl_color}${total_realized:.2f}")
    print(f"  DB:                {_db.DB_PATH}")
    if dry_run:
        print(f"\n  Run with --execute to actually close positions.")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Close paper positions when PM markets resolve")
    ap.add_argument("--execute", action="store_true",
                    help="Actually close positions (default: dry run)")
    ap.add_argument("--all",    action="store_true",
                    help="Close ALL open positions regardless of PM resolution")
    ap.add_argument("--ticker", type=str, default=None,
                    help="Only close positions for a specific ticker")
    ap.add_argument("--log",    default=None,
                    help="Ignored (kept for backwards compatibility)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
