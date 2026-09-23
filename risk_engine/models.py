"""领域类型定义：钱包、交易、摄入结果、风险因子与评分结论。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class WalletType(str, Enum):
    NORMAL = "NORMAL"
    HIGH_RISK = "HIGH_RISK"  # 已知高风险地址


class TxDirection(str, Enum):
    OUTGOING = "OUTGOING"
    INCOMING = "INCOMING"


class FactorCode(str, Enum):
    """可解释风险因子代码。"""
    FUND_CONVERGENCE = "FUND_CONVERGENCE"        # 资金汇聚
    FUND_DISPERSION = "FUND_DISPERSION"          # 资金分散
    RING_FLOW = "RING_FLOW"                      # 环形流转
    HIGH_FREQ_INTERACTION = "HIGH_FREQ_INTERACTION"  # 短时高频交互
    HIGH_RISK_DIRECT = "HIGH_RISK_DIRECT"        # 与高风险地址直接关联
    HIGH_RISK_INDIRECT = "HIGH_RISK_INDIRECT"    # 与高风险地址间接关联


@dataclass(frozen=True)
class Wallet:
    address: str
    wallet_type: WalletType = WalletType.NORMAL


@dataclass(frozen=True)
class Transaction:
    """一笔链上转账。

    block_height/block_index 决定全序；同一 tx_id 必须始终携带相同内容。
    """
    tx_id: str
    from_addr: str
    to_addr: str
    amount: float
    block_height: int
    block_index: int = 0
    timestamp: float = 0.0


@dataclass(frozen=True)
class IngestResult:
    tx_id: str
    accepted: bool
    duplicate: bool          # 幂等命中：内容一致的重复提交
    reason: str | None = None


@dataclass(frozen=True)
class FactorEvidence:
    code: FactorCode
    triggered: bool
    weight: float            # 该因子贡献分值（0 表示未触发）
    detail: str              # 人可读的判定依据
    related: tuple[str, ...] = ()


@dataclass(frozen=True)
class Insufficiency:
    """数据不足/无法判定的原因。"""
    code: str
    detail: str


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"  # 数据不足，不得判为安全


@dataclass(frozen=True)
class RiskScore:
    address: str
    score: float
    level: RiskLevel
    factors: tuple[FactorEvidence, ...]
    insufficient_reasons: tuple[Insufficiency, ...]
    evaluated_at_block: int
    determinism_key: str  # 输入指纹，保证同输入同结论


@dataclass(frozen=True)
class LedgerView:
    """一致性查询快照：某一时刻账本的不可变视图。"""
    balances: dict[str, float] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    last_block: int = 0
    tx_count: int = 0
