"""通用风险定级策略契约（跨业务、零业务词汇）。

core 只定义"如何表达一个定级结论"与"谁来给出结论"，
**绝不定义任何定级规则**（什么算高风险由各业务插件自行实现）。

定级结论里的 requires_human 是**路由的唯一权威信号**：通用层只读它，
不解释 risk_level 的业务含义（新增等级无需改动 core）。
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# 风险等级（通用三档）：
#   LOW      —— 具备"可自动放行"资格的档位
#   HIGH     —— 必须人工介入的档位
#   UNKNOWN  —— 无法判定：保守起见必须人工
RiskLevel = Literal["LOW", "HIGH", "UNKNOWN"]


class RiskClassification(BaseModel):
    """定级结论。

    requires_human 是路由的唯一权威信号；reason 与 matched_rules 用于审计：
    事后能追溯"当时按哪条规则、为什么这样判"。
    """

    risk_level: RiskLevel
    requires_human: bool
    reason: str
    matched_rules: list[str] = Field(default_factory=list)


@runtime_checkable
class RiskPolicyProtocol(Protocol):
    """风险定级策略 SPI：通用层定义接口，业务插件实现规则。"""

    async def classify(self, subject: Mapping[str, Any]) -> RiskClassification:
        """对待判定主体给出定级结论。

        实现方必须保证：**任何异常路径都返回 requires_human=True**，
        不得因异常、解析失败或取值越界而落到"自动放行"。
        """
        ...


__all__ = [
    "RiskClassification",
    "RiskLevel",
    "RiskPolicyProtocol",
]