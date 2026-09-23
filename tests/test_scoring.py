"""风险评分测试：每个因子可单独解释，同输入结论稳定，数据不足不得判安全。"""
from __future__ import annotations

import logging
import unittest

from app.models import FactorCode, Transaction, Verdict, Wallet
from app.service import RiskService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("scoring")


def _wallets(names: list[str], bad: dict[str, tuple[str, ...]] | None = None) -> list[Wallet]:
    bad = bad or {}
    return [Wallet(n, high_risk=n in bad, risk_tags=bad.get(n, ())) for n in names]


class FactorTests(unittest.TestCase):
    def _factor(self, svc: RiskService, addr: str, code: FactorCode):
        rep = svc.evaluate(addr)
        return rep, next(f for f in rep.factors if f.code == code)

    def test_concentration_factor(self) -> None:
        svc = RiskService()
        for w in _wallets(["T", "a", "b", "c"]):
            svc.ingest_wallet(w)
        for i, s in enumerate("abc"):
            svc.ingest_transaction(Transaction(f"k{i}", 1, i, s, "T", 100))
        rep, f = self._factor(svc, "T", FactorCode.CONCENTRATION)
        log.info("输入流水: a/b/c 各向 T 汇入 100 -> CONCENTRATION triggered=%s 依据=%s",
                 f.triggered, f.explanation)
        self.assertTrue(f.triggered)
        self.assertGreater(f.score, 0)
        self.assertEqual(f.data_points["source_count"], 3)

    def test_dispersion_factor(self) -> None:
        svc = RiskService()
        for w in _wallets(["T", "x", "y", "z"]):
            svc.ingest_wallet(w)
        # 预置入账，保证 T 有钱可转出
        svc.ingest_transaction(Transaction("seed", 1, 0, "x", "T", 1000))
        for i, d in enumerate(["x", "y", "z"], start=1):
            svc.ingest_transaction(Transaction(f"p{i}", i, 0, "T", d, 50))
        rep, f = self._factor(svc, "T", FactorCode.DISPERSION)
        log.info("输入流水: T 向 x/y/z 分散转出 -> DISPERSION triggered=%s 依据=%s",
                 f.triggered, f.explanation)
        self.assertTrue(f.triggered)
        self.assertEqual(f.data_points["target_count"], 3)

    def test_high_frequency_factor(self) -> None:
        svc = RiskService()
        for w in _wallets(["T", "Q"]):
            svc.ingest_wallet(w)
        rows = [("h1", 5, 0, "T", "Q", 1), ("h2", 5, 1, "Q", "T", 1),
                ("h3", 6, 0, "T", "Q", 1)]
        for r in rows:
            svc.ingest_transaction(Transaction(*r))
        rep, f = self._factor(svc, "T", FactorCode.HIGH_FREQUENCY)
        log.info("输入流水: T<->Q 在区块5~6内交互3笔 -> triggered=%s 依据=%s",
                 f.triggered, f.explanation)
        self.assertTrue(f.triggered)
        self.assertEqual(f.data_points["counterparty"], "Q")
        self.assertLessEqual(f.data_points["block_span"], 2)

    def test_ring_factor(self) -> None:
        svc = RiskService()
        for w in _wallets(["A", "B", "C"]):
            svc.ingest_wallet(w)
        svc.ingest_transaction(Transaction("g1", 1, 0, "A", "B", 10))
        svc.ingest_transaction(Transaction("g2", 1, 1, "B", "C", 10))
        svc.ingest_transaction(Transaction("g3", 1, 2, "C", "A", 10))
        rep, f = self._factor(svc, "A", FactorCode.RING_FLOW)
        log.info("输入流水: A->B->C->A -> RING_FLOW triggered=%s 依据=%s",
                 f.triggered, f.explanation)
        self.assertTrue(f.triggered)
        self.assertEqual(f.data_points["ring_nodes"], ["A", "B", "C", "A"])

    def test_high_risk_direct_and_indirect(self) -> None:
        svc = RiskService()
        for w in _wallets(["D", "I", "M", "B1", "B2"],
                          bad={"B1": ("sanctioned",), "B2": ("mixer",)}):
            svc.ingest_wallet(w)
        # 直连：D->B1
        svc.ingest_transaction(Transaction("z1", 1, 0, "D", "B1", 9))
        svc.ingest_transaction(Transaction("z0", 1, 1, "D", "M", 1))
        _, f = self._factor(svc, "D", FactorCode.HIGH_RISK_LINK)
        log.info("输入流水: D->B1(标记) -> triggered=%s weight=%.2f 依据=%s",
                 f.triggered, f.score, f.explanation)
        self.assertTrue(f.triggered)
        self.assertAlmostEqual(f.score, 0.30)

        # 间接：I->M->B2，且 I 有足够的普通交易
        svc.ingest_transaction(Transaction("z2", 1, 2, "I", "M", 9))
        svc.ingest_transaction(Transaction("z3", 1, 3, "M", "B2", 9))
        svc.ingest_transaction(Transaction("z4", 2, 0, "I", "D", 1))
        _, f2 = self._factor(svc, "I", FactorCode.HIGH_RISK_LINK)
        log.info("输入流水: I->M->B2(标记) -> triggered=%s weight=%.2f 依据=%s",
                 f2.triggered, f2.score, f2.explanation)
        self.assertTrue(f2.triggered)
        self.assertAlmostEqual(f2.score, 0.15)

    def test_no_risk_clean_wallet_is_low(self) -> None:
        svc = RiskService()
        for w in _wallets(["N", "P", "K", "S"]):
            svc.ingest_wallet(w)
        # 线性资金流、无环、无汇聚分散高频高风险：N 2 笔出，K 2 笔入
        svc.ingest_transaction(Transaction("m1", 1, 0, "N", "P", 10))
        svc.ingest_transaction(Transaction("m2", 2, 0, "P", "K", 5))
        svc.ingest_transaction(Transaction("m3", 3, 0, "N", "S", 2))
        svc.ingest_transaction(Transaction("m4", 4, 0, "P", "S", 2))
        rep = svc.evaluate("N")
        log.info("输入流水: 普通线性往来(无环无汇聚) -> verdict=%s score=%s",
                 rep.verdict.value, rep.score)
        self.assertEqual(rep.verdict, Verdict.LOW)
        self.assertFalse(any(f.triggered for f in rep.factors))


class DeterminismTest(unittest.TestCase):
    def test_same_input_stable_conclusion(self) -> None:
        from app import synthdata

        svc = RiskService.bootstrap_demo()
        addresses = [w.address for w in synthdata.build_wallets()]
        # 同一地址重复评估 20 次结果完全一致
        for addr in addresses:
            first = svc.evaluate(addr)
            for _ in range(19):
                self.assertEqual(svc.evaluate(addr), first)
        log.info("稳定性判定依据: %d 个钱包各评估 20 次，报告(frozen)全部相等", len(addresses))

    def test_synthetic_verdicts_match_expected(self) -> None:
        svc = RiskService.bootstrap_demo()
        expected = {"SUS": Verdict.HIGH, "R1": Verdict.MEDIUM, "DIR": Verdict.MEDIUM,
                    "QUIET": Verdict.UNDETERMINED, "NORMAL": Verdict.LOW}
        for addr, verdict in expected.items():
            rep = svc.evaluate(addr)
            log.info("合成场景 %s -> verdict=%s score=%s 触发=%s",
                     addr, rep.verdict.value, rep.score,
                     [f.code.value for f in rep.factors if f.triggered])
            self.assertEqual(rep.verdict, verdict)


class InsufficientDataTest(unittest.TestCase):
    def test_empty_wallet_undetermined(self) -> None:
        svc = RiskService()
        svc.ingest_wallet(Wallet("SOLO"))
        rep = svc.evaluate("SOLO")
        log.info("输入流水: 空钱包 SOLO -> verdict=%s 原因数=%d（不得默认安全）",
                 rep.verdict.value, len(rep.undetermined_reasons))
        self.assertEqual(rep.verdict, Verdict.UNDETERMINED)
        self.assertTrue(rep.undetermined_reasons)
        self.assertTrue(all(f.insufficient for f in rep.factors))

    def test_single_tx_undetermined_not_safe(self) -> None:
        svc = RiskService()
        for w in _wallets(["Q", "R"]):
            svc.ingest_wallet(w)
        svc.ingest_transaction(Transaction("only", 1, 0, "Q", "R", 10))
        rep = svc.evaluate("Q")
        log.info("输入流水: Q 仅 1 笔交易 -> verdict=%s 原因=%s",
                 rep.verdict.value, rep.undetermined_reasons[0])
        self.assertEqual(rep.verdict, Verdict.UNDETERMINED)
        self.assertNotEqual(rep.verdict, Verdict.LOW)

    def test_factor_level_insufficient_reasons(self) -> None:
        svc = RiskService()
        svc.ingest_wallet(Wallet("SOLO"))
        rep = svc.evaluate("SOLO")
        for f in rep.factors:
            self.assertTrue(f.insufficient)
            self.assertTrue(f.insufficient_reason)
        log.info("因子级判定依据: 5 个因子均给出数据不足原因: %s",
                 [f.code.value + "=" + f.insufficient_reason[:18] for f in rep.factors])


if __name__ == "__main__":
    unittest.main(verbosity=2)
