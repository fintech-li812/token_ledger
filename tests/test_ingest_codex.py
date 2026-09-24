"""Codex 日志解析与入账：幂等、账实核对、财报口径。"""

import tempfile
import unittest
from pathlib import Path

import support
from tokenledger import Ledger
from tokenledger.cli import main
from tokenledger.models import KIND_USAGE


class CodexIngestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = support.init_ledger(self.root)
        self.log = support.write_rollout(self.root / "logs" / "rollout-2026-09-24.jsonl")

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def _ingest(self, *extra):
        with support.capture() as output:
            code = main(
                ["--repo", str(self.root), "ingest", "codex", "--path", str(self.log), *extra]
            )
        self.ledger = support.reopen(self.ledger, self.root)
        return code, output.getvalue()

    def test_ingest_writes_vouchers_with_pricing(self):
        code, output = self._ingest()
        self.assertEqual(code, 0, output)

        counts = self.ledger.index.counts()
        self.assertEqual(counts[KIND_USAGE], 3)

        totals = self.ledger.index.totals(kind=KIND_USAGE)
        self.assertEqual(totals["total_tokens"], support.EXPECTED_TOTAL_TOKENS)
        self.assertEqual(totals["cached_input_tokens"], 2200)
        self.assertAlmostEqual(totals["cost_amount"], support.EXPECTED_COST, places=6)
        self.assertEqual(totals["unpriced_vouchers"], 0)

        rows = self.ledger.index.query(kind=KIND_USAGE, order="occurred_at ASC")
        self.assertEqual(rows[0]["agent_id"], "codex-tui")
        self.assertEqual(rows[0]["agent_provider"], "deepseek")
        self.assertEqual(rows[0]["model"], "deepseek-v4-flash")
        self.assertEqual(rows[0]["project"], "demo-project")
        self.assertEqual(rows[0]["source_parser"], "codex")
        self.assertEqual(rows[0]["session_id"], "01a0-test-session")
        self.assertTrue(rows[0]["dedup_key"].startswith("codex:01a0-test-session:"))

    def test_verify_reconciles_session_total(self):
        self._ingest()
        result = self.ledger.verify()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(len(result["session_totals"]), 1)
        entry = result["session_totals"][0]
        self.assertEqual(entry["status"], "matched")
        self.assertEqual(entry["delta_total_tokens"], support.EXPECTED_TOTAL_TOKENS)
        self.assertEqual(entry["cumulative_total_tokens"], support.EXPECTED_TOTAL_TOKENS)

    def test_second_ingest_is_idempotent(self):
        self._ingest()
        code, output = self._ingest()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.ledger.index.counts()[KIND_USAGE], 3)
        self.assertIn("跳过 3 条", output)

    def test_limit_marks_session_partial(self):
        code, _ = self._ingest("--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger.index.counts()[KIND_USAGE], 1)
        result = self.ledger.verify()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["session_totals"][0]["status"], "partial")

    def test_since_filter_excludes_older_days(self):
        code, output = self._ingest("--since", "2026-09-25")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.ledger.index.counts()[KIND_USAGE], 0)

    def test_unpriced_model_is_reported_not_zeroed(self):
        self.log = support.write_rollout(self.root / "logs" / "rollout-unknown.jsonl", model="unknown-model")
        code, output = self._ingest()
        self.assertEqual(code, 0, output)
        totals = self.ledger.index.totals(kind=KIND_USAGE)
        self.assertEqual(totals["unpriced_vouchers"], 3)
        self.assertEqual(totals["cost_amount"], 0)
        rows = self.ledger.index.query(kind=KIND_USAGE, filters={"cost_status": "unpriced"})
        self.assertEqual(len(rows), 3)

    def test_report_and_export_run(self):
        self._ingest()
        with support.capture() as output:
            self.assertEqual(main(["--repo", str(self.root), "report", "--group-by", "model"]), 0)
        self.assertIn("deepseek-v4-flash", output.getvalue())

        with support.capture() as output:
            self.assertEqual(main(["--repo", str(self.root), "export", "--format", "csv"]), 0)
        self.assertIn("voucher_no", output.getvalue())


if __name__ == "__main__":
    unittest.main()