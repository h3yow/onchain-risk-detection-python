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
