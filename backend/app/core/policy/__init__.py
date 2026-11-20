"""通用策略契约层（跨业务、零业务词汇）。

只放"契约"，不放"规则"：具体阈值与判定逻辑由各业务插件自行实现。
"""

from app.core.policy.protocols import (
    RiskClassification,
    RiskLevel,
    RiskPolicyProtocol,
)

__all__ = [
    "RiskClassification",
    "RiskLevel",
    "RiskPolicyProtocol",
]