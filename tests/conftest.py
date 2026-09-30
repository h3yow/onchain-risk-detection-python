"""pytest 夹具：自动打印输入流水与判定依据。

通过 --log-cli-level=INFO（见 pyproject 配置），每个用例都会输出：
- INGEST-STREAM：实际提交的交易流水（含重复/乱序/冲突）；
- DECISION：每个被评估地址的级别、分数与每个因子的判定依据。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from main import app
from risk_engine.models import Transaction
from risk_engine.service import RiskService
from risk_engine import synthetic



@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("risk-tests")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def service() -> RiskService:
    svc = RiskService()
    for w in synthetic.build_wallets():
        svc.register_wallet(w)
    return svc


@pytest.fixture
def canonical_txs() -> list[Transaction]:
    return synthetic.build_transactions()
