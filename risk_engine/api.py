"""FastAPI HTTP 层：钱包注册、交易摄入、账本查询、风险评估。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .errors import ConflictError, RiskEngineError, ValidationError, WalletConflictError
from .models import LedgerView, RiskScore, Transaction, Wallet, WalletType
from .service import RiskService


class WalletIn(BaseModel):
    address: str
    wallet_type: str = "NORMAL"


class TransactionIn(BaseModel):
    tx_id: str
    from_addr: str
    to_addr: str
    amount: float
    block_height: int
    block_index: int = 0
    timestamp: float = 0.0


def _error_status(exc: RiskEngineError) -> int:
    if isinstance(exc, (ConflictError, WalletConflictError)):
        return 409
    if isinstance(exc, ValidationError):
        return 422
    return 400


def _risk_to_dict(r: RiskScore) -> dict:
    return {
        "address": r.address,
        "score": r.score,
        "level": r.level.value,
        "factors": [
            {
                "code": f.code.value,
                "triggered": f.triggered,
                "weight": f.weight,
                "detail": f.detail,
                "related": list(f.related),
            }
            for f in r.factors
        ],
        "insufficient_reasons": [
            {"code": x.code, "detail": x.detail} for x in r.insufficient_reasons
        ],
        "evaluated_at_block": r.evaluated_at_block,
        "determinism_key": r.determinism_key,
    }


def _ledger_to_dict(v: LedgerView) -> dict:
    return {
        "balances": v.balances,
        "nonces": v.nonces,
        "last_block": v.last_block,
        "tx_count": v.tx_count,
    }


def create_router(service: RiskService) -> APIRouter:
    router = APIRouter()

    @router.post("/wallets")
    def register_wallet(payload: WalletIn) -> dict:
        try:
            wtype = WalletType(payload.wallet_type)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={"error": "ValidationError", "detail": f"未知钱包类型 {payload.wallet_type}"},
            )
        try:
            service.register_wallet(Wallet(address=payload.address, wallet_type=wtype))
        except RiskEngineError as exc:
            return JSONResponse(
                status_code=_error_status(exc),
                content={"error": type(exc).__name__, "detail": str(exc)},
            )
        return {"accepted": True, "address": payload.address}

    @router.post("/transactions")
    def ingest_transaction(payload: TransactionIn) -> dict:
        tx = Transaction(
            tx_id=payload.tx_id,
            from_addr=payload.from_addr,
            to_addr=payload.to_addr,
            amount=payload.amount,
            block_height=payload.block_height,
            block_index=payload.block_index,
            timestamp=payload.timestamp,
        )
        try:
            result = service.ingest_transaction(tx)
        except RiskEngineError as exc:
            return JSONResponse(
                status_code=_error_status(exc),
                content={
                    "error": type(exc).__name__,
                    "detail": str(exc),
                    "tx_id": payload.tx_id,
                    **(
                        {"field": getattr(exc, "field_name", None)}
                        if hasattr(exc, "field_name")
                        else {}
                    ),
                },
            )
        return {
            "tx_id": result.tx_id,
            "accepted": result.accepted,
            "duplicate": result.duplicate,
            "reason": result.reason,
        }

    @router.get("/ledger")
    def get_ledger() -> dict:
        return _ledger_to_dict(service.ledger_view())

    @router.get("/risk/{address}")
    def get_risk(address: str) -> dict:
        return _risk_to_dict(service.evaluate(address))

    return router
