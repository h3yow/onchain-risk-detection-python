"""FastAPI HTTP 接口。

端点：
- POST /wallets              摄入钱包（幂等）
- POST /transactions         摄入交易（幂等，乱序到达按区块回放）
- GET  /wallets/{addr}/risk  查询风险报告（基于一致性快照）
- GET  /wallets/{addr}       查询钱包与余额
- GET  /ledger/snapshot      全量账本快照
- GET  /                     服务与默认演示数据概览

状态码约定：
- 200 ACCEPTED / DUPLICATE（幂等命中）
- 409 CONFLICT（标识相同内容不一致，body 含可区分冲突字段）
- 422 REJECTED（数据非法）
- 404 查询对象不存在

注意：service 调用是阻塞式（内部持锁），统一经线程池执行，
避免阻塞 FastAPI 事件循环；锁串行化保证并发摄入与串行等价。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.errors import LedgerNotFoundError
from app.models import IngestStatus, Transaction, Wallet
from app.service import RiskService


class WalletIn(BaseModel):
    address: str
    label: str = ""
    high_risk: bool = False
    risk_tags: list[str] = Field(default_factory=list)


class TransactionIn(BaseModel):
    tx_id: str
    block: int = Field(ge=0)
    tx_index: int = Field(ge=0, default=0)
    sender: str
    receiver: str
    amount: int = Field(gt=0)
    timestamp: int = 0


def create_app(service: RiskService | None = None) -> FastAPI:
    svc = service if service is not None else RiskService()
    # 单线程执行器：所有摄入请求在此排队，应用层顺序与串行处理一致
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")

    async def _call(fn, *args):
        return await asyncio.get_running_loop().run_in_executor(pool, fn, *args)

    app = FastAPI(title="本地链上交易风险评分服务", version="0.1.0")

    def _raise_for_ingest(result) -> None:
        if result.status == IngestStatus.CONFLICT:
            raise HTTPException(status_code=409, detail={
                "error": "CONTENT_CONFLICT",
                "entity_id": result.entity_id,
                "reason": result.reason,
                "detail": result.detail,
            })
        if result.status == IngestStatus.REJECTED:
            raise HTTPException(status_code=422, detail={
                "error": "INVALID_TRANSACTION",
                "entity_id": result.entity_id,
                "reason": result.reason,
                "detail": result.detail,
            })

    @app.post("/wallets")
    async def post_wallet(body: WalletIn):
        wallet = Wallet(body.address, body.label, body.high_risk, tuple(body.risk_tags))
        result = await _call(svc.ingest_wallet, wallet)
        _raise_for_ingest(result)
        return {"status": result.status.value, "entity_id": result.entity_id,
                "reason": result.reason}

    @app.post("/transactions")
    async def post_transaction(body: TransactionIn):
        result = await _call(svc.ingest_transaction, Transaction(**body.__dict__))
        _raise_for_ingest(result)
        return {"status": result.status.value, "entity_id": result.entity_id,
                "reason": result.reason, "detail": result.detail}

    @app.get("/wallets/{address}/risk")
    async def get_risk(address: str):
        def _eval():
            try:
                return svc.evaluate(address)
            except LedgerNotFoundError as exc:
                raise HTTPException(status_code=404, detail={"error": exc.code,
                                                             "detail": str(exc)})
        rep = await _call(_eval)
        return {
            "address": rep.address,
            "score": rep.score,
            "verdict": rep.verdict.value,
            "evaluated_tip": {"block": rep.evaluated_tip[0], "tx_index": rep.evaluated_tip[1]},
            "undetermined_reasons": list(rep.undetermined_reasons),
            "factors": [
                {"code": f.code.value, "triggered": f.triggered, "score": f.score,
                 "explanation": f.explanation, "data_points": f.data_points,
                 "insufficient": f.insufficient,
                 "insufficient_reason": f.insufficient_reason}
                for f in rep.factors
            ],
        }

    @app.get("/wallets/{address}")
    async def get_wallet(address: str):
        snap = await _call(svc.snapshot)
        if address not in snap.wallets:
            raise HTTPException(status_code=404,
                                detail={"error": "NOT_FOUND", "detail": f"钱包不存在: {address}"})
        w = snap.wallets[address]
        return {"address": w.address, "label": w.label, "high_risk": w.high_risk,
                "risk_tags": list(w.risk_tags), "balance": snap.balance(address),
                "tx_count": len(snap.transactions_for(address))}

    @app.get("/ledger/snapshot")
    async def get_snapshot():
        snap = await _call(svc.snapshot)
        return {
            "tip": {"block": snap.tip[0], "tx_index": snap.tip[1]},
            "wallet_count": len(snap.wallets),
            "transaction_count": len(snap.transactions),
            "balances": snap.balances,
            "transactions": [
                {"tx_id": t.tx_id, "block": t.block, "tx_index": t.tx_index,
                 "sender": t.sender, "receiver": t.receiver, "amount": t.amount,
                 "timestamp": t.timestamp}
                for t in snap.transactions
            ],
        }

    @app.get("/")
    async def root():
        snap = await _call(svc.snapshot)
        return {
            "service": "onchain-risk-detection",
            "wallets": len(snap.wallets),
            "transactions": len(snap.transactions),
            "chain_tip": {"block": snap.tip[0], "tx_index": snap.tip[1]},
            "risk_endpoint": "/wallets/{address}/risk",
        }

    app.state.service = svc
    return app
