"""幂等摄入测试：重复提交不重复计数、内容冲突原因可区分、乱序按区块回放。"""
from __future__ import annotations

import logging
import random
import unittest

from app.errors import ContentConflictError
from app.ledger import canonical_key
from app.models import IngestStatus, Transaction, Wallet
from app.service import RiskService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("idempotency")


def _demo_wallets() -> list[Wallet]:
    return [Wallet("U1"), Wallet("U2"), Wallet("U3"), Wallet("HUB")]


def _demo_txs() -> list[Transaction]:
    return [
        Transaction("c1", 1, 0, "U1", "HUB", 100),
        Transaction("c2", 1, 1, "U2", "HUB", 200),
        Transaction("c3", 1, 2, "U3", "HUB", 300),
    ]


class IdempotencyTest(unittest.TestCase):
    def test_duplicate_transaction_counted_once(self) -> None:
        svc = RiskService()
        for w in _demo_wallets():
            svc.ingest_wallet(w)
        tx = _demo_txs()[0]
        r1 = svc.ingest_transaction(tx)
        r2 = svc.ingest_transaction(tx)
        r3 = svc.ingest_transaction(tx)
        log.info("输入流水: 同一笔交易 %s 提交 3 次 -> %s, %s, %s",
                 tx.tx_id, r1.status.value, r2.status.value, r3.status.value)
        self.assertEqual(r1.status, IngestStatus.ACCEPTED)
        self.assertEqual((r2.status, r3.status), (IngestStatus.DUPLICATE, IngestStatus.DUPLICATE))
        snap = svc.snapshot()
        self.assertEqual(len(snap.transactions), 1, "重复交易不得重复计入")
        self.assertEqual(snap.balance("HUB"), 100)
        self.assertEqual(snap.balance("U1"), -100)
        log.info("判定依据: 账本仅 %d 笔交易，HUB 余额=%d（重复提交未重复计数）",
                 len(snap.transactions), snap.balance("HUB"))

    def test_conflicting_fields_are_distinguishable(self) -> None:
        svc = RiskService()
        svc.ingest_wallet(Wallet("A"))
        svc.ingest_wallet(Wallet("B"))
        svc.ingest_wallet(Wallet("C"))
        original = Transaction("t9", 3, 1, "A", "B", 500, timestamp=100)
        svc.ingest_transaction(original)
        # 同一 tx_id，不同字段 -> 每种字段冲突必须能被单独识别
        # （sender/receiver 的篡改使用第三个合法钱包 C，确保通过合法性校验后仍被识别为冲突）
        mutations = {
            "block": Transaction("t9", 9, 1, "A", "B", 500, timestamp=100),
            "tx_index": Transaction("t9", 3, 8, "A", "B", 500, timestamp=100),
            "sender": Transaction("t9", 3, 1, "C", "B", 500, timestamp=100),
            "receiver": Transaction("t9", 3, 1, "A", "C", 500, timestamp=100),
            "amount": Transaction("t9", 3, 1, "A", "B", 1, timestamp=100),
            "timestamp": Transaction("t9", 3, 1, "A", "B", 500, timestamp=999),
        }
        for field_name, mutated in mutations.items():
            result = svc.ingest_transaction(mutated)
            log.info("冲突输入 tx_id=t9 篡改字段=%s -> status=%s reason=%s",
                     field_name, result.status.value, result.reason)
            self.assertEqual(result.status, IngestStatus.CONFLICT)
            self.assertEqual(result.reason, f"交易字段冲突[{field_name}]")
            self.assertIn(field_name, result.detail)
        # 冲突记录不得污染账本：余额保持原始值
        snap = svc.snapshot()
        self.assertEqual(len(snap.transactions), 1)
        self.assertEqual(snap.balance("A"), -500)
        # 若篡改后记录本身非法（收付款方相同），归类为 REJECTED 而非 CONFLICT
        invalid_mut = svc.ingest_transaction(Transaction("t9", 3, 1, "A", "A", 500))
        self.assertEqual(invalid_mut.status, IngestStatus.REJECTED)
        log.info("判定依据: 6 种字段冲突全部归类 CONFLICT 且账本余额未被污染 A=%s；"
                 "篡改后自身非法则归类 REJECTED", snap.balance("A"))

    def test_wallet_conflict_classified(self) -> None:
        svc = RiskService()
        svc.ingest_wallet(Wallet("W"))
        result = svc.ingest_wallet(Wallet("W", high_risk=True, risk_tags=("x",)))
        log.info("钱包冲突: address=W 改 high_risk -> %s %s", result.status.value, result.reason)
        self.assertEqual(result.status, IngestStatus.CONFLICT)
        self.assertEqual(result.reason, "钱包字段冲突[high_risk]")

    def test_invalid_transactions_rejected(self) -> None:
        svc = RiskService()
        svc.ingest_wallet(Wallet("A"))
        svc.ingest_wallet(Wallet("B"))
        cases = [
            ("金额非正", Transaction("e1", 1, 0, "A", "B", 0)),
            ("未知付款方", Transaction("e2", 1, 0, "X", "B", 10)),
            ("未知收款方", Transaction("e3", 1, 0, "A", "Y", 10)),
            ("收付款相同", Transaction("e4", 1, 0, "A", "A", 10)),
        ]
        for name, tx in cases:
            result = svc.ingest_transaction(tx)
            log.info("非法输入[%s] %s -> %s %s", name, tx.tx_id, result.status.value, result.detail)
            self.assertEqual(result.status, IngestStatus.REJECTED)
        self.assertEqual(len(svc.snapshot().transactions), 0)

    def test_out_of_order_playback(self) -> None:
        """迟到的历史交易必须按区块顺序回放，且评分只反映当前已提交集合。"""
        svc = RiskService()
        for w in _demo_wallets():
            svc.ingest_wallet(w)
        txs = _demo_txs()  # c1/b1, c2/b1, c3/b1 —— 汇聚 3 笔

        # 1) 先只提交区块 1 的 c3（顺序倒置）
        svc.ingest_transaction(txs[2])
        before = svc.evaluate("HUB")
        log.info("阶段1 输入: 仅 c3(U3->HUB) -> verdict=%s 因子触发=%s",
                 before.verdict.value, [f.code.value for f in before.factors if f.triggered])
        self.assertEqual(before.verdict.value, "UNDETERMINED")
        self.assertEqual(before.evaluated_tip, (1, 2))

        # 2) 迟到补提交 c1/c2，内部存储仍按 (block, index) 规范排序
        svc.ingest_transaction(txs[0])
        svc.ingest_transaction(txs[1])
        snap = svc.snapshot()
        ordered = [t.tx_id for t in snap.transactions]
        log.info("阶段2 输入: 迟到补 c1,c2 -> 账本规范顺序=%s", ordered)
        self.assertEqual(ordered, ["c1", "c2", "c3"])
        self.assertEqual(snap.balance("HUB"), 600)

        after = svc.evaluate("HUB")
        triggered = [f.code.value for f in after.factors if f.triggered]
        log.info("判定依据: 回放后 verdict=%s score=%s 触发因子=%s",
                 after.verdict.value, after.score, triggered)
        self.assertIn("CONCENTRATION", triggered)
        self.assertEqual(after.evaluated_tip, (1, 2))

    def test_shuffled_full_playback_matches_canonical(self) -> None:
        """任意打乱顺序提交，最终账本顺序、余额、评分必须与规范顺序一致。"""
        from app import synthdata

        canonical = RiskService.bootstrap_demo()
        shuffled = RiskService()
        for w in synthdata.build_wallets():
            shuffled.ingest_wallet(w)
        txs = synthdata.build_transactions()
        rng = random.Random(20260923)
        rng.shuffle(txs)
        for t in txs:
            shuffled.ingest_transaction(t)

        s1, s2 = canonical.snapshot(), shuffled.snapshot()
        self.assertEqual([canonical_key(t) for t in s1.transactions],
                         [canonical_key(t) for t in s2.transactions])
        self.assertEqual(s1.balances, s2.balances)
        self.assertEqual(s1.tip, s2.tip)
        for addr in s1.wallets:
            r1, r2 = canonical.evaluate(addr), shuffled.evaluate(addr)
            self.assertEqual(r1.score, r2.score)
            self.assertEqual(r1.verdict, r2.verdict)
        log.info("乱序回放判定依据: %d 笔交易打乱提交后，顺序/余额/tip/全部评分与串行一致",
                 len(s1.transactions))


if __name__ == "__main__":
    unittest.main(verbosity=2)
