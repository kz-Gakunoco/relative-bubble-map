from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

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

ES_DISPLAY = "ES"
ES_PROVIDER = "ES=F"
SPX_DISPLAY = "SPX"
SPX_PROVIDER = "^GSPC"

PERIODS = {
    "1D": 1,
    "5D": 5,
    "20D": 20,
    "60D": 60,
    "3M": 63,
    "6M": 126,
    "12M": 252,
}

# Z-score lookback observations. The current observation is compared with the
# preceding N daily observations of the same rolling relative-return series.
Z_LOOKBACKS = {
    "1D": 252,
    "5D": 252,
    "20D": 252,
    "60D": 504,
    "3M": 504,
    "6M": 756,
    "12M": 756,
}

# The main use case is 20D Z-score (252-day reference distribution).
# ~620 stored trading days also covers 60D/3M with a 504-day reference window.
# Younger listings can still calculate shorter-window Z-scores using the history
# that actually exists; the UI shows the observation count in Hover.
REQUIRED_TRADING_DAYS = 620
MIN_HISTORY_YEARS = 5


@dataclass
class RequestBudget:
    limit: int
    used: int = 0

    def consume(self, n: int = 1):
        if self.used + n > self.limit:
            raise RuntimeError(
                f"API request budget exceeded: {self.used+n}>{self.limit}"
            )
        self.used += n


budget = RequestBudget(int(settings.get("max_requests_per_run", 180)))


def load_prices() -> pd.DataFrame:
    if PRICE_FILE.exists():
        df = pd.read_parquet(PRICE_FILE)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df
    return pd.DataFrame(
        columns=["date", "ticker", "open", "high", "low", "close", "adj_close", "volume"]
    )


def save_prices(df: pd.DataFrame):
    if df.empty:
        return
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = (
        df.drop_duplicates(["date", "ticker"], keep="last")
        .sort_values(["ticker", "date"])
    )
    df.to_parquet(PRICE_FILE, index=False)


def yfinance_prices(symbols: Dict[str, str], start: str, end: str) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()

    provider_list = list(symbols.values())
    raw = yf.download(
        provider_list,
        start=start,
        end=end,
        auto_adjust=False,
        group_by="ticker",
        threads=True,
        progress=False,
    )
    out = []
    reverse = {v: k for k, v in symbols.items()}

    if len(provider_list) == 1:
        raw = pd.concat({provider_list[0]: raw}, axis=1)

    if raw.empty:
        return pd.DataFrame()

    level0 = set(raw.columns.get_level_values(0))
    for p_ticker in provider_list:
        if p_ticker not in level0:
            continue
        x = raw[p_ticker].copy().reset_index()
        if x.empty:
            continue
        x.columns = [str(c).lower().replace(" ", "_") for c in x.columns]
        x["ticker"] = reverse[p_ticker]
        if "adj_close" not in x.columns:
            x["adj_close"] = x.get("close")
        x["date"] = pd.to_datetime(x["date"]).dt.tz_localize(None)
        out.append(
            x[["date", "ticker", "open", "high", "low", "close", "adj_close", "volume"]]
        )

    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def fmp_prices(
    symbols: Dict[str, str], start: str, end: str, api_key: str
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()

    out = []
    base = "https://financialmodelingprep.com/stable/historical-price-eod/full"

    for display, provider in symbols.items():
        budget.consume(1)
        r = requests.get(
            base,
            params={
                "symbol": provider,
                "from": start,
                "to": end,
                "apikey": api_key,
            },
            timeout=30,
        )
        if r.status_code == 429:
            raise RuntimeError("FMP rate limit reached (HTTP 429)")
        r.raise_for_status()

        payload = r.json()
        records = payload if isinstance(payload, list) else payload.get("historical", [])
        if not records:
            continue

        x = pd.DataFrame(records).rename(columns={"adjClose": "adj_close"})
        x["date"] = pd.to_datetime(x["date"]).dt.tz_localize(None)
        x["ticker"] = display
        if "adj_close" not in x.columns:
            x["adj_close"] = x["close"]

        out.append(
            x[["date", "ticker", "open", "high", "low", "close", "adj_close", "volume"]]
        )
        time.sleep(0.06)

    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def refresh_meta_yf(universe_df: pd.DataFrame, old: pd.DataFrame) -> pd.DataFrame:
    old_map = old.set_index("ticker").to_dict("index") if not old.empty else {}
    rows = []

    for _, r in universe_df.iterrows():
        ticker, provider = r.ticker, r.provider_ticker
        prev = old_map.get(ticker, {})
        market_cap = prev.get("market_cap", np.nan)

        try:
            fi = yf.Ticker(provider).fast_info
            v = None
            try:
                v = fi.market_cap
            except Exception:
                pass
            if v is None:
                for k in ("market_cap", "marketCap"):
                    try:
                        v = fi[k]
                        if v is not None:
                            break
                    except Exception:
                        pass
            if v:
                market_cap = float(v)
        except Exception:
            pass

        rows.append(
            {
                "ticker": ticker,
                "market_cap": market_cap,
                "meta_updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    return pd.DataFrame(rows)


def refresh_meta_fmp(
    universe_df: pd.DataFrame, old: pd.DataFrame, api_key: str
) -> pd.DataFrame:
    old_map = old.set_index("ticker").to_dict("index") if not old.empty else {}
    rows = []
    base = "https://financialmodelingprep.com/stable/profile"

    for _, r in universe_df.iterrows():
        prev = old_map.get(r.ticker, {})
        market_cap = prev.get("market_cap", np.nan)

        try:
            budget.consume(1)
            resp = requests.get(
                base,
                params={"symbol": r.provider_ticker, "apikey": api_key},
                timeout=30,
            )
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

        rows.append(
            {
                "ticker": r.ticker,
                "market_cap": market_cap,
                "meta_updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        time.sleep(0.06)

    return pd.DataFrame(rows)


def meta_is_stale() -> bool:
    if not META_FILE.exists():
        return True
    try:
        m = pd.read_csv(META_FILE)
        expected = set(universe.ticker.astype(str))
        actual = set(m.get("ticker", pd.Series(dtype=str)).astype(str))
        if expected != actual:
            return True
        if "market_cap" not in m.columns or m["market_cap"].isna().any():
            return True
    except Exception:
        return True

    mtime = datetime.fromtimestamp(META_FILE.stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc) - mtime > timedelta(
        days=int(settings.get("meta_refresh_days", 7))
    )


def calc_return(s: pd.Series, n: int) -> float:
    s = s.dropna()
    if len(s) <= n:
        return np.nan
    return float(s.iloc[-1] / s.iloc[-1 - n] - 1.0)


def calc_ytd(s: pd.Series, dates: pd.Series) -> float:
    if s.empty:
        return np.nan
    df = pd.DataFrame({"d": dates, "p": s}).dropna()
    if df.empty:
        return np.nan

    year = df.d.iloc[-1].year
    y = df[df.d.dt.year == year]
    if len(y) < 2:
        return np.nan
    return float(y.p.iloc[-1] / y.p.iloc[0] - 1.0)


def rolling_relative_z(
    stock: pd.DataFrame,
    benchmark: pd.DataFrame,
    return_window: int,
    lookback: int,
) -> tuple[float, float, float, int]:
    """
    Current rolling stock-minus-benchmark return and its Z-score against the
    PRECEDING lookback daily observations. The current observation is excluded
    from the reference mean/std so an extreme current move is not diluted.
    """
    merged = (
        stock[["date", "adj_close"]]
        .rename(columns={"adj_close": "stock"})
        .merge(
            benchmark[["date", "adj_close"]].rename(columns={"adj_close": "benchmark"}),
            on="date",
            how="inner",
        )
        .sort_values("date")
        .dropna()
    )

    if len(merged) <= return_window + 2:
        return np.nan, np.nan, np.nan, 0

    rel = (
        merged["stock"].astype(float).pct_change(return_window)
        - merged["benchmark"].astype(float).pct_change(return_window)
    ).dropna()

    if rel.empty:
        return np.nan, np.nan, np.nan, 0

    current = float(rel.iloc[-1])
    history = rel.iloc[:-1].tail(lookback).dropna()

    # Require at least half the requested lookback and never less than 60 obs.
    min_obs = max(60, lookback // 2)
    if len(history) < min_obs:
        return current, np.nan, np.nan, int(len(history))

    mu = float(history.mean())
    sigma = float(history.std(ddof=1))
    if not np.isfinite(sigma) or sigma <= 0:
        return current, np.nan, sigma, int(len(history))

    z = (current - mu) / sigma
    return current, float(z), sigma, int(len(history))


def build_snapshot(prices: pd.DataFrame, meta: pd.DataFrame) -> dict:
    all_needed = sorted(
        set(universe.ticker) | set(universe.benchmark) | {ES_DISPLAY, SPX_DISPLAY}
    )
    px = prices[prices.ticker.isin(all_needed)].copy()
    latest_date = px.date.max()

    series = {
        t: g.sort_values("date").copy()
        for t, g in px.groupby("ticker")
    }
    es = series.get(ES_DISPLAY)
    spx = series.get(SPX_DISPLAY)

    def build_benchmark_metrics(g):
        out = {}
        if g is None or g.empty:
            return out
        p = g.adj_close.astype(float)
        for label, n in PERIODS.items():
            r = calc_return(p, n)
            out[f"ret_{label}"] = None if pd.isna(r) else r
        ytd = calc_ytd(p, g.date)
        out["ret_YTD"] = None if pd.isna(ytd) else ytd
        out["last_close"] = float(g.close.iloc[-1])
        out["last_date"] = g.date.iloc[-1].strftime("%Y-%m-%d")
        return out

    es_metrics = build_benchmark_metrics(es)
    spx_metrics = build_benchmark_metrics(spx)

    metrics = {}
    for _, u in universe.iterrows():
        g = series.get(u.ticker)
        b = series.get(u.benchmark)
        if g is None or g.empty:
            continue

        p = g.adj_close.astype(float)
        item = {}

        for label, n in PERIODS.items():
            r = calc_return(p, n)
            br = (
                calc_return(b.adj_close.astype(float), n)
                if b is not None and not b.empty
                else np.nan
            )
            er = es_metrics.get(f"ret_{label}")
            sr = spx_metrics.get(f"ret_{label}")
            item[f"ret_{label}"] = None if pd.isna(r) else r
            item[f"excess_{label}"] = (
                None if pd.isna(r) or pd.isna(br) else r - br
            )
            item[f"es_excess_{label}"] = (
                None if pd.isna(r) or er is None else r - float(er)
            )
            item[f"spx_excess_{label}"] = (
                None if pd.isna(r) or sr is None else r - float(sr)
            )

            # Relative performance versus each stock's assigned industry ETF.
            if b is not None and not b.empty:
                _, iz, isigma, iobs = rolling_relative_z(
                    g, b, n, Z_LOOKBACKS[label]
                )
            else:
                iz, isigma, iobs = np.nan, np.nan, 0

            item[f"industry_z_{label}"] = None if pd.isna(iz) else iz
            item[f"industry_z_sigma_{label}"] = None if pd.isna(isigma) else isigma
            item[f"industry_z_obs_{label}"] = int(iobs)

            if es is not None and not es.empty:
                _, ez, esigma, eobs = rolling_relative_z(
                    g, es, n, Z_LOOKBACKS[label]
                )
            else:
                ez, esigma, eobs = np.nan, np.nan, 0

            if spx is not None and not spx.empty:
                _, sz, ssigma, sobs = rolling_relative_z(
                    g, spx, n, Z_LOOKBACKS[label]
                )
            else:
                sz, ssigma, sobs = np.nan, np.nan, 0

            item[f"es_z_{label}"] = None if pd.isna(ez) else ez
            item[f"es_z_sigma_{label}"] = None if pd.isna(esigma) else esigma
            item[f"es_z_obs_{label}"] = int(eobs)

            item[f"spx_z_{label}"] = None if pd.isna(sz) else sz
            item[f"spx_z_sigma_{label}"] = None if pd.isna(ssigma) else ssigma
            item[f"spx_z_obs_{label}"] = int(sobs)

        ytd = calc_ytd(p, g.date)
        bytd = (
            calc_ytd(b.adj_close.astype(float), b.date)
            if b is not None and not b.empty
            else np.nan
        )
        es_ytd = es_metrics.get("ret_YTD")
        spx_ytd = spx_metrics.get("ret_YTD")
        item["ret_YTD"] = None if pd.isna(ytd) else ytd
        item["excess_YTD"] = None if pd.isna(ytd) or pd.isna(bytd) else ytd - bytd
        item["es_excess_YTD"] = (
            None if pd.isna(ytd) or es_ytd is None else ytd - float(es_ytd)
        )
        item["spx_excess_YTD"] = (
            None if pd.isna(ytd) or spx_ytd is None else ytd - float(spx_ytd)
        )
        # YTD Z-score is intentionally unsupported because the return window
        # changes with the calendar date and has strong seasonality.
        item["industry_z_YTD"] = None
        item["industry_z_sigma_YTD"] = None
        item["industry_z_obs_YTD"] = 0
        item["es_z_YTD"] = None
        item["es_z_sigma_YTD"] = None
        item["es_z_obs_YTD"] = 0
        item["spx_z_YTD"] = None
        item["spx_z_sigma_YTD"] = None
        item["spx_z_obs_YTD"] = 0

        item["last_close"] = float(g.close.iloc[-1])
        item["last_date"] = g.date.iloc[-1].strftime("%Y-%m-%d")
        metrics[u.ticker] = item

    meta_map = (
        meta.set_index("ticker")["market_cap"].to_dict()
        if not meta.empty and "market_cap" in meta.columns
        else {}
    )

    records = []
    for _, u in universe.iterrows():
        m = metrics.get(u.ticker)
        if not m:
            continue
        rec = u.to_dict()
        rec.update(m)
        mc = meta_map.get(u.ticker, np.nan)
        rec["market_cap"] = None if pd.isna(mc) else float(mc)
        records.append(rec)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "latest_market_date": (
            None
            if pd.isna(latest_date)
            else pd.Timestamp(latest_date).strftime("%Y-%m-%d")
        ),
        "request_budget_used": budget.used,
        "z_lookbacks": Z_LOOKBACKS,
        "es": es_metrics,
        "spx": spx_metrics,
        "records": records,
    }


def main():
    old = load_prices()

    base_symbols = list(
        dict.fromkeys(list(universe.ticker) + list(universe.benchmark))
    )
    provider_lookup = dict(zip(universe.ticker, universe.provider_ticker))
    for b in universe.benchmark.unique():
        provider_lookup[b] = b

    today = datetime.now(timezone.utc).date()
    history_years = max(
        MIN_HISTORY_YEARS, int(settings.get("history_years", MIN_HISTORY_YEARS))
    )
    full_start_dt = today - timedelta(days=365 * history_years + 15)
    end_dt = today + timedelta(days=1)

    provider_setting = settings.get("provider", "auto").lower()
    api_key = os.getenv("FMP_API_KEY", "").strip()
    provider = (
        "fmp"
        if (
            provider_setting == "fmp"
            or (provider_setting == "auto" and api_key)
        )
        else "yfinance"
    )

    counts = old.groupby("ticker")["date"].nunique().to_dict() if not old.empty else {}
    recent_start_dt = (
        full_start_dt
        if old.empty
        else pd.Timestamp(old.date.max()).date() - timedelta(days=10)
    )

    # Equities + benchmark ETFs use the selected provider.
    backfill_tickers = {
        t for t in base_symbols if counts.get(t, 0) < REQUIRED_TRADING_DAYS
    }
    regular_tickers = [t for t in base_symbols if t not in backfill_tickers]
    backfill_symbols = {t: provider_lookup[t] for t in backfill_tickers}
    regular_symbols = {t: provider_lookup[t] for t in regular_tickers}

    try:
        fresh_parts = []

        if regular_symbols:
            if provider == "fmp":
                x = fmp_prices(
                    regular_symbols, str(recent_start_dt), str(end_dt), api_key
                )
            else:
                x = yfinance_prices(
                    regular_symbols, str(recent_start_dt), str(end_dt)
                )
            if not x.empty:
                fresh_parts.append(x)

        if backfill_symbols:
            print("BACKFILL:", ", ".join(sorted(backfill_symbols)))
            if provider == "fmp":
                x = fmp_prices(
                    backfill_symbols, str(full_start_dt), str(end_dt), api_key
                )
            else:
                x = yfinance_prices(
                    backfill_symbols, str(full_start_dt), str(end_dt)
                )
            if not x.empty:
                fresh_parts.append(x)

        # ES and SPX are deliberately sourced from Yahoo/yfinance even if FMP is selected.
        # Both require no extra API key for this personal EOD use case.
        es_needs_backfill = counts.get(ES_DISPLAY, 0) < REQUIRED_TRADING_DAYS
        es_start = full_start_dt if es_needs_backfill else recent_start_dt
        es_fresh = yfinance_prices(
            {ES_DISPLAY: ES_PROVIDER}, str(es_start), str(end_dt)
        )
        if not es_fresh.empty:
            fresh_parts.append(es_fresh)

        spx_needs_backfill = counts.get(SPX_DISPLAY, 0) < REQUIRED_TRADING_DAYS
        spx_start = full_start_dt if spx_needs_backfill else recent_start_dt
        spx_fresh = yfinance_prices(
            {SPX_DISPLAY: SPX_PROVIDER}, str(spx_start), str(end_dt)
        )
        if not spx_fresh.empty:
            fresh_parts.append(spx_fresh)

        if fresh_parts:
            old = pd.concat([old] + fresh_parts, ignore_index=True)
            save_prices(old)

        # Retry any symbol whose stored history is still too short.
        counts_after = (
            old.groupby("ticker")["date"].nunique().to_dict()
            if not old.empty
            else {}
        )
        deficient = [
            t
            for t in base_symbols
            if counts_after.get(t, 0) < REQUIRED_TRADING_DAYS
        ]
        if deficient:
            print("SINGLE-SYMBOL RETRY:", ", ".join(sorted(deficient)))
            retry_parts = []
            for t in deficient:
                try:
                    if provider == "fmp":
                        x = fmp_prices(
                            {t: provider_lookup[t]},
                            str(full_start_dt),
                            str(end_dt),
                            api_key,
                        )
                    else:
                        x = yfinance_prices(
                            {t: provider_lookup[t]},
                            str(full_start_dt),
                            str(end_dt),
                        )
                    if not x.empty:
                        retry_parts.append(x)
                except Exception as single_e:
                    print(f"SINGLE RETRY WARNING {t}: {single_e}")

            if retry_parts:
                old = pd.concat([old] + retry_parts, ignore_index=True)
                save_prices(old)

        # ES / SPX single-symbol retries.
        counts_after = (
            old.groupby("ticker")["date"].nunique().to_dict()
            if not old.empty
            else {}
        )
        if counts_after.get(ES_DISPLAY, 0) < REQUIRED_TRADING_DAYS:
            try:
                x = yfinance_prices(
                    {ES_DISPLAY: ES_PROVIDER},
                    str(full_start_dt),
                    str(end_dt),
                )
                if not x.empty:
                    old = pd.concat([old, x], ignore_index=True)
                    save_prices(old)
            except Exception as es_e:
                print(f"ES RETRY WARNING: {es_e}")

        counts_after = (
            old.groupby("ticker")["date"].nunique().to_dict()
            if not old.empty
            else {}
        )
        if counts_after.get(SPX_DISPLAY, 0) < REQUIRED_TRADING_DAYS:
            try:
                x = yfinance_prices(
                    {SPX_DISPLAY: SPX_PROVIDER},
                    str(full_start_dt),
                    str(end_dt),
                )
                if not x.empty:
                    old = pd.concat([old, x], ignore_index=True)
                    save_prices(old)
            except Exception as spx_e:
                print(f"SPX RETRY WARNING: {spx_e}")

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
    SNAPSHOT_FILE.write_text(
        json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    tracked = set(base_symbols) | {ES_DISPLAY, SPX_DISPLAY}
    counts_final = (
        prices[prices["ticker"].isin(tracked)]
        .groupby("ticker")["date"]
        .nunique()
        .to_dict()
    )

    status = {
        "ok": True,
        "provider": provider,
        "es_provider": "yfinance:ES=F",
        "spx_provider": "yfinance:^GSPC",
        "generated_at_utc": snapshot["generated_at_utc"],
        "latest_market_date": snapshot["latest_market_date"],
        "request_budget_used": budget.used,
        "record_count": len(snapshot["records"]),
        "missing_universe_tickers": sorted(
            set(universe.ticker)
            - {r["ticker"] for r in snapshot["records"]}
        ),
        "insufficient_history_tickers": {
            str(t): int(counts_final.get(t, 0))
            for t in sorted(tracked)
            if int(counts_final.get(t, 0)) < REQUIRED_TRADING_DAYS
        },
    }
    STATUS_FILE.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
