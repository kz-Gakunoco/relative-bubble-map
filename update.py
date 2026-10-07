from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent
CFG = ROOT / "config"
DATA = ROOT / "data"
DOCS = ROOT / "docs"
DATA.mkdir(exist_ok=True)
DOCS.mkdir(exist_ok=True)

settings = json.loads((CFG / "settings.json").read_text(encoding="utf-8"))
universe = pd.read_csv(CFG / "universe.csv", encoding="utf-8-sig")

PRICE_FILE = DATA / "prices.parquet"
META_FILE = DATA / "meta.csv"
STATUS_FILE = DATA / "status.json"
SNAPSHOT_FILE = DOCS / "snapshot.json"

PERIODS = {
    "1D": 1,
    "5D": 5,
    "20D": 20,
    "60D": 60,
    "3M": 63,
    "6M": 126,
    "12M": 252,
}

@dataclass
class RequestBudget:
    limit: int
    used: int = 0
    def consume(self, n: int = 1):
        if self.used + n > self.limit:
            raise RuntimeError(f"API request budget exceeded: {self.used+n}>{self.limit}")
        self.used += n

budget = RequestBudget(int(settings.get("max_requests_per_run", 180)))


def load_prices() -> pd.DataFrame:
    if PRICE_FILE.exists():
        df = pd.read_parquet(PRICE_FILE)
        df["date"] = pd.to_datetime(df["date"])
        return df
    return pd.DataFrame(columns=["date","ticker","open","high","low","close","adj_close","volume"])


def save_prices(df: pd.DataFrame):
    df = df.drop_duplicates(["date","ticker"], keep="last").sort_values(["ticker","date"])
    df.to_parquet(PRICE_FILE, index=False)


def yfinance_prices(symbols: Dict[str,str], start: str, end: str) -> pd.DataFrame:
    # One batch download. yfinance may perform multiple upstream calls internally,
    # but this keeps our own update logic compact and resilient.
    provider_list = list(symbols.values())
    raw = yf.download(provider_list, start=start, end=end, auto_adjust=False,
                      group_by="ticker", threads=True, progress=False)
    out = []
    reverse = {v:k for k,v in symbols.items()}
    if len(provider_list) == 1:
        raw = pd.concat({provider_list[0]: raw}, axis=1)
    for p_ticker in provider_list:
        if p_ticker not in raw.columns.get_level_values(0):
            continue
        x = raw[p_ticker].copy().reset_index()
        if x.empty:
            continue
        x.columns = [str(c).lower().replace(' ', '_') for c in x.columns]
        x["ticker"] = reverse[p_ticker]
        x = x.rename(columns={"adj_close":"adj_close"})
        if "adj_close" not in x.columns:
            x["adj_close"] = x.get("close")
        out.append(x[["date","ticker","open","high","low","close","adj_close","volume"]])
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def fmp_prices(symbols: Dict[str,str], start: str, end: str, api_key: str) -> pd.DataFrame:
    # FMP stable EOD endpoint. One request per symbol; date range is fetched in one call.
    out = []
    base = "https://financialmodelingprep.com/stable/historical-price-eod/full"
    for display, provider in symbols.items():
        budget.consume(1)
        r = requests.get(base, params={"symbol":provider, "from":start, "to":end, "apikey":api_key}, timeout=30)
        if r.status_code == 429:
            raise RuntimeError("FMP rate limit reached (HTTP 429)")
        r.raise_for_status()
        payload = r.json()
        records = payload if isinstance(payload, list) else payload.get("historical", [])
        if not records:
            continue
        x = pd.DataFrame(records)
        ren = {"adjClose":"adj_close"}
        x = x.rename(columns=ren)
        x["date"] = pd.to_datetime(x["date"])
        x["ticker"] = display
        if "adj_close" not in x:
            x["adj_close"] = x["close"]
        out.append(x[["date","ticker","open","high","low","close","adj_close","volume"]])
        time.sleep(0.06)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def refresh_meta_yf(universe: pd.DataFrame, old: pd.DataFrame) -> pd.DataFrame:
    old_map = old.set_index("ticker").to_dict("index") if not old.empty else {}
    rows = []
    for _, r in universe.iterrows():
        ticker, provider = r.ticker, r.provider_ticker
        prev = old_map.get(ticker, {})
        market_cap = prev.get("market_cap", np.nan)
        try:
            fi = yf.Ticker(provider).fast_info
            v = fi.get("market_cap") if hasattr(fi, "get") else None
            if v:
                market_cap = float(v)
        except Exception:
            pass
        rows.append({
            "ticker": ticker,
            "market_cap": market_cap,
            "meta_updated_at": datetime.now(timezone.utc).isoformat()
        })
    return pd.DataFrame(rows)


def refresh_meta_fmp(universe: pd.DataFrame, old: pd.DataFrame, api_key: str) -> pd.DataFrame:
    old_map = old.set_index("ticker").to_dict("index") if not old.empty else {}
    rows = []
    base = "https://financialmodelingprep.com/stable/profile"
    for _, r in universe.iterrows():
        prev = old_map.get(r.ticker, {})
        market_cap = prev.get("market_cap", np.nan)
        try:
            budget.consume(1)
            resp = requests.get(base, params={"symbol":r.provider_ticker,"apikey":api_key}, timeout=30)
            if resp.status_code == 429:
                raise RuntimeError("FMP rate limit reached (HTTP 429)")
            resp.raise_for_status()
            arr = resp.json()
            if isinstance(arr, list) and arr:
                market_cap = float(arr[0].get("marketCap") or market_cap)
        except RuntimeError:
            raise
        except Exception:
            pass
        rows.append({"ticker":r.ticker,"market_cap":market_cap,
                     "meta_updated_at":datetime.now(timezone.utc).isoformat()})
        time.sleep(0.06)
    return pd.DataFrame(rows)


def meta_is_stale() -> bool:
    if not META_FILE.exists():
        return True
    mtime = datetime.fromtimestamp(META_FILE.stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc) - mtime > timedelta(days=int(settings.get("meta_refresh_days", 7)))


def calc_return(s: pd.Series, n: int) -> float:
    s = s.dropna()
    if len(s) <= n:
        return np.nan
    return float(s.iloc[-1] / s.iloc[-1-n] - 1.0)


def calc_ytd(s: pd.Series, dates: pd.Series) -> float:
    if s.empty:
        return np.nan
    df = pd.DataFrame({"d":dates, "p":s}).dropna()
    if df.empty:
        return np.nan
    year = df.d.iloc[-1].year
    y = df[df.d.dt.year == year]
    if len(y) < 2:
        return np.nan
    return float(y.p.iloc[-1] / y.p.iloc[0] - 1.0)


def build_snapshot(prices: pd.DataFrame, meta: pd.DataFrame) -> dict:
    bench_map = universe.set_index("ticker")["benchmark"].to_dict()
    all_needed = sorted(set(universe.ticker) | set(universe.benchmark))
    px = prices[prices.ticker.isin(all_needed)].copy()
    latest_date = px.date.max()
    metrics = {}
    series = {t:g.sort_values("date") for t,g in px.groupby("ticker")}
    for _, u in universe.iterrows():
        g = series.get(u.ticker)
        b = series.get(u.benchmark)
        if g is None or g.empty:
            continue
        p = g.adj_close.astype(float)
        item = {}
        for label,n in PERIODS.items():
            r = calc_return(p,n)
            br = calc_return(b.adj_close.astype(float),n) if b is not None and not b.empty else np.nan
            item[f"ret_{label}"] = None if pd.isna(r) else r
            item[f"excess_{label}"] = None if pd.isna(r) or pd.isna(br) else r-br
        ytd = calc_ytd(p,g.date)
        bytd = calc_ytd(b.adj_close.astype(float),b.date) if b is not None and not b.empty else np.nan
        item["ret_YTD"] = None if pd.isna(ytd) else ytd
        item["excess_YTD"] = None if pd.isna(ytd) or pd.isna(bytd) else ytd-bytd
        item["last_close"] = float(g.close.iloc[-1])
        item["last_date"] = g.date.iloc[-1].strftime("%Y-%m-%d")
        metrics[u.ticker] = item
    meta_map = meta.set_index("ticker")["market_cap"].to_dict() if not meta.empty else {}
    records = []
    for _,u in universe.iterrows():
        m = metrics.get(u.ticker)
        if not m: continue
        rec = u.to_dict()
        rec.update(m)
        mc = meta_map.get(u.ticker, np.nan)
        rec["market_cap"] = None if pd.isna(mc) else float(mc)
        records.append(rec)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "latest_market_date": None if pd.isna(latest_date) else pd.Timestamp(latest_date).strftime("%Y-%m-%d"),
        "request_budget_used": budget.used,
        "records": records
    }


def main():
    old = load_prices()
    all_symbols = pd.DataFrame({
        "ticker": list(dict.fromkeys(list(universe.ticker)+list(universe.benchmark))),
    })
    provider_lookup = dict(zip(universe.ticker, universe.provider_ticker))
    # ETF provider tickers equal their display tickers.
    for b in universe.benchmark.unique(): provider_lookup[b] = b

    if old.empty:
        start_dt = datetime.now(timezone.utc).date() - timedelta(days=365*int(settings.get("history_years",3))+10)
    else:
        # Re-fetch a short overlap window so late corrections do not create gaps.
        start_dt = pd.Timestamp(old.date.max()).date() - timedelta(days=10)
    end_dt = datetime.now(timezone.utc).date() + timedelta(days=1)

    provider_setting = settings.get("provider","auto").lower()
    api_key = os.getenv("FMP_API_KEY", "").strip()
    provider = "fmp" if (provider_setting == "fmp" or (provider_setting == "auto" and api_key)) else "yfinance"

    symbols = {t:provider_lookup[t] for t in all_symbols.ticker}
    try:
        if provider == "fmp":
            fresh = fmp_prices(symbols, str(start_dt), str(end_dt), api_key)
        else:
            fresh = yfinance_prices(symbols, str(start_dt), str(end_dt))
        if not fresh.empty:
            old = pd.concat([old, fresh], ignore_index=True)
            save_prices(old)
    except Exception as e:
        # Preserve prior data; never destroy a working snapshot because an API failed.
        print(f"PRICE UPDATE WARNING: {e}")

    old_meta = pd.read_csv(META_FILE) if META_FILE.exists() else pd.DataFrame()
    if meta_is_stale():
        try:
            if provider == "fmp":
                meta = refresh_meta_fmp(universe, old_meta, api_key)
            else:
                meta = refresh_meta_yf(universe, old_meta)
            meta.to_csv(META_FILE, index=False)
        except Exception as e:
            print(f"META UPDATE WARNING: {e}")
            meta = old_meta
    else:
        meta = old_meta

    prices = load_prices()
    snapshot = build_snapshot(prices, meta)
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    status = {
        "ok": True,
        "provider": provider,
        "generated_at_utc": snapshot["generated_at_utc"],
        "latest_market_date": snapshot["latest_market_date"],
        "request_budget_used": budget.used,
        "record_count": len(snapshot["records"]),
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
