"""领域模型：钱包、交易、摄入结果与评分结果的类型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class IngestStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"


class Verdict(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNDETERMINED = "UNDETERMINED"


class FactorCode(str, Enum):
    CONCENTRATION = "CONCENTRATION"          # 资金汇聚
    DISPERSION = "DISPERSION"                # 资金分散
    HIGH_FREQUENCY = "HIGH_FREQUENCY"        # 短时高频交互
    RING_FLOW = "RING_FLOW"                  # 环形流转
    HIGH_RISK_LINK = "HIGH_RISK_LINK"        # 高风险地址关联


@dataclass(frozen=True)
class Wallet:
    address: str
    label: str = ""
    high_risk: bool = False
    risk_tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Transaction:
    tx_id: str
    block: int
    tx_index: int
    sender: str
    receiver: str
    amount: int
    timestamp: int = 0


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    entity_id: str
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class FactorEvidence:
    code: FactorCode
    triggered: bool
    score: float
    explanation: str
    data_points: dict = field(default_factory=dict)
    insufficient: bool = False
    insufficient_reason: str = ""


@dataclass(frozen=True)
class RiskReport:
    address: str
    score: float
    verdict: Verdict
    factors: tuple[FactorEvidence, ...]
    undetermined_reasons: tuple[str, ...] = ()
    evaluated_tip: tuple[int, int] = (0, 0)
