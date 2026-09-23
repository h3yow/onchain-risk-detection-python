"""风险因子、阈值判定、数据不足与确定性稳定性测试。"""
from __future__ import annotations

from risk_engine.models import FactorCode, RiskLevel, Transaction
from risk_engine.scoring import RiskScorer
from tests.helpers import log_decision, log_stream


def _ingest(service, txs, logger, note):
    log_stream(logger, txs, note)
    service.ingest_many(txs)


def test_explainable_factors_on_synthetic_dataset(service, canonical_txs, logger):
    _ingest(service, canonical_txs, logger, "(合成全量)")
    expected = {
        "X": {FactorCode.FUND_CONVERGENCE, FactorCode.FUND_DISPERSION},
        "R": {FactorCode.RING_FLOW},
        "F": {FactorCode.RING_FLOW, FactorCode.HIGH_FREQ_INTERACTION},
        "VIC": {FactorCode.HIGH_RISK_DIRECT},
        "MID": {FactorCode.HIGH_RISK_INDIRECT},
        "U": {FactorCode.HIGH_RISK_INDIRECT},
        "H": {
            FactorCode.FUND_CONVERGENCE,
            FactorCode.FUND_DISPERSION,
            FactorCode.HIGH_FREQ_INTERACTION,
            FactorCode.HIGH_RISK_DIRECT,
        },
    }
    for addr, codes in expected.items():
        score = service.evaluate(addr)
        log_decision(logger, score)
        hit = {f.code for f in score.factors if f.triggered}
        assert hit == codes, f"{addr} 因子集合不符: {hit}"
        # 每个触发的因子都必须有非空依据与正权重
        for f in score.factors:
            if f.triggered:
                assert f.weight > 0 and f.detail
                if f.code in (FactorCode.FUND_CONVERGENCE, FactorCode.FUND_DISPERSION,
                              FactorCode.HIGH_RISK_DIRECT, FactorCode.HIGH_RISK_INDIRECT):
                    assert f.related, f"{addr} {f.code} 应给出关联地址"


def test_threshold_levels(service, canonical_txs, logger):
    _ingest(service, canonical_txs, logger, "(阈值分级)")
    levels = {a: service.evaluate(a).level for a in ("H", "X", "VIC", "MID", "CLEAN")}
    assert levels["H"] is RiskLevel.HIGH
    assert levels["X"] is RiskLevel.MEDIUM
    assert levels["VIC"] is RiskLevel.MEDIUM
    assert levels["MID"] is RiskLevel.LOW
    assert levels["CLEAN"] is RiskLevel.LOW


def test_insufficient_data_is_unknown_not_safe(service, logger):
    # 全新服务，只有 1 笔交易；相关地址参与笔数不足
    tx = Transaction("only-1", "A", "B", 10.0, 1, 0, 1.0)
    log_stream(logger, [tx], "(数据不足)")
    service.ingest_transaction(tx)
    for addr in ("A", "B", "NEVER_SEEN"):
        score = service.evaluate(addr)
        log_decision(logger, score)
        assert score.level is RiskLevel.UNKNOWN, "数据不足不得判为安全"
        codes = {r.code for r in score.insufficient_reasons}
        assert "INSUFFICIENT_TRANSACTIONS" in codes


def test_empty_ledger_is_unknown(service, logger):
    score = service.evaluate("ANYONE")
    log_decision(logger, score)
    assert score.level is RiskLevel.UNKNOWN
    assert {r.code for r in score.insufficient_reasons} >= {"EMPTY_LEDGER", "INSUFFICIENT_TRANSACTIONS"}


def test_determinism_same_input_same_output(canonical_txs, logger):
    high_risk = {"BAD"}
    s1 = RiskScorer(canonical_txs, high_risk).evaluate("X")
    s2 = RiskScorer(list(reversed(canonical_txs)), high_risk).evaluate("X")  # 故意乱序传入
    s3 = RiskScorer(canonical_txs, high_risk).evaluate("X")
    log_decision(logger, s1)
    assert s1 == s2 == s3, "同一输入（含乱序到达）多次评估结论必须稳定"
    assert s1.determinism_key == s2.determinism_key


def test_different_input_different_key(canonical_txs):
    k1 = RiskScorer.determinism_key("X", canonical_txs, {"BAD"})
    k2 = RiskScorer.determinism_key("Y", canonical_txs, {"BAD"})
    k3 = RiskScorer.determinism_key("X", canonical_txs, {"OTHER"})
    assert len({k1, k2, k3}) == 3
