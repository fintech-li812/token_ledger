"""来源日志证明：签发、入账校验、以及"签名后被改动"必须被发现。

这个测试针对的正是账本自身无法覆盖的那一段：
哈希链证明不了"入账之前源日志有没有被动过"，attestation 就是补这一段。
"""

import json
import tempfile
import unittest
from pathlib import Path

import support
from tokenledger.attestation import STATUS_CHANGED, STATUS_UNSIGNED, STATUS_VERIFIED
from tokenledger.cli import main
from tokenledger.models import KIND_ATTESTATION, KIND_USAGE


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger_dir = self.root / "book"
        self.key_path = self.root / "test.key"
        self.log = support.write_rollout(self.root / "logs" / "rollout-signed.jsonl")
        self.ledger = support.init_ledger(self.ledger_dir)

        support.set_attestation_key(self.ledger_dir, self.key_path)
        self.ledger = support.reopen(self.ledger, self.ledger_dir)

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def _cli(self, *argv):
        with support.capture() as output:
            code = main(["--repo", str(self.ledger_dir), *argv])
        self.ledger = support.reopen(self.ledger, self.ledger_dir)
        return code, output.getvalue()

    def _sign(self, *extra):
        """签名默认针对 setUp 里那份日志；extra 用来传选项，例如 --signer ops。"""
        return self._cli("attest", "sign", str(self.log), *extra)

    def test_keygen_creates_key_outside_the_ledger(self):
        code, output = self._cli("attest", "keygen", "--out", str(self.key_path))
        self.assertEqual(code, 0, output)
        self.assertTrue(self.key_path.is_file())
        self.assertFalse(
            str(self.key_path).startswith(str(self.ledger_dir)),
            "密钥不应放在账本目录里（密钥一进仓库就等于把印章和账本锁在一个抽屉里）",
        )

    def test_sign_then_ingest_is_verified(self):
        self.assertEqual(self._cli("attest", "keygen", "--out", str(self.key_path))[0], 0)
        code, output = self._sign("--signer", "ops")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.ledger.index.counts()[KIND_ATTESTATION], 1)

        code, output = self._cli("ingest", "codex", "--path", str(self.log))
        self.assertEqual(code, 0, output)
        rows = self.ledger.index.query(kind=KIND_USAGE)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["attestation_status"] == STATUS_VERIFIED for row in rows), rows)

        result = self.ledger.verify()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["attestations"], [{"status": STATUS_VERIFIED, "vouchers": 3}])
        self.assertEqual(self.ledger.index.totals()["unattested_vouchers"], 0)

    def test_signing_the_same_file_twice_does_not_duplicate(self):
        self._cli("attest", "keygen", "--out", str(self.key_path))
        self._sign()
        code, output = self._sign()
        self.assertEqual(code, 0)
        self.assertIn("跳过 1 条", output)
        self.assertEqual(self.ledger.index.counts()[KIND_ATTESTATION], 1)

    def test_unsigned_log_is_flagged_and_can_be_rejected(self):
        code, output = self._cli("ingest", "codex", "--path", str(self.log))
        self.assertEqual(code, 0, output)
        rows = self.ledger.index.query(kind=KIND_USAGE)
        self.assertTrue(all(row["attestation_status"] == STATUS_UNSIGNED for row in rows))
        self.assertEqual(self.ledger.index.totals()["unattested_vouchers"], 3)

        with support.capture() as output:
            self.assertEqual(main(["--repo", str(self.ledger_dir), "report", "--exceptions"]), 0)
        self.assertIn("来源无证明的凭证", output.getvalue())

        # 策略收紧后，未证明的日志拒绝入账，退出码 6
        other = support.write_rollout(
            self.root / "logs" / "rollout-other.jsonl", session_id="01a0-other-session"
        )
        code, output = self._cli("ingest", "codex", "--path", str(other), "--require-attestation")
        self.assertEqual(code, 6)
        self.assertIn("拒绝入账", output)
        self.assertEqual(self.ledger.index.counts()[KIND_USAGE], 3, "被拒绝的文件不应产生凭证")

    def test_tampering_after_signing_is_detected_and_fails_audit(self):
        self._cli("attest", "keygen", "--out", str(self.key_path))
        self._sign()
        self._cli("ingest", "codex", "--path", str(self.log))
        self.assertTrue(self.ledger.verify()["ok"])

        # 签名之后往日志里追加一次用量 —— 正是"入账前被改过"的场景
        appended = {
            "timestamp": "2026-09-24T15:00:00.000Z",
            "ordinal": 99,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 5, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                        "output_tokens": 5, "reasoning_output_tokens": 0, "total_tokens": 10,
                    },
                    "total_token_usage": {
                        "input_tokens": 3505, "cached_input_tokens": 2200, "cache_write_input_tokens": 0,
                        "output_tokens": 455, "reasoning_output_tokens": 50, "total_tokens": 3960,
                    },
                },
            },
        }
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(appended, ensure_ascii=False) + "\n")

        code, output = self._cli("ingest", "codex", "--path", str(self.log))
        self.assertEqual(code, 0, output)
        changed = self.ledger.index.query(kind=KIND_USAGE, filters={"attestation": STATUS_CHANGED})
        self.assertEqual(len(changed), 1, "被改动的日志产生的凭证必须标记为 changed")

        # 审计必须因此不通过
        code, output = self._cli("verify")
        self.assertEqual(code, 3, output)
        self.assertIn("被改动过", output)

        # 专门的证明核对同样报错，退出码 6
        code, output = self._cli("attest", "verify", str(self.log))
        self.assertEqual(code, 6)
        self.assertIn("签名后被改动", output)

        # 策略收紧时，被改动的日志直接拒绝入账
        code, output = self._cli(
            "ingest", "codex", "--path", str(self.log), "--require-attestation",
        )
        self.assertEqual(code, 6)

    def test_attest_list_shows_signed_files(self):
        self._cli("attest", "keygen", "--out", str(self.key_path))
        self._sign()
        code, output = self._cli("attest", "list")
        self.assertEqual(code, 0)
        self.assertIn("证明凭证号", output)
        self.assertIn("rollout-signed.jsonl", output)


if __name__ == "__main__":
    unittest.main()