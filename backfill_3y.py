from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict
import time

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
CFG = ROOT / "config"
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

UNIVERSE_FILE = CFG / "universe.csv"
PRICE_FILE = DATA / "prices.parquet"

YEARS = 3
BATCH_SIZE = 20
OVERLAP_BUFFER_DAYS = 15

ES_DISPLAY = "ES"
ES_PROVIDER = "ES=F"
SPX_DISPLAY = "SPX"
SPX_PROVIDER = "^GSPC"


def load_existing() -> pd.DataFrame:
    if PRICE_FILE.exists():
        df = pd.read_parquet(PRICE_FILE)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df
    return pd.DataFrame(
        columns=["date", "ticker", "open", "high", "low", "close", "adj_close", "volume"]
    )


def normalize_download(raw: pd.DataFrame, symbols: Dict[str, str]) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()

    provider_list = list(symbols.values())
    reverse = {v: k for k, v in symbols.items()}

    if len(provider_list) == 1:
        raw = pd.concat({provider_list[0]: raw}, axis=1)

    out = []
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

        wanted = ["date", "ticker", "open", "high", "low", "close", "adj_close", "volume"]
        missing = [c for c in wanted if c not in x.columns]
        if missing:
            print(f"SKIP {p_ticker}: missing columns {missing}")
            continue

        out.append(x[wanted])

    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def download_batch(symbols: Dict[str, str], start: str, end: str) -> pd.DataFrame:
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
    return normalize_download(raw, symbols)


def main():
    universe = pd.read_csv(UNIVERSE_FILE, encoding="utf-8-sig")

    provider_lookup = dict(zip(universe["ticker"], universe["provider_ticker"]))
    display_symbols = list(dict.fromkeys(list(universe["ticker"]) + list(universe["benchmark"])))

    for b in universe["benchmark"].dropna().unique():
        provider_lookup[b] = b

    provider_lookup[ES_DISPLAY] = ES_PROVIDER
    provider_lookup[SPX_DISPLAY] = SPX_PROVIDER
    display_symbols.extend([ES_DISPLAY, SPX_DISPLAY])
    display_symbols = list(dict.fromkeys(display_symbols))

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=365 * YEARS + OVERLAP_BUFFER_DAYS)
    end = today + timedelta(days=1)

    print(f"Backfill range: {start} -> {end}")
    print(f"Symbols: {len(display_symbols)}")

    existing = load_existing()
    downloaded_parts = []
    failed = []

    # Batch requests keep the Yahoo traffic modest.
    for i in range(0, len(display_symbols), BATCH_SIZE):
        batch = display_symbols[i:i + BATCH_SIZE]
        symbols = {t: provider_lookup[t] for t in batch}
        print(f"BATCH {i//BATCH_SIZE + 1}: {', '.join(batch)}")

        try:
            x = download_batch(symbols, str(start), str(end))
            if not x.empty:
                downloaded_parts.append(x)

            got = set(x["ticker"].unique()) if not x.empty else set()
            failed.extend([t for t in batch if t not in got])
        except Exception as e:
            print(f"BATCH WARNING: {e}")
            failed.extend(batch)

        time.sleep(1.0)

    # Retry missing symbols individually. This is important because Yahoo batch
    # downloads can occasionally omit one ticker without failing the whole call.
    failed = list(dict.fromkeys(failed))
    retry_failed = []

    if failed:
        print("SINGLE-SYMBOL RETRY:", ", ".join(failed))

    for t in failed:
        try:
            x = download_batch({t: provider_lookup[t]}, str(start), str(end))
            if not x.empty:
                downloaded_parts.append(x)
                print(f"RETRY OK: {t} ({x['date'].nunique()} rows)")
            else:
                retry_failed.append(t)
                print(f"RETRY EMPTY: {t}")
        except Exception as e:
            retry_failed.append(t)
            print(f"RETRY FAILED {t}: {e}")

        time.sleep(0.6)

    if not downloaded_parts:
        raise RuntimeError("No historical data was downloaded. Existing data was left untouched.")

    fresh = pd.concat(downloaded_parts, ignore_index=True)
    combined = pd.concat([existing, fresh], ignore_index=True)

    combined["date"] = pd.to_datetime(combined["date"]).dt.tz_localize(None)
    combined = (
        combined.drop_duplicates(["date", "ticker"], keep="last")
        .sort_values(["ticker", "date"])
    )
    combined.to_parquet(PRICE_FILE, index=False)

    counts = combined.groupby("ticker")["date"].nunique().sort_values()
    print("\nStored history counts:")
    print(counts.to_string())

    print(f"\nSaved: {PRICE_FILE}")
    print(f"Rows: {len(combined):,}")
    print(f"Symbols stored: {combined['ticker'].nunique()}")

    if retry_failed:
        print("\nWARNING - still missing after retry:")
        print(", ".join(retry_failed))
        print("The workflow will continue and update.py will preserve usable data.")


if __name__ == "__main__":
    main()
