"""
scheduler.py — Continuous auto-trader for the hedge dashboard.

Runs paper_trader.py every hour during US market hours and
position_closer.py once per day after market close.

Usage:
  python scheduler.py                    # run forever (Ctrl-C to stop)
  python scheduler.py --interval 30      # scan every 30 minutes
  python scheduler.py --min-vol 100000   # higher liquidity filter
  python scheduler.py --no-market-check  # run even outside market hours

Runs in your terminal or a background tmux/screen session.
Logs every action with timestamps.
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")

MARKET_OPEN  = (9, 30)   # 9:30 AM ET
MARKET_CLOSE = (16, 0)   # 4:00 PM ET


def ts() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")


def log(msg: str):
    print(f"[{ts()}] {msg}", flush=True)


def is_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE


def run_script(script: str, extra_args: list[str]) -> int:
    cmd = [sys.executable, script] + extra_args
    log(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    return result.returncode


def run_trader(args):
    trader_args = [
        "--execute",
        "--min-vol",   str(args.min_vol),
        "--max-trades", str(args.max_trades),
        "--min-score",  str(args.min_score),
    ]
    rc = run_script("paper_trader.py", trader_args)
    if rc != 0:
        log(f"  paper_trader.py exited with code {rc}")


def run_closer():
    log("Running position_closer.py (end-of-day close check)...")
    run_script("position_closer.py", ["--execute"])


def main():
    ap = argparse.ArgumentParser(description="Continuous hedge auto-trader")
    ap.add_argument("--interval",        type=int,   default=60,
                    help="Minutes between scans (default: 60)")
    ap.add_argument("--min-vol",         type=float, default=500_000,
                    help="Min PM market volume (default: 500000)")
    ap.add_argument("--max-trades",      type=int,   default=5,
                    help="Max new trades per run (default: 5)")
    ap.add_argument("--min-score",       type=float, default=0.75,
                    help="Min opportunity score (default: 0.75)")
    ap.add_argument("--no-market-check", action="store_true",
                    help="Ignore market hours (run always)")
    args = ap.parse_args()

    interval_secs = args.interval * 60
    last_close_check = None

    log("=" * 60)
    log("  Hedge Dashboard Auto-Trader")
    log(f"  Interval:   every {args.interval} min")
    log(f"  Min volume: ${args.min_vol:,.0f}")
    log(f"  Min score:  {args.min_score}")
    log(f"  Max trades: {args.max_trades} per run")
    log(f"  Market hrs: {'ignored' if args.no_market_check else '9:30–16:00 ET Mon–Fri'}")
    log("=" * 60)

    while True:
        now_et = datetime.now(ET)
        market_open = is_market_open() or args.no_market_check

        if market_open:
            log("Market open — running trader scan...")
            run_trader(args)
        else:
            log(f"Market closed ({now_et.strftime('%A %H:%M ET')}) — skipping trade scan")

        # Run position closer once per day after market close
        today = now_et.date()
        after_close = (now_et.hour, now_et.minute) >= MARKET_CLOSE
        if after_close and last_close_check != today:
            log("After close — checking for PM-resolved positions to close...")
            run_closer()
            last_close_check = today

        log(f"Sleeping {args.interval} min until next scan...")
        log("-" * 60)
        time.sleep(interval_secs)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n[{ts()}] Stopped by user.")
