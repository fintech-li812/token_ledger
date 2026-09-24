"""授权预算与外部对账：超限标记、退出码、差异识别。

注意测试顺序：授权必须先于支出签发。
凭证上的 budget.status 是**入账当时**的判定快照（账本只增不改），
事后补签授权不会回头改写历史凭证 —— 这正是内控里"授权先于支出"的含义。
"""

import tempfile
import unittest
from pathlib import Path

import support
from tokenledger.cli import main
from tokenledger.models import KIND_USAGE


class BudgetAndReconcileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = support.init_ledger(self.root)
        self.log = support.write_rollout(self.root / "logs" / "rollout-2026-09-24.jsonl")

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    # -- 辅助 ------------------------------------------------------------- #
    def _set_budget(self, amount, *extra):
        with support.capture() as output:
            code = main(
                [
                    "--repo", str(self.root), "budget", "set",
                    "--agent", "codex-tui", "--limit-usd", str(amount),
                    "--from", "2026-09-01", "--to", "2026-09-30",
                    "--approved-by", "me", *extra,
                ]
            )
        self.assertEqual(code, 0, output.getvalue())
        self.ledger = support.reopen(self.ledger, self.root)

    def _ingest(self, *extra):
        with support.capture() as output:
            code = main(
                ["--repo", str(self.root), "ingest", "codex", "--path", str(self.log), *extra]
            )
        self.assertEqual(code, 0, output.getvalue())
        self.ledger = support.reopen(self.ledger, self.root)

    def _budget_status(self, *extra):
        with support.capture() as output:
            code = main(["--repo", str(self.root), "budget", "status", *extra])
        return code, output.getvalue()

    # -- 授权控制 --------------------------------------------------------- #
    def test_no_authorization_marks_unbudgeted(self):
        self._ingest()
        totals = self.ledger.index.totals(kind=KIND_USAGE)
        self.assertEqual(totals["unbudgeted_vouchers"], 3)
        self.assertEqual(totals["exceeded_vouchers"], 0)

    def test_over_budget_is_recorded_and_flagged(self):
        self._set_budget(0.001)
        self._ingest()

        totals = self.ledger.index.totals(kind=KIND_USAGE)
        self.assertEqual(totals["exceeded_vouchers"], 3)
        self.assertEqual(totals["unbudgeted_vouchers"], 0)
        self.assertEqual(self.ledger.index.counts()[KIND_USAGE], 3, "超限凭证也必须入账")

        code, output = self._budget_status("--strict")
        self.assertEqual(code, 5)
        self.assertIn("已超限", output)

    def test_generous_budget_stays_within(self):
        self._set_budget(10)
        self._ingest()
        totals = self.ledger.index.totals(kind=KIND_USAGE)
        self.assertEqual(totals["exceeded_vouchers"], 0)
        self.assertEqual(self._budget_status("--strict")[0], 0)

    def test_unpriced_usage_makes_amount_indeterminate(self):
        self._set_budget(10)
        self.log = support.write_rollout(
            self.root / "logs" / "rollout-unknown.jsonl", model="unknown-model"
        )
        self._ingest()

        totals = self.ledger.index.totals(kind=KIND_USAGE)
        self.assertEqual(totals["unpriced_vouchers"], 3, "未定价用量必须被统计出来")
        self.assertEqual(totals["exceeded_vouchers"], 0, "金额不可得时不应误报超限")

        code, output = self._budget_status()
        self.assertEqual(code, 0)
        self.assertIn("无法判定", output, "缺价格不能被当成没花钱")

    def test_exception_report_lists_problems(self):
        self._set_budget(0.001)
        self._ingest()
        with support.capture() as output:
            self.assertEqual(main(["--repo", str(self.root), "report", "--exceptions"]), 0)
        text = output.getvalue()
        self.assertIn("超预算凭证", text)
        self.assertIn("3 条", text)

    # -- 外部对账 --------------------------------------------------------- #
    def _write_bill(self, name, amount, model="deepseek-v4-flash"):
        return support.write_bill(
            self.root / name,
            [
                {
                    "model": model,
                    "date": "2026-09-24",
                    "input_tokens": 3500,
                    "output_tokens": 450,
                    "total_tokens": support.EXPECTED_TOTAL_TOKENS,
                    "amount": amount,
                    "currency": "USD",
                }
            ],
        )

    def test_reconcile_matching_bill_passes(self):
        self._ingest()
        bill = self._write_bill("bill_ok.csv", support.EXPECTED_COST)
        with support.capture() as output:
            code = main(
                [
                    "--repo", str(self.root), "reconcile", "--bill", str(bill),
                    "--tolerance-amount", "0.000001",
                ]
            )
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("完全一致", output.getvalue())

    def test_reconcile_detects_amount_difference(self):
        self._ingest()
        bill = self._write_bill("bill_bad.csv", 0.5)
        with support.capture() as output:
            code = main(["--repo", str(self.root), "reconcile", "--bill", str(bill)])
        self.assertEqual(code, 4)
        self.assertIn("有差异", output.getvalue())

    def test_reconcile_flags_model_missing_in_ledger(self):
        self._ingest()
        bill = self._write_bill("bill_missing.csv", 0.01, model="some-other-model")
        with support.capture() as output:
            code = main(["--repo", str(self.root), "reconcile", "--bill", str(bill)])
        self.assertEqual(code, 4)
        self.assertIn("账本缺失", output.getvalue())


if __name__ == "__main__":
    unittest.main()