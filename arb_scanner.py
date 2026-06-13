"""
Prediction Market Arbitrage Scanner (read-only)
================================================
Scans Kalshi and Polymarket for cross-platform mispricings and logs them.
NO execution — this collects data so you can judge whether opportunities
are real, frequent, and large enough to be worth automating.

Usage:
    pip install requests
    python arb_scanner.py                 # one scan pass
    python arb_scanner.py --loop 60       # scan every 60 seconds
    python arb_scanner.py --min-edge 0.02 # only log edges >= 2 cents

Output:
    opportunities.csv  — every detected mispricing with timestamps
    matches_review.csv — candidate market pairs for YOU to manually verify
                         (resolution criteria mismatch is the #1 way arb
                         strategies lose money — always verify by hand)
"""

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"

# Fee assumptions (verify against current fee schedules!)
# Kalshi: trading fee ~ 0.07 * P * (1-P) per contract, rounded up
# Polymarket: no trading fee currently, but ~2% on some markets historically;
#             gas/withdrawal costs exist. Adjust these to be conservative.
KALSHI_FEE_RATE = 0.07
POLY_FEE_FLAT = 0.0

SIMILARITY_THRESHOLD = 0.55   # title similarity to consider a candidate pair
OPP_FILE = "opportunities.csv"
MATCH_FILE = "matches_review.csv"

session = requests.Session()
session.headers.update({"User-Agent": "arb-scanner-research/0.1"})


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_kalshi_markets(max_pages: int = 10) -> list[dict]:
    """Fetch open Kalshi markets via the public API (no auth needed for
    market data). Returns a normalized list of dicts."""
    markets, cursor = [], None
    for _ in range(max_pages):
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = session.get(f"{KALSHI_BASE}/markets", params=params, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[kalshi] fetch error: {e}")
            break
        data = r.json()
        for m in data.get("markets", []):
            # Kalshi prices are in cents (1-99)
            yes_ask = m.get("yes_ask")
            no_ask = m.get("no_ask")
            if not yes_ask or not no_ask:
                continue
            markets.append({
                "platform": "kalshi",
                "id": m.get("ticker"),
                "title": m.get("title") or m.get("ticker", ""),
                "yes_ask": yes_ask / 100.0,
                "no_ask": no_ask / 100.0,
                "volume": m.get("volume", 0),
                "close_time": m.get("close_time", ""),
            })
        cursor = data.get("cursor")
        if not cursor:
            break
    print(f"[kalshi] fetched {len(markets)} open markets")
    return markets


def fetch_polymarket_markets(max_pages: int = 10) -> list[dict]:
    """Fetch active Polymarket markets via the Gamma API."""
    markets = []
    offset = 0
    for _ in range(max_pages):
        params = {"active": "true", "closed": "false", "limit": 100,
                  "offset": offset}
        try:
            r = session.get(f"{POLY_GAMMA}/markets", params=params, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[polymarket] fetch error: {e}")
            break
        data = r.json()
        if not data:
            break
        for m in data:
            # outcomePrices is a JSON-encoded list like '["0.45", "0.55"]'
            try:
                prices = json.loads(m.get("outcomePrices") or "[]")
                outcomes = json.loads(m.get("outcomes") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if len(prices) != 2 or len(outcomes) != 2:
                continue  # only binary markets for arb matching
            # Map to yes/no. Most binary markets are ["Yes","No"].
            try:
                yes_idx = [o.lower() for o in outcomes].index("yes")
            except ValueError:
                yes_idx = 0
            yes_price = float(prices[yes_idx])
            no_price = float(prices[1 - yes_idx])
            if yes_price <= 0 or no_price <= 0:
                continue
            markets.append({
                "platform": "polymarket",
                "id": m.get("conditionId") or m.get("id"),
                "title": m.get("question") or "",
                # Gamma returns midpoint-ish prices; treat as approx asks.
                # For real precision you'd hit the CLOB order book endpoint
                # per market — fine for a scanner, required before trading.
                "yes_ask": yes_price,
                "no_ask": no_price,
                "volume": float(m.get("volumeNum") or 0),
                "close_time": m.get("endDate", ""),
            })
        offset += 100
    print(f"[polymarket] fetched {len(markets)} active binary markets")
    return markets


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

STOPWORDS = {"will", "the", "a", "an", "in", "on", "by", "of", "to", "be",
             "is", "at", "for", "before", "after"}


def normalize_title(t: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    words = [w for w in t.split() if w not in STOPWORDS]
    return " ".join(words)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def find_candidate_pairs(kalshi: list[dict], poly: list[dict]) -> list[tuple]:
    """Pair markets across platforms by title similarity. These are
    CANDIDATES only — resolution criteria must be verified manually."""
    pairs = []
    for k in kalshi:
        best, best_score = None, 0.0
        for p in poly:
            s = similarity(k["title"], p["title"])
            if s > best_score:
                best, best_score = p, s
        if best and best_score >= SIMILARITY_THRESHOLD:
            pairs.append((k, best, round(best_score, 3)))
    print(f"[match] {len(pairs)} candidate pairs above "
          f"{SIMILARITY_THRESHOLD} similarity")
    return pairs


# ---------------------------------------------------------------------------
# Arb math
# ---------------------------------------------------------------------------

def kalshi_fee(price: float) -> float:
    """Approximate Kalshi per-contract fee: 0.07 * P * (1-P), rounded up
    to the next cent. Verify against the current fee schedule."""
    import math
    return math.ceil(KALSHI_FEE_RATE * price * (1 - price) * 100) / 100


def compute_edge(k: dict, p: dict) -> list[dict]:
    """Two arb directions:
       A) Buy YES on Kalshi + buy NO on Polymarket
       B) Buy NO on Kalshi + buy YES on Polymarket
    Edge = $1 payout - total cost - fees. Positive = locked profit
    (IF the markets truly resolve identically)."""
    results = []
    combos = [
        ("kalshi_YES + poly_NO", k["yes_ask"], p["no_ask"], k["yes_ask"]),
        ("kalshi_NO + poly_YES", k["no_ask"], p["yes_ask"], k["no_ask"]),
    ]
    for label, k_price, p_price, k_fee_price in combos:
        cost = k_price + p_price
        fees = kalshi_fee(k_fee_price) + POLY_FEE_FLAT
        edge = 1.0 - cost - fees
        results.append({
            "direction": label,
            "kalshi_price": round(k_price, 3),
            "poly_price": round(p_price, 3),
            "gross_cost": round(cost, 3),
            "est_fees": round(fees, 3),
            "edge_per_contract": round(edge, 4),
        })
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def append_csv(path: str, row: dict):
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


def scan_once(min_edge: float):
    ts = datetime.now(timezone.utc).isoformat()
    kalshi = fetch_kalshi_markets()
    poly = fetch_polymarket_markets()
    if not kalshi or not poly:
        print("[scan] missing data from one platform; skipping pass")
        return

    pairs = find_candidate_pairs(kalshi, poly)
    n_opps = 0
    for k, p, score in pairs:
        append_csv(MATCH_FILE, {
            "timestamp": ts, "similarity": score,
            "kalshi_ticker": k["id"], "kalshi_title": k["title"],
            "poly_id": p["id"], "poly_title": p["title"],
        })
        for r in compute_edge(k, p):
            if r["edge_per_contract"] >= min_edge:
                n_opps += 1
                append_csv(OPP_FILE, {
                    "timestamp": ts, "similarity": score,
                    "kalshi_ticker": k["id"], "kalshi_title": k["title"],
                    "poly_id": p["id"], "poly_title": p["title"],
                    **r,
                    "kalshi_volume": k["volume"],
                    "poly_volume": p["volume"],
                })
                print(f"  [OPP] {r['edge_per_contract']:+.3f}  "
                      f"{r['direction']}  sim={score}  «{k['title'][:60]}»")
    print(f"[scan] pass complete — {n_opps} opportunities >= "
          f"{min_edge:.2f} edge logged to {OPP_FILE}")


def main():
    ap = argparse.ArgumentParser(description="Prediction market arb scanner")
    ap.add_argument("--loop", type=int, default=0,
                    help="rescan every N seconds (0 = single pass)")
    ap.add_argument("--min-edge", type=float, default=0.01,
                    help="minimum edge per contract to log (default $0.01)")
    args = ap.parse_args()

    while True:
        scan_once(args.min_edge)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
