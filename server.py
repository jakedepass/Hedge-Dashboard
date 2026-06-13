"""
server.py — Hedge Dashboard Backend
=====================================
Endpoints:
  GET /api/polymarket     — live binary markets from Polymarket Gamma API  [FREE, no key]
  GET /api/prices         — BTC, ETH, SPY, QQQ prices from CoinGecko/yfinance  [FREE, no key]
  GET /api/stock/<ticker> — single stock quote via yfinance  [FREE, no key]
  GET /api/account        — Alpaca paper account balance & positions  [REQUIRES key]
  POST /api/order         — place a paper trade on Alpaca  [REQUIRES key]
  GET /api/positions      — open Alpaca paper positions  [REQUIRES key]

Run:
  python server.py

Data freshness:
  Polymarket prices  — live (fetched on each request)
  Crypto prices      — live via CoinGecko (60s rate limit on free tier)
  Stock quotes       — ~15 min delayed via yfinance
  Alpaca account     — live (paper)
"""

import os
import json
import subprocess
import sys
import time
import requests
import yfinance as yf

from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)  # allow the HTML frontend to call this from file:// or localhost

# ---------------------------------------------------------------------------
# Config — all keys from .env, never hardcoded
# ---------------------------------------------------------------------------

ALPACA_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
ALPACA_URL    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

POLY_GAMMA    = "https://gamma-api.polymarket.com"
COINGECKO     = "https://api.coingecko.com/api/v3"

PORT = int(os.getenv("FLASK_PORT", 5050))

session = requests.Session()
session.headers.update({"User-Agent": "hedge-dashboard/0.1"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def alpaca_headers() -> dict:
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise EnvironmentError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env"
        )
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type": "application/json",
    }


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# /api/polymarket  — live binary markets
# DATA: LIVE  |  KEY: none
# ---------------------------------------------------------------------------

@app.route("/api/polymarket")
def polymarket():
    """
    Returns up to 50 active binary Polymarket markets, normalized to:
      { id, title, yes_price, no_price, volume, end_date, url }
    Query params:
      ?limit=N    — max markets (default 50)
      ?keyword=X  — filter by keyword in question text
    """
    limit   = int(request.args.get("limit", 50))
    keyword = request.args.get("keyword", "").lower()

    # Paginate to get up to `limit` markets (API caps at 100 per request)
    raw = []
    offset = 0
    while len(raw) < limit:
        batch_size = min(100, limit - len(raw))
        params = {"active": "true", "closed": "false",
                  "limit": batch_size, "offset": offset}
        try:
            r = session.get(f"{POLY_GAMMA}/markets", params=params, timeout=15)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            return jsonify({"error": f"Polymarket fetch failed: {e}"}), 502
        if not batch:
            break
        raw.extend(batch)
        offset += len(batch)
        if len(batch) < batch_size:
            break

    markets = []
    for m in raw:
        title = (m.get("question") or "").strip()
        if keyword and keyword not in title.lower():
            continue

        # outcomePrices and outcomes are JSON-encoded strings
        try:
            prices   = json.loads(m.get("outcomePrices") or "[]")
            outcomes = json.loads(m.get("outcomes")      or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if len(prices) != 2 or len(outcomes) != 2:
            continue

        try:
            yes_idx = [o.lower() for o in outcomes].index("yes")
        except ValueError:
            yes_idx = 0

        yes_price = float(prices[yes_idx])
        no_price  = float(prices[1 - yes_idx])
        if yes_price <= 0 or no_price <= 0:
            continue

        slug = m.get("slug") or m.get("id", "")
        markets.append({
            "id":        m.get("conditionId") or m.get("id"),
            "title":     title,
            "yes_price": round(yes_price, 4),
            "no_price":  round(no_price, 4),
            "volume":    float(m.get("volumeNum") or 0),
            "end_date":  m.get("endDate", ""),
            "url":       f"https://polymarket.com/event/{slug}",
            # implied prob = yes midpoint
            "prob":      round(yes_price, 4),
        })

    return jsonify({
        "source":    "Polymarket Gamma API",  # LIVE
        "fetched_at": ts(),
        "count":     len(markets),
        "markets":   markets,
    })


# ---------------------------------------------------------------------------
# /api/prices  — BTC, ETH (CoinGecko) + SPY, QQQ (yfinance)
# DATA: LIVE (crypto) / ~15min delayed (equities)  |  KEY: none
# ---------------------------------------------------------------------------

@app.route("/api/prices")
def prices():
    """
    Returns current prices for BTC, ETH, SPY, QQQ.
    Crypto: CoinGecko free tier (no key, ~60s cache on their side).
    Equities: yfinance fast_info (~15 min delayed on free tier).
    """
    result = {}

    # --- Crypto via CoinGecko (LIVE, free) ---
    try:
        r = session.get(
            f"{COINGECKO}/simple/price",
            params={"ids": "bitcoin,ethereum", "vs_currencies": "usd",
                    "include_24hr_change": "true"},
            timeout=10,
        )
        r.raise_for_status()
        cg = r.json()
        result["BTC"] = {
            "price":     cg["bitcoin"]["usd"],
            "change_24h": cg["bitcoin"].get("usd_24h_change", 0),
            "source":    "CoinGecko",   # LIVE
        }
        result["ETH"] = {
            "price":     cg["ethereum"]["usd"],
            "change_24h": cg["ethereum"].get("usd_24h_change", 0),
            "source":    "CoinGecko",   # LIVE
        }
    except Exception as e:
        result["crypto_error"] = str(e)

    # --- Equities via yfinance (~15min delayed, free) ---
    for ticker in ["SPY", "QQQ"]:
        try:
            info = yf.Ticker(ticker).fast_info
            result[ticker] = {
                "price":     round(info.last_price, 2),
                "change_24h": round(
                    (info.last_price / info.previous_close - 1) * 100, 2
                ) if info.previous_close else None,
                "source": "yfinance (~15min delayed)",  # DELAYED
            }
        except Exception as e:
            result[ticker] = {"error": str(e)}

    return jsonify({
        "fetched_at": ts(),
        "prices": result,
    })


# ---------------------------------------------------------------------------
# /api/stock/<ticker>  — single stock quote + basic stats
# DATA: ~15min delayed  |  KEY: none
# ---------------------------------------------------------------------------

@app.route("/api/stock/<ticker>")
def stock_quote(ticker: str):
    """
    Returns price, day change, 52w range, market cap, and
    30-day historical closes (for correlation scoring).
    ticker: any valid yfinance symbol (e.g. MRNA, NVDA, AAPL)
    """
    ticker = ticker.upper()
    try:
        t    = yf.Ticker(ticker)
        info = t.fast_info
        hist = t.history(period="30d")["Close"]

        closes = [round(float(v), 2) for v in hist.values]
        dates  = [str(d.date()) for d in hist.index]

        return jsonify({
            "ticker":      ticker,
            "price":       round(info.last_price, 2),
            "prev_close":  round(info.previous_close, 2),
            "change_pct":  round((info.last_price / info.previous_close - 1) * 100, 3),
            "market_cap":  info.market_cap,
            "52w_high":    round(info.year_high, 2),
            "52w_low":     round(info.year_low, 2),
            "source":      "yfinance (~15min delayed)",  # DELAYED
            "fetched_at":  ts(),
            "history": {
                "dates":  dates,
                "closes": closes,
            },
        })
    except Exception as e:
        return jsonify({"error": f"yfinance error for {ticker}: {e}"}), 502


# ---------------------------------------------------------------------------
# /api/account  — Alpaca paper account summary
# DATA: LIVE  |  KEY: required (ALPACA_API_KEY + ALPACA_SECRET_KEY in .env)
# ---------------------------------------------------------------------------

@app.route("/api/account")
def account():
    """
    Returns Alpaca paper account: equity, cash, buying power, P&L.
    Requires ALPACA_API_KEY and ALPACA_SECRET_KEY in .env.
    """
    try:
        r = requests.get(
            f"{ALPACA_URL}/v2/account",
            headers=alpaca_headers(),
            timeout=10,
        )
        r.raise_for_status()
        a = r.json()
        return jsonify({
            "source":        "Alpaca Paper Trading",  # LIVE
            "fetched_at":    ts(),
            "equity":        float(a["equity"]),
            "cash":          float(a["cash"]),
            "buying_power":  float(a["buying_power"]),
            "portfolio_value": float(a["portfolio_value"]),
            "daytrade_count": a.get("daytrade_count", 0),
            "status":        a["status"],
        })
    except EnvironmentError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Alpaca account error: {e}"}), 502


# ---------------------------------------------------------------------------
# /api/positions  — open Alpaca paper positions
# DATA: LIVE  |  KEY: required
# ---------------------------------------------------------------------------

@app.route("/api/positions")
def positions():
    """
    Returns all open paper positions with entry price, current price,
    unrealized P&L, and which PM contract the hedge is for (if tagged).
    """
    try:
        r = requests.get(
            f"{ALPACA_URL}/v2/positions",
            headers=alpaca_headers(),
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
        pos = []
        for p in raw:
            pos.append({
                "ticker":        p["symbol"],
                "qty":           float(p["qty"]),
                "side":          p["side"],
                "entry_price":   float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "market_value":  float(p["market_value"]),
                "unrealized_pl": float(p["unrealized_pl"]),
                "unrealized_plpc": float(p["unrealized_plpc"]) * 100,
                # client_order_id or metadata can carry PM contract ID
                # (paper_trader.py will tag orders with pm_contract_id)
            })
        return jsonify({
            "source":     "Alpaca Paper Trading",  # LIVE
            "fetched_at": ts(),
            "count":      len(pos),
            "positions":  pos,
        })
    except EnvironmentError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Alpaca positions error: {e}"}), 502


# ---------------------------------------------------------------------------
# POST /api/order  — place a paper trade
# DATA: LIVE (paper)  |  KEY: required
#
# Body (JSON):
#   ticker        — stock symbol, e.g. "MRNA"
#   qty           — number of shares (fractional ok if Alpaca enables it)
#   side          — "buy" or "sell"
#   order_type    — "market" | "limit" (default: market)
#   limit_price   — required if order_type=limit
#   pm_contract   — the Polymarket contract ID this hedge is for (logged only)
#   edge          — estimated edge in dollars (logged only)
#   note          — free-text reason (logged only)
#
# Always shows: ticker, direction, size, edge, PM contract being hedged
# ---------------------------------------------------------------------------

@app.route("/api/order", methods=["POST"])
def place_order():
    """
    Submits a paper order to Alpaca. Logs the PM contract being hedged
    and the estimated edge so every trade is traceable.

    *** PAPER TRADING ONLY — no real money ***
    """
    body = request.get_json(force=True)

    # Required fields
    ticker     = body.get("ticker", "").upper()
    qty        = body.get("qty")
    side       = body.get("side", "").lower()
    order_type = body.get("order_type", "market")

    # Hedge metadata (logged, not sent to Alpaca)
    pm_contract = body.get("pm_contract", "")
    edge        = body.get("edge", None)
    note        = body.get("note", "")

    if not ticker or not qty or side not in ("buy", "sell"):
        return jsonify({"error": "ticker, qty, and side (buy|sell) are required"}), 400

    alpaca_body = {
        "symbol":        ticker,
        "qty":           str(qty),
        "side":          side,
        "type":          order_type,
        "time_in_force": "day",
        # Tag PM contract in client_order_id so we can reconcile later
        "client_order_id": f"pm_{pm_contract[:36]}" if pm_contract else None,
    }
    if order_type == "limit":
        lp = body.get("limit_price")
        if not lp:
            return jsonify({"error": "limit_price required for limit orders"}), 400
        alpaca_body["limit_price"] = str(lp)

    # Remove None values (Alpaca rejects unexpected nulls)
    alpaca_body = {k: v for k, v in alpaca_body.items() if v is not None}

    try:
        r = requests.post(
            f"{ALPACA_URL}/v2/orders",
            headers=alpaca_headers(),
            json=alpaca_body,
            timeout=10,
        )
        r.raise_for_status()
        order = r.json()
    except EnvironmentError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Alpaca order error: {e}", "detail": str(e)}), 502

    # Summary printed to terminal so you can see every trade at a glance
    print(
        f"\n[ORDER] {'='*50}\n"
        f"  Ticker:      {ticker}\n"
        f"  Direction:   {side.upper()}\n"
        f"  Qty:         {qty} shares\n"
        f"  Type:        {order_type}\n"
        f"  Edge:        {'$'+str(edge) if edge is not None else 'not specified'}\n"
        f"  PM contract: {pm_contract or 'not specified'}\n"
        f"  Note:        {note}\n"
        f"  Alpaca ID:   {order.get('id')}\n"
        f"  Status:      {order.get('status')}\n"
        f"{'='*52}\n"
    )

    return jsonify({
        "success":      True,
        "source":       "Alpaca Paper Trading",   # PAPER — no real money
        "submitted_at": ts(),
        "trade_summary": {
            "ticker":      ticker,
            "direction":   side,
            "qty":         qty,
            "edge":        edge,
            "pm_contract": pm_contract,
            "note":        note,
        },
        "alpaca_order": {
            "id":           order.get("id"),
            "status":       order.get("status"),
            "filled_qty":   order.get("filled_qty"),
            "filled_price": order.get("filled_avg_price"),
        },
    })


# ---------------------------------------------------------------------------
# /api/scan  — run event_stock_mapper.py and return JSON opportunities
# DATA: LIVE (PM) / ~15min delayed (stock prices)  |  KEY: none
# ---------------------------------------------------------------------------

@app.route("/api/scan")
def scan_opportunities():
    """
    Runs event_stock_mapper.py --json and returns ranked hedge opportunities.
    Query params:
      ?min_vol=N   — minimum PM market volume (default 0)
      ?limit=N     — max markets to scan (default 200)
    Takes ~5-10s (fetches live PM data).
    """
    min_vol = request.args.get("min_vol", 0, type=float)
    limit   = request.args.get("limit",  200, type=int)
    script  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "event_stock_mapper.py")
    try:
        result = subprocess.run(
            [sys.executable, script, "--json",
             "--limit", str(limit), "--min-vol", str(min_vol)],
            capture_output=True, text=True, timeout=60,
        )
        if not result.stdout.strip():
            return jsonify({"error": "mapper returned no output",
                            "stderr": result.stderr[:500]}), 502
        opps = json.loads(result.stdout)
        return jsonify({
            "source":       "event_stock_mapper.py",  # LIVE PM + DELAYED stock
            "fetched_at":   ts(),
            "count":        len(opps),
            "opportunities": opps,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "scan timed out (60s)"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# /api/pnl  — run pnl_tracker.py and return trade P&L JSON
# DATA: LIVE (Alpaca positions) + LIVE (PM resolution)  |  KEY: Alpaca required
# ---------------------------------------------------------------------------

@app.route("/api/pnl")
def pnl_report():
    """
    Runs pnl_tracker.py --json and returns P&L for all paper trades.
    Query params:
      ?include_dry_runs=true  — include dry-run trades (default false)
      ?log=path               — path to trade log (default paper_trades.jsonl)
    """
    include_dry = request.args.get("include_dry_runs", "false").lower() == "true"
    log_path    = request.args.get("log", "paper_trades.jsonl")
    script      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pnl_tracker.py")
    cmd = [sys.executable, script, "--json", "--log", log_path]
    if include_dry:
        cmd.append("--include-dry-runs")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if not result.stdout.strip():
            return jsonify({"trades": [], "count": 0, "total_pnl": 0,
                            "source": "pnl_tracker.py"})
        trades    = json.loads(result.stdout)
        total_pnl = round(sum(t.get("stock_pnl", 0) for t in trades), 2)
        return jsonify({
            "source":    "pnl_tracker.py",  # LIVE prices
            "fetched_at": ts(),
            "count":     len(trades),
            "total_pnl": total_pnl,
            "trades":    trades,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "pnl tracker timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "time":   ts(),
        "alpaca_configured": bool(ALPACA_KEY and ALPACA_SECRET),
        "alpaca_url": ALPACA_URL,
    })


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════╗
║       Hedge Dashboard Backend                ║
╠══════════════════════════════════════════════╣
║  http://localhost:{PORT}/api/health           ║
║  http://localhost:{PORT}/api/polymarket       ║
║  http://localhost:{PORT}/api/prices           ║
║  http://localhost:{PORT}/api/account          ║
║  http://localhost:{PORT}/api/positions        ║
╠══════════════════════════════════════════════╣
║  DATA FLAGS                                  ║
║  ✓ Polymarket  — LIVE (no key)               ║
║  ✓ CoinGecko   — LIVE (no key)               ║
║  ~ yfinance    — ~15min delayed (no key)     ║
║  ✓ Alpaca      — LIVE paper (key required)   ║
╚══════════════════════════════════════════════╝
""")
    if not ALPACA_KEY:
        print("⚠  WARNING: ALPACA_API_KEY not set in .env")
        print("   /api/account and /api/order endpoints will fail.\n")

    app.run(host="0.0.0.0", port=PORT, debug=True)
