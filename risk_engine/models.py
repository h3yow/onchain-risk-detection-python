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


class EvidenceStatus(str, Enum):
    """单个风险维度的证据充分程度。

    SUFFICIENT    证据充分，触发/未触发结论都可采信（“查过了确实没问题”）
    INSUFFICIENT  有观测数据但不足以判定（样本方向单一/对手方不足/跨度不够…）
    NO_DATA       该维度完全没有可看的数据（“没查出来问题”，不能当作安全）
    """
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    NO_DATA = "NO_DATA"


class ConfidenceLevel(str, Enum):
    """整体评估完整度（可信度）档位。"""
    HIGH = "HIGH"    # 绝大多数维度可评估
    MEDIUM = "MEDIUM"  # 达到最低可判定覆盖
    LOW = "LOW"      # 可评估维度太少，结论不可采信


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
    # 维度级证据状态：证据充分 / 有观测但不足 / 完全无数据
    evidence_status: EvidenceStatus = EvidenceStatus.SUFFICIENT
    # 证据不足或缺数据时，具体缺什么（稳定代码 + 人可读说明）；充分时为 None
    gap_code: str | None = None
    gap_detail: str | None = None


@dataclass(frozen=True)
class DimensionGap:
    """本次没评上的维度及其缺证据原因。"""
    code: FactorCode
    evidence_status: EvidenceStatus
    gap_code: str
    gap_detail: str


@dataclass(frozen=True)
class Completeness:
    """本次评估的完整度汇总：结论由几个维度支撑、哪些没评上、可信度如何。"""
    total_dimensions: int                 # 风险维度总数（固定为 6）
    evaluated_dimensions: int             # 证据充分、真正参与结论支撑的维度数
    coverage_ratio: float                 # evaluated / total，取值 [0, 1]
    confidence: ConfidenceLevel           # 整体可信度档位
    missing_dimensions: tuple[DimensionGap, ...]  # 没评上的维度及逐条原因


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
    # 维度级证据状态与完整度汇总（新增字段，向后兼容，不改动任何原有字段）
    completeness: Completeness | None = None
    # 下不了结论时逐条列出的维度级原因（与 insufficient_reasons 并存）
    inconclusive_reasons: tuple[Insufficiency, ...] = ()


@dataclass(frozen=True)
class LedgerView:
    """一致性查询快照：某一时刻账本的不可变视图。"""
    balances: dict[str, float] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    last_block: int = 0
    tx_count: int = 0
