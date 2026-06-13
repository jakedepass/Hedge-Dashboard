"""
event_stock_mapper.py — Polymarket Event → Stock Ticker Mapper
===============================================================
Pulls live Polymarket events and maps them to correlated stock tickers
based on pattern matching. Outputs ranked hedge opportunities.

How it works:
  1. Fetches live binary markets from your local server.py
  2. Runs each event title through a rule set (FDA, earnings, legal, etc.)
  3. Scores each match by pattern confidence + market volume
  4. Prints a ranked list of hedge opportunities with ticker, direction, edge

Usage:
  python event_stock_mapper.py              # scan and print opportunities
  python event_stock_mapper.py --json       # output as JSON (for dashboard)
  python event_stock_mapper.py --min-vol 50000  # only high-volume markets

Data:
  Polymarket events — LIVE (via server.py → Polymarket Gamma API)
  Stock prices      — LIVE (~15min delayed via server.py → yfinance)
  No API keys needed for this file.

Output columns:
  TICKER   — stock to hedge with
  DIR      — long or short
  CONF     — pattern match confidence (0-1)
  PM PROB  — Polymarket implied probability
  VOLUME   — Polymarket market volume ($)
  EVENT    — Polymarket event title
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from typing import Optional

import requests

SERVER = "http://localhost:5050"

# ---------------------------------------------------------------------------
# Pattern ruleset
# Each rule has:
#   patterns  — list of regex patterns matched against the event title
#   ticker    — stock ticker to hedge with
#   direction — "long" (buy) or "short" (sell) relative to YES resolving
#               e.g. "long" means: if YES resolves, stock goes UP
#   confidence — base confidence score (boosted by volume)
#   category  — human-readable category label
#   note      — why this correlation exists
# ---------------------------------------------------------------------------

RULES = [

    # ── FDA / Biotech ──────────────────────────────────────────────────────
    {
        "patterns": [r"fda.{0,30}approv", r"approv.{0,30}fda"],
        "ticker": "XBI",   # SPDR Biotech ETF (broad biotech exposure)
        "direction": "long",
        "confidence": 0.82,
        "category": "FDA Approval",
        "note": "FDA approvals broadly lift biotech sector (XBI)",
    },
    {
        "patterns": [r"\bpdufa\b", r"pdufa date"],
        "ticker": "XBI",
        "direction": "long",
        "confidence": 0.85,
        "category": "FDA PDUFA",
        "note": "PDUFA dates are binary drug approval events",
    },
    {
        "patterns": [r"\bmrna\b", r"moderna.{0,20}(approv|trial|phase)"],
        "ticker": "MRNA",
        "direction": "long",
        "confidence": 0.91,
        "category": "FDA / Moderna",
        "note": "Moderna-specific drug events",
    },
    {
        "patterns": [r"\bpfizer\b.{0,30}(approv|drug|vaccine|trial)"],
        "ticker": "PFE",
        "direction": "long",
        "confidence": 0.88,
        "category": "FDA / Pfizer",
        "note": "Pfizer drug approval events",
    },
    {
        "patterns": [r"\bbiogen\b"],
        "ticker": "BIIB",
        "direction": "long",
        "confidence": 0.87,
        "category": "FDA / Biogen",
        "note": "Biogen pipeline events",
    },
    {
        "patterns": [r"clinical.{0,20}trial", r"phase (2|3|iii|ii).{0,20}(trial|result|data)"],
        "ticker": "XBI",
        "direction": "long",
        "confidence": 0.72,
        "category": "Clinical Trial",
        "note": "Phase 2/3 trial results move biotech broadly",
    },

    # ── Fed / Interest Rates ───────────────────────────────────────────────
    {
        "patterns": [r"fed.{0,20}(cut|lower|reduc).{0,20}rate",
                     r"rate.{0,20}(cut|lower|reduc).{0,20}fed",
                     r"federal reserve.{0,20}cut"],
        "ticker": "QQQ",
        "direction": "long",
        "confidence": 0.79,
        "category": "Fed Rate Cut",
        "note": "Rate cuts are bullish for growth/tech (QQQ)",
    },
    {
        "patterns": [r"fed.{0,20}(hike|raise|increas).{0,20}rate",
                     r"rate.{0,20}(hike|raise|increas)"],
        "ticker": "QQQ",
        "direction": "short",
        "confidence": 0.76,
        "category": "Fed Rate Hike",
        "note": "Rate hikes are bearish for growth/tech (QQQ)",
    },
    {
        "patterns": [r"fomc", r"federal open market"],
        "ticker": "SPY",
        "direction": "long",
        "confidence": 0.65,
        "category": "FOMC",
        "note": "FOMC decisions move broad market (SPY)",
    },
    {
        "patterns": [r"\bcpi\b.{0,30}(below|under|lower|drop)",
                     r"inflation.{0,30}(below|under|lower|drop|cool)"],
        "ticker": "QQQ",
        "direction": "long",
        "confidence": 0.74,
        "category": "CPI / Inflation",
        "note": "Lower inflation is bullish for growth stocks",
    },
    {
        "patterns": [r"\bcpi\b.{0,30}(above|over|higher|rise|hot)",
                     r"inflation.{0,30}(above|over|higher|rise|surge)"],
        "ticker": "QQQ",
        "direction": "short",
        "confidence": 0.71,
        "category": "CPI / Inflation",
        "note": "Higher inflation is bearish for growth stocks",
    },

    # ── Recession / Economy ────────────────────────────────────────────────
    {
        "patterns": [r"recession", r"gdp.{0,20}(negative|contrac|shrink)",
                     r"economic.{0,20}downturn"],
        "ticker": "SPY",
        "direction": "short",
        "confidence": 0.81,
        "category": "Recession",
        "note": "Recession risk is broadly bearish for equities (SPY)",
    },
    {
        "patterns": [r"unemployment.{0,20}(rise|above|high)",
                     r"job.{0,20}(loss|cut|layoff).{0,20}(million|thousand)"],
        "ticker": "SPY",
        "direction": "short",
        "confidence": 0.68,
        "category": "Jobs / Unemployment",
        "note": "Rising unemployment is bearish for broad market",
    },

    # ── Tech / AI ──────────────────────────────────────────────────────────
    {
        "patterns": [r"\bnvidia\b|\bnvda\b"],
        "ticker": "NVDA",
        "direction": "long",
        "confidence": 0.93,
        "category": "Nvidia",
        "note": "Nvidia-specific events",
    },
    {
        "patterns": [r"\bapple\b.{0,20}(launch|release|iphone|vision|announce)",
                     r"iphone.{0,20}(launch|release|sales)"],
        "ticker": "AAPL",
        "direction": "long",
        "confidence": 0.89,
        "category": "Apple",
        "note": "Apple product events",
    },
    {
        "patterns": [r"\bmicrosoft\b.{0,20}(ai|copilot|azure|deal|acqui)",
                     r"\bmsft\b"],
        "ticker": "MSFT",
        "direction": "long",
        "confidence": 0.87,
        "category": "Microsoft",
        "note": "Microsoft AI/cloud events",
    },
    {
        "patterns": [r"\bmeta\b.{0,20}(ai|vr|ar|llama|advertis|revenue)",
                     r"facebook.{0,20}(revenue|earnings)"],
        "ticker": "META",
        "direction": "long",
        "confidence": 0.86,
        "category": "Meta",
        "note": "Meta AI and ad revenue events",
    },
    {
        "patterns": [r"\bgoogle\b|\balphabet\b|\bgemini\b.{0,20}(launch|release)"],
        "ticker": "GOOGL",
        "direction": "long",
        "confidence": 0.85,
        "category": "Google / Alphabet",
        "note": "Google AI and search events",
    },
    {
        "patterns": [r"ai.{0,20}(regulation|ban|restrict|act\b)",
                     r"artificial intelligence.{0,20}(law|regulat|ban)"],
        "ticker": "QQQ",
        "direction": "short",
        "confidence": 0.69,
        "category": "AI Regulation",
        "note": "AI regulation is bearish for tech-heavy QQQ",
    },

    # ── Legal / Antitrust ──────────────────────────────────────────────────
    {
        "patterns": [r"antitrust.{0,30}(google|alphabet)"],
        "ticker": "GOOGL",
        "direction": "short",
        "confidence": 0.84,
        "category": "Antitrust / Google",
        "note": "Antitrust rulings are bearish for the named company",
    },
    {
        "patterns": [r"antitrust.{0,30}(apple|app store)"],
        "ticker": "AAPL",
        "direction": "short",
        "confidence": 0.83,
        "category": "Antitrust / Apple",
        "note": "App Store antitrust rulings hit Apple revenue",
    },
    {
        "patterns": [r"antitrust.{0,30}(amazon|aws)"],
        "ticker": "AMZN",
        "direction": "short",
        "confidence": 0.82,
        "category": "Antitrust / Amazon",
        "note": "Antitrust actions bearish for Amazon",
    },
    {
        "patterns": [r"antitrust.{0,30}(microsoft|msft)"],
        "ticker": "MSFT",
        "direction": "short",
        "confidence": 0.82,
        "category": "Antitrust / Microsoft",
        "note": "Antitrust actions bearish for Microsoft",
    },
    {
        "patterns": [r"\bsec\b.{0,30}(sue|lawsuit|charge|settle|fine)",
                     r"securities.{0,20}(fraud|violation|charge)"],
        "ticker": "XLF",   # Financial sector ETF
        "direction": "short",
        "confidence": 0.66,
        "category": "SEC Action",
        "note": "SEC enforcement actions are broadly bearish for financials",
    },

    # ── Crypto / ETF ───────────────────────────────────────────────────────
    {
        "patterns": [r"bitcoin.{0,20}etf|btc.{0,20}etf|spot.{0,20}bitcoin"],
        "ticker": "MSTR",  # MicroStrategy — high BTC correlation
        "direction": "long",
        "confidence": 0.83,
        "category": "Bitcoin ETF",
        "note": "Bitcoin ETF approvals are bullish for BTC proxies like MSTR",
    },
    {
        "patterns": [r"ethereum.{0,20}etf|eth.{0,20}etf|spot.{0,20}ethereum"],
        "ticker": "ETHA",  # iShares Ethereum ETF
        "direction": "long",
        "confidence": 0.85,
        "category": "Ethereum ETF",
        "note": "ETH ETF flows directly affect ETHA",
    },
    {
        "patterns": [r"solana.{0,20}etf|sol.{0,20}etf"],
        "ticker": "COIN",  # Coinbase — SOL exposure proxy
        "direction": "long",
        "confidence": 0.71,
        "category": "Solana ETF",
        "note": "Solana ETF approval bullish for crypto exchanges like COIN",
    },
    {
        "patterns": [r"\bcoinbase\b|\bcoin\b.{0,10}(sec|regulat|approv)"],
        "ticker": "COIN",
        "direction": "long",
        "confidence": 0.88,
        "category": "Coinbase",
        "note": "Coinbase-specific regulatory events",
    },

    # ── Energy ─────────────────────────────────────────────────────────────
    {
        "patterns": [r"oil.{0,20}(price|barrel|opec).{0,20}(above|rise|high)",
                     r"opec.{0,20}(cut|produc)"],
        "ticker": "XLE",   # Energy Select Sector ETF
        "direction": "long",
        "confidence": 0.73,
        "category": "Oil / Energy",
        "note": "Higher oil prices lift energy stocks (XLE)",
    },
    {
        "patterns": [r"oil.{0,20}(price|barrel).{0,20}(below|drop|low|crash)",
                     r"opec.{0,20}(increas|flood)"],
        "ticker": "XLE",
        "direction": "short",
        "confidence": 0.71,
        "category": "Oil / Energy",
        "note": "Lower oil prices hurt energy stocks (XLE)",
    },

    # ── Elections — FEDERAL only (mapped to real proxies, not GEO) ────────
    {
        "patterns": [r"(trump|donald trump).{0,40}(win|elected|president).{0,20}(2028|election)",
                     r"(2028).{0,30}(trump|donald trump).{0,30}(win|elect|president)"],
        "ticker": "DJT",   # Trump Media — most direct Trump election proxy
        "direction": "long",
        "confidence": 0.78,
        "category": "Election / Trump 2028",
        "note": "Trump election win is most directly correlated with DJT stock",
    },
    {
        "patterns": [r"republican.{0,30}(control|win|majority).{0,20}(house|senate|congress)",
                     r"(house|senate|congress).{0,30}republican.{0,20}(majority|control)"],
        "ticker": "XLF",   # Financials benefit from Republican deregulation
        "direction": "long",
        "confidence": 0.67,
        "category": "Election / Congress",
        "note": "Republican Congress majority is bullish for financials via deregulation",
    },
    {
        "patterns": [r"democrat.{0,30}(control|win|majority).{0,20}(house|senate|congress)"],
        "ticker": "XLU",   # Utilities benefit from Democrat clean energy policy
        "direction": "long",
        "confidence": 0.62,
        "category": "Election / Congress",
        "note": "Democrat Congress majority is bullish for utilities/clean energy",
    },

    # ── TikTok ─────────────────────────────────────────────────────────────
    {
        "patterns": [r"tiktok.{0,30}(ban|banned|shut)", r"ban.{0,20}tiktok"],
        "ticker": "META",
        "direction": "long",
        "confidence": 0.78,
        "category": "TikTok Ban",
        "note": "TikTok ban routes ad spend to Meta/Instagram",
    },
    {
        "patterns": [r"tiktok.{0,30}(microsoft|msft|acqui|buy|sold)",
                     r"(microsoft|msft).{0,30}(acqui|buy).{0,20}tiktok"],
        "ticker": "MSFT",
        "direction": "long",
        "confidence": 0.82,
        "category": "TikTok / Microsoft",
        "note": "Microsoft acquiring TikTok would add massive user base",
    },
    {
        "patterns": [r"tiktok.{0,30}(musk|x\.com|twitter|elon)"],
        "ticker": "TSLA",
        "direction": "long",
        "confidence": 0.71,
        "category": "TikTok / Musk",
        "note": "Musk acquiring TikTok seen as positive for his empire",
    },

    # ── Tariffs / Trade ────────────────────────────────────────────────────
    {
        "patterns": [r"tariff.{0,30}(china|chinese|beijing)",
                     r"trade.{0,20}war.{0,20}(china|chinese)"],
        "ticker": "FXI",
        "direction": "short",
        "confidence": 0.74,
        "category": "Tariffs / China",
        "note": "US tariffs on China are bearish for Chinese equities (FXI)",
    },
    {
        "patterns": [r"tariff.{0,30}(pause|end|lift|remove|reduce)",
                     r"trade.{0,20}(deal|agreement).{0,20}(china|mexico|canada)"],
        "ticker": "SPY",
        "direction": "long",
        "confidence": 0.71,
        "category": "Tariff Relief",
        "note": "Tariff pauses/deals are bullish for broad market",
    },
    {
        "patterns": [r"tariff.{0,30}(impose|increas|escalat|new)",
                     r"new.{0,20}tariff"],
        "ticker": "SPY",
        "direction": "short",
        "confidence": 0.70,
        "category": "Tariff Escalation",
        "note": "New tariffs create supply chain uncertainty, bearish for SPY",
    },

    # ── Debt / Shutdown ────────────────────────────────────────────────────
    {
        "patterns": [r"debt.{0,20}ceiling", r"government.{0,20}shutdown",
                     r"default.{0,20}(us|united states|treasury)"],
        "ticker": "TLT",
        "direction": "short",
        "confidence": 0.77,
        "category": "Debt / Shutdown",
        "note": "Debt ceiling crises are bearish for treasuries (TLT)",
    },

    # ── Executive / Leadership ─────────────────────────────────────────────
    {
        "patterns": [r"(ceo|chief executive).{0,30}(resign|fire|replac|step down|oust)",
                     r"(resign|fire|replac|step down).{0,30}(ceo|chief executive)"],
        "ticker": "SPY",   # placeholder — ideally matched to company
        "direction": "short",
        "confidence": 0.58,
        "category": "CEO Departure",
        "note": "CEO departures create short-term uncertainty; ideally hedge the specific stock",
    },
    {
        "patterns": [r"elon musk.{0,30}(tesla|resign|ceo|leave|sell)"],
        "ticker": "TSLA",
        "direction": "short",
        "confidence": 0.81,
        "category": "Musk / Tesla",
        "note": "Musk departure uncertainty is bearish for Tesla",
    },
    {
        "patterns": [r"elon musk.{0,30}(twitter|x\.com|doge|government)",
                     r"doge.{0,20}(cut|budget|government)"],
        "ticker": "TSLA",
        "direction": "long",
        "confidence": 0.60,
        "category": "Musk / DOGE",
        "note": "Musk government influence can lift TSLA sentiment",
    },
]


# ---------------------------------------------------------------------------
# Data class for a matched opportunity
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    event_title: str
    event_id: str
    pm_prob: float
    pm_volume: float
    pm_url: str
    ticker: str
    direction: str       # "long" or "short"
    category: str
    note: str
    pattern_conf: float  # rule confidence
    volume_boost: float  # extra confidence from high volume
    total_score: float   # final ranking score
    hedge_action: str    # human-readable: "Buy MRNA" or "Short QQQ"


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

def match_event(event: dict) -> Optional[Opportunity]:
    """
    Runs an event title through all rules and returns the best match,
    or None if no rule fires above threshold.
    """
    title = event.get("title", "").lower()
    prob  = event.get("prob", 0.5)
    vol   = event.get("volume", 0)

    best_rule  = None
    best_score = 0.0

    for rule in RULES:
        for pattern in rule["patterns"]:
            if re.search(pattern, title, re.IGNORECASE):
                # Volume boost: log scale, maxes out at ~0.1 for $1M+ volume
                import math
                vol_boost = min(0.10, math.log10(max(vol, 1)) / 70)
                score = rule["confidence"] + vol_boost
                if score > best_score:
                    best_score = score
                    best_rule  = rule
                    best_vol_boost = vol_boost
                break  # don't double-count patterns in same rule

    if not best_rule or best_score < 0.55:
        return None

    # hedge_action: if YES resolves and direction is long → Buy ticker
    # if YES resolves and direction is short → Short ticker
    action_verb = "Buy" if best_rule["direction"] == "long" else "Short"
    hedge_action = f"{action_verb} {best_rule['ticker']}"

    return Opportunity(
        event_title   = event.get("title", ""),
        event_id      = event.get("id", ""),
        pm_prob       = prob,
        pm_volume     = vol,
        pm_url        = event.get("url", ""),
        ticker        = best_rule["ticker"],
        direction     = best_rule["direction"],
        category      = best_rule["category"],
        note          = best_rule["note"],
        pattern_conf  = best_rule["confidence"],
        volume_boost  = round(best_vol_boost, 4),
        total_score   = round(best_score, 4),
        hedge_action  = hedge_action,
    )


# ---------------------------------------------------------------------------
# Fetch + scan
# ---------------------------------------------------------------------------

def fetch_events(limit: int = 200) -> list[dict]:
    """Fetch live Polymarket events from the local server."""
    try:
        r = requests.get(f"{SERVER}/api/polymarket", params={"limit": limit}, timeout=15)
        r.raise_for_status()
        data = r.json()
        print(f"[polymarket] {data['count']} live markets fetched  ← LIVE DATA", file=__import__("sys").stderr)
        return data.get("markets", [])
    except Exception as e:
        print(f"[error] Could not reach server.py: {e}", file=sys.stderr)
        print(f"        Is server.py running? Try: python3 server.py", file=sys.stderr)
        sys.exit(1)


def scan(min_vol: float = 0, limit: int = 200) -> list[Opportunity]:
    """Fetch events, match them, return sorted opportunities."""
    events = fetch_events(limit)
    opps = []
    for ev in events:
        if ev.get("volume", 0) < min_vol:
            continue
        opp = match_event(ev)
        if opp:
            opps.append(opp)

    # Sort by total_score descending
    opps.sort(key=lambda o: o.total_score, reverse=True)
    return opps


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_opportunities(opps: list[Opportunity]):
    if not opps:
        print("\nNo matches found. Try lowering --min-vol or check server.py is running.\n")
        return

    print(f"\n{'='*90}")
    print(f"  HEDGE OPPORTUNITIES  ({len(opps)} found)")
    print(f"{'='*90}")
    print(f"  {'TICKER':<8} {'DIR':<6} {'CONF':>5}  {'PROB':>5}  {'VOLUME':>12}  {'CATEGORY':<20}  EVENT")
    print(f"  {'-'*8} {'-'*6} {'-'*5}  {'-'*5}  {'-'*12}  {'-'*20}  {'-'*40}")

    for o in opps:
        vol_str = f"${o.pm_volume:>10,.0f}"
        print(
            f"  {o.ticker:<8} {o.direction.upper():<6} {o.total_score:>5.2f}  "
            f"{o.pm_prob:>4.0%}  {vol_str}  {o.category:<20}  "
            f"{o.event_title[:55]}"
        )

    print(f"\n{'='*90}")
    print(f"\n  TOP PICK DETAIL")
    print(f"{'='*90}")
    top = opps[0]
    print(f"  Event:    {top.event_title}")
    print(f"  URL:      {top.pm_url}")
    print(f"  PM prob:  {top.pm_prob:.0%}  (implied by Polymarket)")
    print(f"  Volume:   ${top.pm_volume:,.0f}")
    print(f"  Ticker:   {top.ticker}")
    print(f"  Action:   {top.hedge_action}")
    print(f"  Category: {top.category}")
    print(f"  Why:      {top.note}")
    print(f"  Score:    {top.total_score:.3f} (pattern {top.pattern_conf:.2f} + vol boost {top.volume_boost:.4f})")
    print(f"\n  ⚠  PAPER TRADING ONLY — verify resolution criteria before hedging")
    print(f"{'='*90}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Map Polymarket events to stock hedges")
    ap.add_argument("--min-vol",  type=float, default=0,
                    help="Minimum Polymarket market volume to consider (default: 0)")
    ap.add_argument("--limit",    type=int,   default=200,
                    help="Max Polymarket markets to fetch (default: 200)")
    ap.add_argument("--json",     action="store_true",
                    help="Output as JSON instead of table (for dashboard integration)")
    args = ap.parse_args()

    print(f"\n[scan] Fetching live Polymarket events...", file=__import__("sys").stderr)
    opps = scan(min_vol=args.min_vol, limit=args.limit)

    if args.json:
        print(json.dumps([asdict(o) for o in opps], indent=2))
    else:
        print_opportunities(opps)


if __name__ == "__main__":
    main()
