"""电网检修调度风险定级策略（垂直插件私有）。

core 只定义 RiskPolicyProtocol（app/core/policy/protocols.py），
**全部规则在本文件下沉**；core 不感知电压等级等任何电网概念。

定级规则（二值，无灰区）：
    电压等级无法识别 / 越界  → UNKNOWN，必须人工（保守兜底）
    电压等级 ≥ 110kV          → HIGH，   必须人工核签
    电压等级 < 110kV          → LOW，    可自动签发调度许可令

【解析铁律】
1. 负号不参与匹配："-110kV" 不得被剥掉负号后误判成 110kV。
2. 物理范围校验：提取后必须满足 0 < kv ≤ 1000，越界一律视为非法输入。
3. 任何解析失败都返回 None → UNKNOWN → 转人工，绝不落到自动放行。
"""

import re
from typing import Any, Mapping

from app.core.policy.protocols import RiskClassification
from app.plugins.power_grid.constants import (
    POWER_HUMAN_REVIEW_KV_MIN,
    POWER_RISK_LEVEL_HIGH,
    POWER_RISK_LEVEL_LOW,
    POWER_RISK_LEVEL_UNKNOWN,
    POWER_VOLTAGE_KV_MAX,
)

# 带单位的电压等级（"110kV" / "220 kV" / "110千伏"）
# 负向前瞻：数字前若紧邻数字、小数点或负号则不匹配 —— 保证 "-110kV" 不被误取。
KV_WITH_UNIT_PATTERN = re.compile(
    r"(?<![\d.\-])(\d+(?:\.\d+)?)\s*(?:kV|千伏)",
    re.IGNORECASE,
)
# 纯数字电压等级（"50"）：按 kV 解释；负号不被接受
KV_BARE_NUMBER_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)$")

# 定级规则标识（写入 matched_rules，供审计追溯到具体规则）
RULE_VOLTAGE_UNRECOGNIZED = "VOLTAGE_UNRECOGNIZED"
RULE_VOLTAGE_GE_HUMAN_REVIEW_MIN = "VOLTAGE_GE_HUMAN_REVIEW_MIN"
RULE_VOLTAGE_LT_HUMAN_REVIEW_MIN = "VOLTAGE_LT_HUMAN_REVIEW_MIN"


def extract_kv(voltage_level: str | None) -> int | None:
    """从电压等级文本中提取 kV 数值。

    返回 None 的三种情形（调用方一律按"无法判定"转人工）：
    - 空值或非字符串；
    - 文本无法解析（含负号写法，如 "-110kV"）；
    - 解析结果越界（kv ≤ 0 或 kv > 1000）。
    """
    if not voltage_level:
        return None
    raw = str(voltage_level).strip()
    if not raw:
        return None

    match = KV_WITH_UNIT_PATTERN.search(raw)
    if match is None:
        bare = KV_BARE_NUMBER_PATTERN.match(raw)
        if bare is None:
            return None
        match = bare

    try:
        kv = int(float(match.group(1)))
    except (TypeError, ValueError):
        return None

    # 物理范围校验：越界值属非法输入，不得据以自动放行
    if kv <= 0 or kv > POWER_VOLTAGE_KV_MAX:
        return None
    return kv


class PowerGridRiskPolicy:
    """电网检修调度风险定级策略（实现 core 的 RiskPolicyProtocol）。

    方法为纯函数、无副作用：状态机在 resume 时会重放节点体，
    任何写库 / 写外部系统的动作放在这里都会被执行两次。
    """

    async def classify(self, subject: Mapping[str, Any]) -> RiskClassification:
        """按电压等级定级：判定该工单是否必须人工核签。"""
        kv = extract_kv(subject.get("voltage_level"))
        if kv is None:
            return RiskClassification(
                risk_level=POWER_RISK_LEVEL_UNKNOWN,
                requires_human=True,
                reason="电压等级无法识别或超出有效范围，按保守原则转人工核签",
                matched_rules=[RULE_VOLTAGE_UNRECOGNIZED],
            )
        if kv >= POWER_HUMAN_REVIEW_KV_MIN:
            return RiskClassification(
                risk_level=POWER_RISK_LEVEL_HIGH,
                requires_human=True,
                reason=f"{kv}kV 属高压及以上检修，必须人工核签",
                matched_rules=[RULE_VOLTAGE_GE_HUMAN_REVIEW_MIN],
            )
        return RiskClassification(
            risk_level=POWER_RISK_LEVEL_LOW,
            requires_human=False,
            reason=f"{kv}kV 属常规检修，安措齐备后自动签发调度许可令",
            matched_rules=[RULE_VOLTAGE_LT_HUMAN_REVIEW_MIN],
        )


__all__ = [
    "PowerGridRiskPolicy",
    "extract_kv",
]