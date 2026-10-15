import json
import unittest
from pathlib import Path

from update_top300 import issuer_key

ROOT = Path(__file__).resolve().parents[1]


class PublishedDataTests(unittest.TestCase):
    def test_curated_snapshot_unchanged_in_size(self):
        snapshot = json.loads((ROOT / "docs/snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(len(snapshot["records"]), 54)

    @unittest.skipUnless((ROOT / "docs/snapshot_top300.json").exists(), "Top 300 not generated yet")
    def test_generated_top300(self):
        snapshot = json.loads((ROOT / "docs/snapshot_top300.json").read_text(encoding="utf-8"))
        records = snapshot["records"]
        self.assertEqual(len(records), 300)
        self.assertEqual(len({r["ticker"] for r in records}), 300)
        self.assertEqual(len({issuer_key(r["company"]) for r in records}), 300)
        self.assertEqual([r["market_cap_rank"] for r in records], list(range(1, 301)))
        caps = [r["market_cap"] for r in records]
        self.assertEqual(caps, sorted(caps, reverse=True))
        self.assertTrue(all(r["last_close"] > 0 and r["market_cap"] > 0 for r in records))
        self.assertEqual(snapshot["universe"]["company_count"], 300)
        print("Top 300 snapshot:", snapshot["latest_market_date"], "dates:", sorted({r["last_date"] for r in records}),
              "20D return gaps:", [r["ticker"] for r in records if r.get("ret_20D") is None])

