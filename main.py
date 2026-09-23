"""本地入口。

- HTTP 服务：uv run uvicorn main:app
- 终端演示：uv run python main.py demo
"""
from __future__ import annotations

import sys

from app.api import create_app
from app.service import RiskService

service = RiskService.bootstrap_demo()
app = create_app(service)


def _run_demo() -> None:
    from app import synthdata

    svc = RiskService.bootstrap_demo()
    print("=" * 78)
    print("本地合成链上数据风险评分演示")
    print("=" * 78)
    print(f"钱包 {len(synthdata.build_wallets())} 个，交易 "
          f"{len(synthdata.build_transactions())} 笔，链顶 {svc.snapshot().tip}")
    print("-" * 78)
    for addr in ("HUB", "SPREAD", "FREQ_A", "R1", "INDIR", "DIR",
                 "QUIET", "NORMAL", "SUS"):
        rep = svc.evaluate(addr)
        print(f"\n[{addr}] 结论={rep.verdict.value} 总分={rep.score}")
        for f in rep.factors:
            if f.triggered:
                print(f"  * {f.code.value:<14} +{f.score:<5} {f.explanation}")
            elif f.insufficient:
                print(f"  - {f.code.value:<14} 数据不足: {f.insufficient_reason}")
        for reason in rep.undetermined_reasons:
            print(f"  ! 无法判定: {reason}")
    print("=" * 78)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        _run_demo()
    else:
        print("用法: uv run python main.py demo | uv run uvicorn main:app")
