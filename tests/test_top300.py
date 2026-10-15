import unittest

from update_top300 import choose_universe


def row(symbol, name, cap, country="United States", sector="Technology", industry="Computer Software"):
    return {"symbol": symbol, "name": name, "marketCap": str(cap), "country": country,
            "sector": sector, "industry": industry}


class UniverseTests(unittest.TestCase):
    def test_domestic_common_and_company_deduplication(self):
        rows = [row("GOOG", "Alphabet Inc. Class C Capital Stock", 100),
                row("GOOGL", "Alphabet Inc. Class A Common Stock", 99),
                row("AAPL", "Apple Inc. Common Stock", 90),
                row("TSM", "Taiwan Semiconductor American Depositary Shares", 200, "Taiwan"),
                row("PREF", "Test Preferred Stock", 300)]
        result = choose_universe(rows, 2)
        self.assertEqual(result.ticker.tolist(), ["GOOGL", "AAPL"])
        self.assertEqual(result.market_cap_rank.tolist(), [1, 2])
        self.assertEqual(result.benchmark.tolist(), ["XLC", "XLK"])

    def test_incomplete_list_cannot_publish(self):
        with self.assertRaisesRegex(RuntimeError, "Expected 300"):
            choose_universe([row("AAPL", "Apple Inc.", 90)])

    def test_unknown_sector_cannot_silently_skip_company(self):
        with self.assertRaisesRegex(RuntimeError, "Unmapped sector"):
            choose_universe([row("ZZZ", "Zeta Inc.", 90, sector="Unknown")], 1)


if __name__ == "__main__":
    unittest.main()

