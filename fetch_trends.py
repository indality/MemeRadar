#!/usr/bin/env python3
"""
memecoin trend fetcher
-----------------------
Pulls "trending / newly active" Solana memecoin data from two public sources:

  1. pump.fun frontend API (undocumented, but widely used by trackers)
     - new coins:        https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&sort=market_cap&order=DESC
     - king of the hill:  https://frontend-api-v3.pump.fun/coins/king-of-the-hill
  2. DexScreener public API (documented, CORS-friendly)
     - boosted/trending: https://api.dexscreener.com/token-boosts/top/v1
     - token profiles:   https://api.dexscreener.com/token-profiles/latest/v1
     - pair details:     https://api.dexscreener.com/latest/dex/tokens/{address}

NOTE: pump.fun's API is not officially documented and endpoints/response shapes
can change without notice. If a request fails, check https://pump.fun in a
browser dev tools Network tab for the current endpoint and adjust PUMP_API_BASE.

This script does NOT execute any trades. It only reads public data, scores it,
and writes results to disk (data.json + a timestamped CSV log) so you can build
a dashboard on top of it or just eyeball the output.

Usage:
    pip install requests rich --break-system-packages
    python fetch_trends.py            # single run
    python fetch_trends.py --loop 60  # loop every 60 seconds
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

try:
    from rich.console import Console
    from rich.table import Table
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False

PUMP_API_BASE = "https://frontend-api-v3.pump.fun"
DEXSCREENER_BASE = "https://api.dexscreener.com"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"

# Only the top N candidates (by momentum score) get a RugCheck lookup, since
# RugCheck's free tier is rate-limited (~1 req/sec without an API key) and we'd
# rather move fast on the tokens that actually matter than burn the quota on
# everything.
RUGCHECK_TOP_N = 20
RUGCHECK_REQUEST_DELAY_SEC = 1.1

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (trend-dashboard-script)",
    "Accept": "application/json",
}

# ---- Filters: tune these to your risk tolerance -------------------------
MIN_LIQUIDITY_USD = 5_000      # ignore pools with less than this liquidity
MIN_VOLUME_24H_USD = 10_000    # ignore tokens with less than this 24h volume
MAX_AGE_HOURS = 48              # only look at tokens created in the last N hours
# ---------------------------------------------------------------------------


def safe_get(url, params=None, timeout=10):
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[warn] request failed for {url}: {e}", file=sys.stderr)
        return None


def fetch_pump_new_coins(limit=50):
    """Recently created coins on pump.fun, sorted by market cap."""
    data = safe_get(
        f"{PUMP_API_BASE}/coins",
        params={"offset": 0, "limit": limit, "sort": "market_cap", "order": "DESC", "includeNsfw": "false"},
    )
    return data or []


def fetch_pump_king_of_the_hill():
    """The current 'king of the hill' token (highest momentum on pump.fun)."""
    data = safe_get(f"{PUMP_API_BASE}/coins/king-of-the-hill")
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def fetch_dexscreener_boosted():
    """Tokens that have paid for a DexScreener boost (signals active promotion)."""
    data = safe_get(f"{DEXSCREENER_BASE}/token-boosts/top/v1")
    if not data:
        return []
    return [d for d in data if d.get("chainId") == "solana"]


def fetch_dexscreener_pair(token_address):
    """Pull live price/volume/liquidity for a given token address."""
    data = safe_get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}")
    if not data or "pairs" not in data or not data["pairs"]:
        return None
    # Take the pair with the highest liquidity
    pairs = sorted(data["pairs"], key=lambda p: (p.get("liquidity") or {}).get("usd", 0), reverse=True)
    return pairs[0]


def fetch_rugcheck_report(token_address):
    """
    Pull RugCheck's full risk report for a token: LP lock status, mint/freeze
    authority, top holder concentration, and their overall risk label.

    NOTE: we use the full `/report` endpoint (not `/report/summary`) because
    the summary endpoint omits `markets` (LP lock data) and `topHolders`
    (concentration data) entirely - confirmed by inspecting a live response.

    Docs: https://api.rugcheck.xyz/swagger/index.html
    Returns None if the token hasn't been indexed yet or the request fails.
    """
    data = safe_get(f"{RUGCHECK_BASE}/tokens/{token_address}/report")
    if not data:
        return None

    top_holders = data.get("topHolders") or []
    # Some RugCheck entries list the same owner more than once (e.g. separate
    # token accounts for the same wallet), which can push a naive sum over
    # 100%. Deduplicate by address first, then take the true top 10.
    unique_holders = {}
    for h in top_holders:
        addr = h.get("address") or h.get("owner")
        pct = h.get("pct") or 0
        if addr and pct > unique_holders.get(addr, 0):
            unique_holders[addr] = pct
    top10_vals = sorted(unique_holders.values(), reverse=True)[:10]
    top10_pct = sum(top10_vals) if top10_vals else None
    if top10_pct is not None:
        top10_pct = min(top10_pct, 100)  # safety clamp, holdings can't exceed total supply

    lp_locked_pct = None
    markets = data.get("markets") or []
    if markets:
        lp_pcts = [m.get("lp", {}).get("lpLockedPct") for m in markets if m.get("lp")]
        lp_pcts = [p for p in lp_pcts if p is not None]
        if lp_pcts:
            lp_locked_pct = max(lp_pcts)

    risks = data.get("risks") or []

    return {
        # score_normalised is RugCheck's 0-100 scale (higher = riskier).
        # Plain `score` is an unbounded raw score and not comparable across tokens.
        "risk_score": data.get("score_normalised"),
        "risk_score_raw": data.get("score"),
        "risk_label": risks[0].get("level") if risks else None,
        "risks_detail": [r.get("name") for r in risks],
        "mint_authority_renounced": data.get("mintAuthority") is None,
        "freeze_authority_renounced": data.get("freezeAuthority") is None,
        "lp_locked_pct": lp_locked_pct,
        "holder_count": data.get("totalHolders"),
        "top10_holder_pct": round(top10_pct, 1) if top10_pct is not None else None,
    }


def enrich_with_rugcheck(rows, top_n=RUGCHECK_TOP_N):
    """Attach RugCheck risk data to the top N rows by momentum score, in place."""
    candidates = sorted(rows, key=lambda r: r.get("score", 0), reverse=True)[:top_n]
    for r in candidates:
        addr = r.get("address")
        if not addr:
            continue
        rc = fetch_rugcheck_report(addr)
        if rc:
            r.update(rc)
        time.sleep(RUGCHECK_REQUEST_DELAY_SEC)  # stay under RugCheck's free-tier rate limit
    return rows


def normalize_pump_coin(coin):
    """Map a pump.fun coin record into our common schema."""
    created_ts = coin.get("created_timestamp")
    age_hours = None
    if created_ts:
        age_hours = (time.time() * 1000 - created_ts) / 1000 / 3600 if created_ts > 1e12 else (time.time() - created_ts) / 3600

    return {
        "source": "pump.fun",
        "symbol": coin.get("symbol"),
        "name": coin.get("name"),
        "address": coin.get("mint"),
        "market_cap_usd": coin.get("usd_market_cap") or coin.get("market_cap"),
        "created_age_hours": age_hours,
        "reply_count": coin.get("reply_count"),
        "raw": coin,
    }


def normalize_dexscreener_pair(pair):
    liq = (pair.get("liquidity") or {}).get("usd")
    vol24 = (pair.get("volume") or {}).get("h24")
    price_change_24h = (pair.get("priceChange") or {}).get("h24")
    created_ms = pair.get("pairCreatedAt")
    age_hours = (time.time() * 1000 - created_ms) / 1000 / 3600 if created_ms else None
    # marketCap = circulating supply value; fdv = fully diluted value (all supply).
    # For pump.fun-style tokens nearly all supply is usually already circulating,
    # so the two are close, but we prefer marketCap when DexScreener provides it.
    market_cap = pair.get("marketCap") or pair.get("fdv")

    return {
        "source": "dexscreener",
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "name": (pair.get("baseToken") or {}).get("name"),
        "address": (pair.get("baseToken") or {}).get("address"),
        "price_usd": pair.get("priceUsd"),
        "market_cap_usd": market_cap,
        "liquidity_usd": liq,
        "volume_24h_usd": vol24,
        "price_change_24h_pct": price_change_24h,
        "created_age_hours": age_hours,
        "url": pair.get("url"),
        "raw": pair,
    }


def score_token(t):
    """
    Simple momentum score: rewards high volume relative to liquidity
    (churn/interest) and recency, penalizes very old or illiquid tokens.
    This is a heuristic for surfacing candidates to *investigate further*,
    not a signal to buy.
    """
    liq = t.get("liquidity_usd") or 0
    vol = t.get("volume_24h_usd") or 0
    age = t.get("created_age_hours")

    if liq < MIN_LIQUIDITY_USD or vol < MIN_VOLUME_24H_USD:
        return 0

    vol_to_liq = vol / liq if liq else 0
    recency_boost = 1.0
    if age is not None:
        if age > MAX_AGE_HOURS:
            return 0
        recency_boost = max(0.2, 1 - (age / MAX_AGE_HOURS))

    return round(vol_to_liq * recency_boost, 3)


def build_report():
    rows = []

    # --- pump.fun sources ---
    pump_coins = fetch_pump_new_coins(limit=50)
    print(f"[debug] pump.fun /coins returned {len(pump_coins)} coins")

    pump_with_address = 0
    pump_with_dex_pair = 0
    for coin in pump_coins:
        n = normalize_pump_coin(coin)
        if n["address"]:
            pump_with_address += 1
            pair = fetch_dexscreener_pair(n["address"])
            if pair:
                pump_with_dex_pair += 1
                merged = normalize_dexscreener_pair(pair)
                merged["source"] = "pump.fun+dexscreener"
                merged["pump_market_cap_usd"] = n["market_cap_usd"]
                merged["pump_reply_count"] = n["reply_count"]
                rows.append(merged)
    print(f"[debug] pump.fun coins with address: {pump_with_address}, "
          f"found on dexscreener: {pump_with_dex_pair}")

    for coin in fetch_pump_king_of_the_hill():
        n = normalize_pump_coin(coin)
        if n["address"] and not any(r["address"] == n["address"] for r in rows):
            pair = fetch_dexscreener_pair(n["address"])
            if pair:
                merged = normalize_dexscreener_pair(pair)
                merged["source"] = "pump.fun-koth+dexscreener"
                rows.append(merged)

    # --- dexscreener boosted (promoted) tokens ---
    boosted = fetch_dexscreener_boosted()
    print(f"[debug] dexscreener boosted (solana): {len(boosted)}")
    for boost in boosted:
        addr = boost.get("tokenAddress")
        if addr and not any(r["address"] == addr for r in rows):
            pair = fetch_dexscreener_pair(addr)
            if pair:
                merged = normalize_dexscreener_pair(pair)
                merged["source"] = "dexscreener-boosted"
                rows.append(merged)

    print(f"[debug] total merged rows before scoring: {len(rows)}")

    for r in rows:
        r["score"] = score_token(r)

    scored_zero = sum(1 for r in rows if not r["score"])
    print(f"[debug] rows filtered out by score (liquidity/volume/age thresholds): {scored_zero}")

    rows = [r for r in rows if r["score"] and r["score"] > 0]
    rows.sort(key=lambda r: r["score"], reverse=True)

    print(f"[info] running RugCheck on top {min(RUGCHECK_TOP_N, len(rows))} candidates...")
    rows = enrich_with_rugcheck(rows)

    return rows


def save_outputs(rows):
    timestamp = datetime.now(timezone.utc).isoformat()

    json_path = os.path.join(DATA_DIR, "latest.json")
    with open(json_path, "w") as f:
        json.dump({"generated_at": timestamp, "results": rows}, f, indent=2, default=str)

    csv_path = os.path.join(DATA_DIR, f"log_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv")
    file_exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "symbol", "address", "source", "market_cap_usd", "price_usd",
                              "liquidity_usd", "volume_24h_usd", "price_change_24h_pct",
                              "created_age_hours", "score", "risk_score", "risk_label",
                              "mint_authority_renounced", "freeze_authority_renounced",
                              "lp_locked_pct", "holder_count", "top10_holder_pct"])
        for r in rows:
            writer.writerow([timestamp, r.get("symbol"), r.get("address"), r.get("source"),
                              r.get("market_cap_usd"), r.get("price_usd"), r.get("liquidity_usd"), r.get("volume_24h_usd"),
                              r.get("price_change_24h_pct"), r.get("created_age_hours"), r.get("score"),
                              r.get("risk_score"), r.get("risk_label"),
                              r.get("mint_authority_renounced"), r.get("freeze_authority_renounced"),
                              r.get("lp_locked_pct"), r.get("holder_count"), r.get("top10_holder_pct")])

    print(f"[ok] saved {len(rows)} rows -> {json_path} and {csv_path}")


def print_table(rows, limit=20):
    rows = rows[:limit]
    if RICH_AVAILABLE:
        table = Table(title="Top trending candidates (heuristic score, NOT financial advice)")
        table.add_column("Symbol")
        table.add_column("Market Cap $")
        table.add_column("Liquidity $")
        table.add_column("Vol 24h $")
        table.add_column("24h %")
        table.add_column("Age (h)")
        table.add_column("Score")
        table.add_column("Risk")
        table.add_column("LP lock%")
        table.add_column("Top10 hold%")
        table.add_column("Source")
        for r in rows:
            risk_str = "—"
            if r.get("risk_score") is not None:
                mint_ok = "✓" if r.get("mint_authority_renounced") else "✗"
                risk_str = f"{r.get('risk_score')} (mint:{mint_ok})"
            table.add_row(
                str(r.get("symbol")),
                f"{r.get('market_cap_usd') or 0:,.0f}",
                f"{r.get('liquidity_usd') or 0:,.0f}",
                f"{r.get('volume_24h_usd') or 0:,.0f}",
                f"{r.get('price_change_24h_pct') or 0:.1f}%",
                f"{(r.get('created_age_hours') or 0):.1f}",
                str(r.get("score")),
                risk_str,
                f"{r.get('lp_locked_pct')}" if r.get("lp_locked_pct") is not None else "—",
                f"{r.get('top10_holder_pct')}" if r.get("top10_holder_pct") is not None else "—",
                str(r.get("source")),
            )
        console.print(table)
    else:
        for r in rows:
            print(f"{r.get('symbol'):>10} | score={r.get('score'):>6} | "
                  f"vol24h=${r.get('volume_24h_usd', 0):,.0f} | liq=${r.get('liquidity_usd', 0):,.0f} | "
                  f"age={r.get('created_age_hours', 0):.1f}h | {r.get('source')}")


def run_once():
    print(f"[info] fetching at {datetime.now(timezone.utc).isoformat()} ...")
    rows = build_report()
    print_table(rows)
    save_outputs(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0, help="Seconds between runs. 0 = run once and exit.")
    args = parser.parse_args()

    if args.loop <= 0:
        run_once()
    else:
        while True:
            run_once()
            print(f"[info] sleeping {args.loop}s...\n")
            time.sleep(args.loop)


if __name__ == "__main__":
    main()
