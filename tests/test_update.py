import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import update


def yahoo_frame(multi=True):
    frame = pd.DataFrame(
        {"Open": [10.0], "High": [11.0], "Low": [9.0],
         "Close": [10.0], "Adj Close": [10.0], "Volume": [100]},
        index=pd.DatetimeIndex(["2026-10-14"], name="Date"),
    )
    return pd.concat({"SPY": frame}, axis=1) if multi else frame


class PriceTests(unittest.TestCase):
    def test_original_single_symbol_bug(self):
        raw = pd.concat({"SPY": yahoo_frame()}, axis=1)
        x = raw["SPY"].reset_index()
        x.columns = [str(c).lower().replace(" ", "_") for c in x.columns]
        with self.assertRaises(KeyError):
            x["date"]

    def test_single_symbol_formats(self):
        for multi in (True, False):
            with self.subTest(multi=multi), patch.object(update.yf, "download", return_value=yahoo_frame(multi)):
                result = update.yfinance_prices({"ETF": "SPY"}, "2026-10-01", "2026-10-15")
                self.assertEqual(result.ticker.tolist(), ["ETF"])
                self.assertEqual(result.date.iloc[0], pd.Timestamp("2026-10-14"))

    def test_multiple_symbols(self):
        frame = pd.concat({"SPY": yahoo_frame(False), "QQQ": yahoo_frame(False)}, axis=1)
        with patch.object(update.yf, "download", return_value=frame):
            result = update.yfinance_prices({"A": "SPY", "B": "QQQ"}, "2026-10-01", "2026-10-15")
        self.assertEqual(set(result.ticker), {"A", "B"})

    def test_missing_symbol_is_failure(self):
        with patch.object(update.yf, "download", return_value=yahoo_frame()):
            with self.assertRaisesRegex(RuntimeError, "QQQ"):
                update.yfinance_prices({"SPY": "SPY", "QQQ": "QQQ"}, "2026-10-01", "2026-10-15")

    def test_empty_response_is_failure(self):
        with patch.object(update.yf, "download", return_value=pd.DataFrame()):
            with self.assertRaisesRegex(RuntimeError, "empty price response"):
                update.yfinance_prices({"SPY": "SPY"}, "2026-10-01", "2026-10-15")

    def test_stalled_dates_and_weekend_grace(self):
        snap = {"latest_market_date": "2026-10-09", "spx": {"last_date": "2026-10-09"}, "es": {"last_date": "2026-10-09"}}
        self.assertEqual(update.freshness_errors(snap, datetime(2026, 10, 12, 7, tzinfo=timezone.utc)), [])
        self.assertTrue(update.freshness_errors(snap, datetime(2026, 10, 15, 23, tzinfo=timezone.utc)))

    def test_failure_preserves_published_snapshot(self):
        with tempfile.TemporaryDirectory(dir=update.ROOT) as temp:
            target = Path(temp) / "snapshot.json"
            target.write_text("previous good data")
            health = {"ok": True, "errors": [], "warnings": []}
            with patch.object(update, "SNAPSHOT_FILE", target), patch.object(update, "validate_snapshot", return_value=health), patch.object(update, "freshness_errors", return_value=[]), patch.object(update, "upsert_signal_history") as history:
                result, _ = update.publish_snapshot_if_healthy({}, pd.DataFrame(), pd.DataFrame(), ["download failed"])
            self.assertFalse(result["ok"])
            self.assertFalse(result["published"])
            self.assertEqual(target.read_text(), "previous good data")
            history.assert_not_called()


if __name__ == "__main__":
    unittest.main()

