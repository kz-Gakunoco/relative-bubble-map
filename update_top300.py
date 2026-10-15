"""Independent top-300 US company universe and snapshot pipeline."""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

import update as core

ROOT = Path(__file__).resolve().parent
UNIVERSE_FILE = ROOT / "config/universe_top300.csv"
MEMBERSHIP_FILE = ROOT / "config/top300_membership.json"
SECTORS = {
    "Technology": ("情報技術", "XLK"),
    "Telecommunications": ("コミュニケーション・サービス", "XLC"),
    "Consumer Discretionary": ("一般消費財", "XLY"),
    "Consumer Staples": ("生活必需品", "XLP"),
    "Finance": ("金融", "XLF"),
    "Health Care": ("ヘルスケア", "XLV"),
    "Industrials": ("資本財・産業", "XLI"),
    "Energy": ("エネルギー", "XLE"),
    "Basic Materials": ("素材", "XLB"),
    "Utilities": ("公益", "XLU"),
    "Real Estate": ("不動産", "XLRE"),
}
PREFERRED_CLASS = {"GOOG": "GOOGL", "BRK.A": "BRK.B", "FOX": "FOXA", "NWS": "NWSA"}


def issuer_key(name):
    name = re.split(r"\b(?:class|series|common stock|common shares|ordinary shares|capital stock)\b", name, flags=re.I)[0]
    return re.sub(r"[^a-z0-9]", "", name.lower())


def choose_universe(rows, limit=300):
    candidates = []
    for r in rows:
        if str(r.get("country", "")).strip().lower() != "united states":
            continue
        name = str(r.get("name", ""))
        symbol = str(r.get("symbol", "")).strip().replace("/", ".")
        if not re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", symbol):
            continue
        if re.search(r"\b(?:preferred|depositary|depository|warrants?|rights|units?|ETF|ETN|exchange.traded|closed.end|notes|debentures|bonds|convertible|ZONES|limited partnership)\b|\bL\.?P\.?\b", name, re.I):
            continue
        try:
            cap = float(str(r.get("marketCap", "")).replace(",", "").replace("$", ""))
        except ValueError:
            continue
        if not math.isfinite(cap) or cap <= 0:
            continue
        candidates.append(dict(r, ticker=symbol, market_cap=cap, issuer=issuer_key(name)))
    candidates.sort(key=lambda r: (-r["market_cap"], r["ticker"]))
    available = {r["ticker"]: r for r in candidates}
    selected, seen = [], set()
    for r in candidates:
        representative = available.get(PREFERRED_CLASS.get(r["ticker"]), r)
        key = r["issuer"]
        if key in seen or representative["ticker"] in {s["ticker"] for s in selected}:
            continue
        seen.add(key)
        selected.append(dict(representative, market_cap=r["market_cap"]))
        if len(selected) == limit:
            break
    if len(selected) != limit:
        raise RuntimeError(f"Expected {limit} US companies; received {len(selected)}")
    result = []
    # Preserve the existing, curated sector/industry ETF assignments where known.
    curated = pd.read_csv(ROOT / "config/universe.csv", encoding="utf-8-sig").set_index("ticker")
    for rank, r in enumerate(selected, 1):
        industry = str(r.get("industry", ""))
        sector, etf = SECTORS.get(r.get("sector"), (None, None))
        if "real estate" in industry.lower() or "reit" in industry.lower():
            sector, etf = "不動産", "XLRE"
        if re.search(r"semiconductor", industry, re.I):
            sector, etf = "情報技術", "SOXX"
        if r["ticker"] in {"GOOG", "GOOGL", "META", "NFLX", "DIS", "CMCSA", "CHTR", "EA", "TTWO", "WBD", "FOXA", "NWSA", "OMC", "IPG", "LYV", "TKO", "PINS", "SNAP", "RDDT", "SPOT"}:
            sector, etf = "コミュニケーション・サービス", "XLC"
        if r["ticker"] in curated.index:
            c = curated.loc[r["ticker"]]
            sector, etf = c.sector, c.benchmark
        if not sector:
            raise RuntimeError(f"Unmapped sector: {r['ticker']} {r.get('sector')}")
        result.append({"ticker": r["ticker"], "company": r["name"], "sector": sector,
                       "theme": industry or str(r.get("sector", "")), "benchmark": etf,
                       "provider_ticker": r["ticker"].replace(".", "-"),
                       "market_cap_rank": rank, "selection_market_cap": r["market_cap"]})
    return pd.DataFrame(result)


def fetch_membership():
    response = requests.get("https://api.nasdaq.com/api/screener/stocks",
        params={"tableonly": "true", "limit": 25000, "download": "true"},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*",
                 "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    rows = data.get("rows") or (data.get("table") or {}).get("rows") or []
    total = int(str(data.get("totalrecords") or len(rows)).replace(",", ""))
    if total > len(rows):
        raise RuntimeError(f"Incomplete screener response: {len(rows)}/{total}")
    selected = choose_universe(rows)
    metadata = {"source": "Nasdaq Stock Screener", "source_url": "https://www.nasdaq.com/market-activity/stocks/screener",
                "selected_at_utc": datetime.now(timezone.utc).isoformat(), "company_count": len(selected),
                "scope": "US-domiciled common equities; ETFs/ADRs excluded; one representative share class",
                "benchmark_policy": "Curated assignments where available; otherwise sector ETF, semiconductors SOXX"}
    return selected, metadata


def relax_recent_listing_errors(health, prices):
    """Keep recent IPOs without suppressing missing returns in older stocks."""
    counts = prices.dropna(subset=["adj_close"]).groupby("ticker")["date"].nunique()
    revised = []
    for error in health["errors"]:
        if error.startswith("missing_ret_20D="):
            missing = error.split("=", 1)[1].split(",")
            young = [t for t in missing if 0 < counts.get(t, 0) <= 20]
            older = [t for t in missing if t not in young]
            if young:
                health["warnings"].append("recent_listing_ret_20D_unavailable=" + ",".join(young))
            if older:
                revised.append("missing_ret_20D=" + ",".join(older))
        else:
            revised.append(error)
    health["errors"] = revised
    health["ok"] = not revised
    return health


def main():
    core.DATA = ROOT / "data/top300"
    core.DATA.mkdir(parents=True, exist_ok=True)
    core.PRICE_FILE = core.DATA / "prices.parquet"
    core.META_FILE = core.DATA / "meta.csv"
    core.STATUS_FILE = core.DATA / "status.json"
    core.HISTORY_FILE = core.DATA / "signal_history.csv"
    core.SNAPSHOT_FILE = ROOT / "docs/snapshot_top300.json"
    try:
        selected, membership = fetch_membership()
        # Keep membership and its price snapshot consistent; publish config only on success.
        core.universe = selected.drop(columns=["selection_market_cap"])
        # The screener supplies current capitalization without 300 per-symbol metadata calls.
        pd.DataFrame({"ticker": selected.ticker, "market_cap": selected.selection_market_cap,
                      "meta_updated_at": membership["selected_at_utc"]}).to_csv(core.META_FILE, index=False)
        core.meta_is_stale = lambda: False
        # Keep this pipeline on Yahoo even if a limited FMP key is later added.
        core.settings["provider"] = "yfinance"
        if not core.PRICE_FILE.exists() and (ROOT / "data/prices.parquet").exists():
            core.save_prices(pd.read_parquet(ROOT / "data/prices.parquet"))
        original = core.yfinance_prices

        def batched(symbols, start, end):
            items = sorted(symbols.items())
            parts = []
            for offset in range(0, len(items), 25):
                batch = dict(items[offset:offset + 25])
                for attempt in range(3):
                    try:
                        parts.append(original(batch, start, end))
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(3 * (attempt + 1))
                time.sleep(1)
            return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

        core.yfinance_prices = batched
        build = core.build_snapshot

        def with_membership(prices, meta):
            snapshot = build(prices, meta)
            snapshot["universe"] = {"id": "top300", "name": "米国時価総額上位300社", **membership}
            return snapshot

        core.build_snapshot = with_membership
        validate = core.validate_snapshot

        def allow_recent_listings(snapshot, prices, meta):
            health = validate(snapshot, prices, meta)
            # Recent IPOs legitimately lack long-window returns. Keep all 300
            # companies and report gaps, without masking failures in older names.
            return relax_recent_listing_errors(health, prices)

        core.validate_snapshot = allow_recent_listings
        core.main()
        selected.to_csv(UNIVERSE_FILE, index=False, encoding="utf-8")
        MEMBERSHIP_FILE.write_text(json.dumps(membership, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        # Membership retrieval failures must not leave a previous success status behind.
        core.STATUS_FILE.write_text(json.dumps({"ok": False, "published": False,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "health_errors": [f"{type(exc).__name__}: {exc}" ]}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()

