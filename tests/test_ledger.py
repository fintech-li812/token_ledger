"""账本核心：编号连续性、哈希链、防篡改、幂等、索引重建。"""

import json
import tempfile
import unittest
from pathlib import Path

import support
from tokenledger import Ledger
from tokenledger.ledger import DuplicateVoucher
from tokenledger.models import KIND_USAGE, Usage, make_voucher_no


class LedgerCoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = support.init_ledger(self.root, pricing=False)

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def _record(self, tokens=100, dedup=None):
        return self.ledger.append(
            KIND_USAGE,
            {
                "occurred_at": "2026-09-24T00:00:00Z",
                "occurred_day": "2026-09-24",
                "agent": {"id": "codex-tui"},
                "model": "test-model",
                "usage": Usage(input_tokens=tokens, output_tokens=1, total_tokens=tokens + 1).to_dict(),
                "dedup_key": dedup,
            },
            dedup_key=dedup,
        )

    def test_voucher_numbers_and_chain(self):
        first = self._record()
        second = self._record()
        self.assertEqual(first["voucher_no"], make_voucher_no(KIND_USAGE, 2026, 1))
        self.assertEqual(second["voucher_no"], make_voucher_no(KIND_USAGE, 2026, 2))
        self.assertEqual(second["prev_hash"], first["hash"])

    def test_verify_passes_on_clean_ledger(self):
        self._record()
        self._record()
        result = self.ledger.verify()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["kinds"][KIND_USAGE]["vouchers"], 2)

    def test_verify_detects_tampering(self):
        self._record()
        path = self.ledger.stream_path(KIND_USAGE)
        record = json.loads(path.read_text(encoding="utf-8").strip())
        record["payload"]["usage"]["input_tokens"] = 999999
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

        result = self.ledger.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("篡改" in error for error in result["errors"]), result["errors"])

    def test_verify_detects_deleted_voucher(self):
        for _ in range(3):
            self._record()
        path = self.ledger.stream_path(KIND_USAGE)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

        result = self.ledger.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("不连续" in error for error in result["errors"]), result["errors"])

    def test_duplicate_dedup_key_is_rejected(self):
        self._record(dedup="same-key")
        with self.assertRaises(DuplicateVoucher):
            self._record(dedup="same-key")

    def test_reindex_rebuilds_index_from_journal(self):
        for _ in range(3):
            self._record()
        expected = self.ledger.index.counts()

        self.ledger.close()
        self.ledger.index_path.unlink()
        self.ledger = Ledger.open(self.root)
        self.assertTrue(self.ledger.index.counts()["total"] == 0)

        self.ledger.reindex()
        self.assertEqual(self.ledger.index.counts(), expected)
        self.assertTrue(self.ledger.verify()["ok"])

    def test_verify_reports_index_drift(self):
        self._record()
        self.ledger.index.add(
            {
                "voucher_no": "TL-2026-999999",
                "kind": KIND_USAGE,
                "recorded_at": "2026-09-24T00:00:00Z",
                "hash": "deadbeef",
                "payload": {"usage": {}, "occurred_at": "2026-09-24T00:00:00Z"},
            }
        )
        result = self.ledger.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["index"]["extra_in_index"], ["TL-2026-999999"])


if __name__ == "__main__":
    unittest.main()