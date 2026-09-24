"""record 子命令的回归测试。

这条路径曾经因为参数名不一致（--cache-write-tokens 与 args.cache_write_input_tokens）
而完全不可用，当时的测试只覆盖了 ingest 所以没被发现。这里把 record 的关键行为钉住。
"""

import tempfile
import unittest
from pathlib import Path

import support
from tokenledger.cli import main
from tokenledger.models import KIND_USAGE


class RecordCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = support.init_ledger(self.root)

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def _record(self, *extra):
        with support.capture() as output:
            code = main(["--repo", str(self.root), "record", *extra])
        self.ledger = support.reopen(self.ledger, self.root)
        return code, output.getvalue()

    def test_record_books_priced_voucher(self):
        # 单价（每百万 token）：input=1.0 cached_input=0.1 cache_write=1.0 output=2.0
        # 期望：1000*1.0 + 100*1.0 + 500*2.0 = 2100 / 1e6
        code, output = self._record(
            "--agent", "my-agent",
            "--model", "deepseek-v4-flash",
            "--input-tokens", "1000",
            "--output-tokens", "500",
            "--cache-write-tokens", "100",
            "--occurred-at", "2026-09-24T10:00:00Z",
        )
        self.assertEqual(code, 0, output)

        rows = self.ledger.index.query(kind=KIND_USAGE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_tokens"], 1500)
        self.assertEqual(rows[0]["cache_write_input_tokens"], 100)
        self.assertEqual(rows[0]["cost_status"], "priced")
        self.assertAlmostEqual(rows[0]["cost_amount"], 0.0021, places=6)
        self.assertEqual(rows[0]["budget_status"], "unbudgeted")
        self.assertTrue(self.ledger.verify()["ok"])

    def test_record_rejects_duplicate_dedup_key(self):
        first, _ = self._record(
            "--agent", "my-agent", "--model", "deepseek-v4-flash",
            "--input-tokens", "10", "--dedup-key", "fixed-key",
        )
        self.assertEqual(first, 0)
        second, output = self._record(
            "--agent", "my-agent", "--model", "deepseek-v4-flash",
            "--input-tokens", "10", "--dedup-key", "fixed-key",
        )
        self.assertEqual(second, 1)
        self.assertIn("错误", output)
        self.assertEqual(self.ledger.index.counts()[KIND_USAGE], 1)

    def test_record_unpriced_model_is_not_silently_zero(self):
        code, _ = self._record(
            "--agent", "my-agent", "--model", "mystery-model", "--input-tokens", "123"
        )
        self.assertEqual(code, 0)
        rows = self.ledger.index.query(kind=KIND_USAGE)
        self.assertEqual(rows[0]["cost_status"], "unpriced")
        self.assertIsNone(rows[0]["cost_amount"])

    def test_record_respects_authorization(self):
        with support.capture():
            main([
                "--repo", str(self.root), "budget", "set",
                "--agent", "my-agent", "--limit-tokens", "1000",
                "--approved-by", "me",
            ])
        self.ledger = support.reopen(self.ledger, self.root)

        self._record(
            "--agent", "my-agent", "--model", "deepseek-v4-flash",
            "--input-tokens", "100", "--output-tokens", "100",
        )
        self.assertEqual(self.ledger.index.totals(kind=KIND_USAGE)["exceeded_vouchers"], 0)

        # 再记一笔就会越过 1000 token 的授权额度：入账照旧，但必须被标记
        self._record(
            "--agent", "my-agent", "--model", "deepseek-v4-flash",
            "--input-tokens", "1000", "--output-tokens", "1000",
        )
        totals = self.ledger.index.totals(kind=KIND_USAGE)
        self.assertEqual(self.ledger.index.counts()[KIND_USAGE], 2, "超限也要入账")
        self.assertEqual(totals["exceeded_vouchers"], 1)


if __name__ == "__main__":
    unittest.main()