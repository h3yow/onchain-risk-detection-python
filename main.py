"""本地链上交易风险评分服务入口。

启动：uv run uvicorn main:app --reload
"""
from fastapi import FastAPI

from risk_engine.api import create_router
from risk_engine.service import RiskService

service = RiskService()
app = FastAPI(title="Local On-chain Risk Detection")
app.include_router(create_router(service))


@app.get("/")
def read_root() -> dict:
    return {"service": "onchain-risk-detection", "status": "ok"}
