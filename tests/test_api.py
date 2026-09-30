"""HTTP 层：幂等、冲突状态码归类、账本与风险查询。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def _tx(tid="t1", **kw):
    base = dict(tx_id=tid, from_addr="A", to_addr="B", amount=10.0,
                block_height=1, block_index=0, timestamp=1.0)
    base.update(kw)
    return base


def test_ingest_duplicate_and_conflict_over_http(client):
    r = client.post("/transactions", json=_tx())
    assert r.status_code == 200 and r.json()["accepted"] is True

    r = client.post("/transactions", json=_tx())
    assert r.status_code == 200 and r.json()["duplicate"] is True

    r = client.post("/transactions", json=_tx(amount=999.0))
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "ConflictError"
    assert body["field"] == "amount", "冲突原因必须可区分到字段"


def test_validation_error_status(client):
    r = client.post("/transactions", json=_tx(amount=-1))
    assert r.status_code == 422


def test_unknown_risk_is_reported(client):
    # 全新 tx_id 保证地址数据不足
    payload = _tx(tid="lonely-tx", from_addr="LONELY_A", to_addr="LONELY_B")
    client.post("/transactions", json=payload)
    r = client.get("/risk/LONELY_A")
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == "UNKNOWN"
    assert body["insufficient_reasons"], "必须给出无法判定原因"


def test_ledger_endpoint_consistent(client):
    r = client.get("/ledger")
    assert r.status_code == 200
    assert set(r.json()) >= {"balances", "nonces", "last_block", "tx_count"}


def test_risk_response_is_backward_compatible_and_rich(client):
    # 原有字段全部保留，新增维度证据状态与完整度汇总
    payload = _tx(tid="rich-tx", from_addr="RICH_A", to_addr="RICH_B")
    client.post("/transactions", json=payload)
    r = client.get("/risk/RICH_A")
    assert r.status_code == 200
    body = r.json()

    # 原有字段不删不改
    assert set(body) >= {
        "address", "score", "level", "factors",
        "insufficient_reasons", "evaluated_at_block", "determinism_key",
    }
    assert body["level"] == "UNKNOWN"  # 低信息账户不得给分级
    factor = body["factors"][0]
    assert set(factor) >= {"code", "triggered", "weight", "detail", "related"}
    # 新增的维度级字段
    assert factor["evidence_status"] in {"SUFFICIENT", "INSUFFICIENT", "NO_DATA"}
    assert "gap_code" in factor and "gap_detail" in factor

    comp = body["completeness"]
    assert set(comp) == {
        "total_dimensions", "evaluated_dimensions", "coverage_ratio",
        "confidence", "missing_dimensions",
    }
    assert comp["total_dimensions"] == 6
    assert comp["evaluated_dimensions"] + len(comp["missing_dimensions"]) == 6
    assert comp["confidence"] in {"HIGH", "MEDIUM", "LOW"}
    missing = comp["missing_dimensions"][0]
    assert set(missing) == {"code", "evidence_status", "gap_code", "gap_detail"}

    assert body["inconclusive_reasons"], "覆盖不足时必须逐条给出下不了结论的原因"
    codes = {x["code"] for x in body["inconclusive_reasons"]}
    assert "INSUFFICIENT_DIMENSION_COVERAGE" in codes
