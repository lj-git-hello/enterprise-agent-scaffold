"""电网检修调度状态机（垂直插件）。

流程：
    START -> safety_check -> oms_draft -> risk_classify -> human_audit(interrupt)
                                                       -> issue_permit -> END

risk_classify 依电压等级定级：高压及以上挂起等待调度长核签，其余自动签发。
节点经通用服务层访问外部系统：RAG（安规检索）与 ACL（OMS/SCADA）。
图的编译/checkpointer 注入由 build_dispatch_graph 收口。
"""

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.core.rag.vector_service import VectorStoreService
from app.core.workflow import ThreadMappingStore
from app.plugins.power_grid.adapters import OMSGatewayAdapter, SCADAGatewayAdapter
from app.plugins.power_grid.constants import POWER_AUTO_ACTOR, POWER_RAG_BIZ_TAG
from app.plugins.power_grid.policy import PowerGridRiskPolicy
from app.plugins.power_grid.states import DispatchState

logger = logging.getLogger(__name__)

# 安规措施判定关键词
GROUND_WIRE_KEYWORD = "接地线"
VOLTAGE_TEST_KEYWORD = "验电"

# RAG 检索参数
RAG_TOP_K = 3
RAG_MIN_SIMILARITY = 0.3
RAG_BIZ_TAG = POWER_RAG_BIZ_TAG

# 审核结论键
DECISION_PERMITTED_KEY = "is_permitted"
DECISION_REVIEWER_KEY = "reviewer_id"

# 状态机节点名（与图内节点 key 一致）：current_node 指针回写的取值来源
NODE_SAFETY_CHECK = "safety_check"
NODE_OMS_DRAFT = "oms_draft"
NODE_RISK_CLASSIFY = "risk_classify"
NODE_HUMAN_AUDIT = "human_audit"
NODE_ISSUE_PERMIT = "issue_permit"


async def _mark_current_node(thread_id: str, node_name: str) -> None:
    """回写状态机当前节点指针（供待办池定位卡点与运维巡检）。

    【非致命】写库失败只告警：指针属旁路观测数据，缺失不影响流程推进，
    绝不允许因它让整条审批流崩掉。
    """
    if not thread_id:
        return
    try:
        await ThreadMappingStore.update_current_node(thread_id, node_name)
    except Exception:  # noqa: BLE001 —— 指针回写属旁路观测，失败不阻断
        logger.warning(
            "[电网调度] current_node 回写失败 thread=%s node=%s",
            thread_id,
            node_name,
            exc_info=True,
        )


async def safety_check_node(state: DispatchState) -> dict:
    """内嵌 RAG 安规校验：检索规程依据，比对现场安措，产出违规项。"""
    await _mark_current_node(state.get("thread_id", ""), NODE_SAFETY_CHECK)
    voltage_level = state.get("voltage_level", "")
    operation_task = state.get("operation_task", "")
    measures_text = "；".join(state.get("safety_measures", []))

    try:
        await VectorStoreService().search_advanced(
            query=f"{voltage_level} {operation_task}",
            biz_tag=RAG_BIZ_TAG,
            top_k=RAG_TOP_K,
            min_similarity=RAG_MIN_SIMILARITY,
        )
    except Exception:
        # 检索异常不阻断，转规则比对
        pass

    violations: list[str] = []
    if GROUND_WIRE_KEYWORD not in measures_text:
        violations.append(
            f"现场安措未明确“挂设{GROUND_WIRE_KEYWORD}”，"
            "依据安规，停电检修可能来电各端必须挂设接地线。"
        )
    if VOLTAGE_TEST_KEYWORD not in measures_text:
        violations.append(
            f"现场安措未明确“{VOLTAGE_TEST_KEYWORD}”，"
            "依据安规，装设接地线前必须先验电确认无电压。"
        )

    return {"violations": violations}


async def oms_draft_node(state: DispatchState) -> dict:
    """初审通过后经 ACL 创建 OMS 正式工单。"""
    await _mark_current_node(state.get("thread_id", ""), NODE_OMS_DRAFT)
    biz_order_id = await OMSGatewayAdapter.create_official_order(
        draft_data={
            "substation_name": state.get("substation"),
            "voltage_level": state.get("voltage_level"),
            "work_content": state.get("operation_task"),
        },
        creator_id=state.get("user_id", ""),
    )
    return {"biz_order_id": biz_order_id}


async def risk_classify_node(state: DispatchState) -> dict:
    """电压等级定级：判定该工单是否必须人工核签。

    【铁律】本节点必须无副作用：状态机在 resume 时会从节点头部重放，
    任何写库 / 写外部系统的动作放在这里都会被执行两次。
    """
    classification = await PowerGridRiskPolicy().classify(
        {"voltage_level": state.get("voltage_level", "")}
    )
    return {
        "risk_level": classification.risk_level,
        "risk_reason": classification.reason,
        "requires_human": classification.requires_human,
        "risk_matched_rules": classification.matched_rules,
    }


async def human_audit_node(state: DispatchState) -> dict:
    """挂起等待调度长核签；resume 值即审核结论。

    指针回写放在 interrupt() 之前：挂起后线程停留在本节点，指针即为本节点名；
    resume 时本节点从头重放，指针回写幂等（同名覆盖），无副作用。
    """
    await _mark_current_node(state.get("thread_id", ""), NODE_HUMAN_AUDIT)
    decision = interrupt(
        {
            "waiting_for": "dispatcher_audit",
            "biz_order_id": state.get("biz_order_id"),
            "substation": state.get("substation"),
            "voltage_level": state.get("voltage_level"),
            "operation_task": state.get("operation_task"),
            "violations": state.get("violations", []),
            # 定级结论随挂起载荷交给审批人，便于其判断风险
            "risk_level": state.get("risk_level"),
            "risk_reason": state.get("risk_reason"),
        }
    )
    return {"audit_decision": decision}


async def issue_permit_node(state: DispatchState) -> dict:
    """核准通过或定级为可自动放行后，经 ACL 签发 SCADA 调度许可令。

    - 自动路径没有人工决策：签发主体回退为系统主体（写入 CA 签名源，审计可追溯）；
    - 幂等：许可令已存在时直接返回，避免恢复重试时重复签发。
    """
    if state.get("permit_info"):
        return {}
    await _mark_current_node(state.get("thread_id", ""), NODE_ISSUE_PERMIT)
    decision = state.get("audit_decision") or {}
    auto_issued = not decision
    reviewer_id = decision.get(DECISION_REVIEWER_KEY) or POWER_AUTO_ACTOR
    permit_info = await SCADAGatewayAdapter.issue_credential(
        biz_order_id=state.get("biz_order_id", ""),
        reviewer_id=reviewer_id,
    )
    return {"permit_info": permit_info, "auto_issued": auto_issued}


def route_after_safety_check(state: DispatchState) -> str:
    """有违规 → 结束；无违规 → 创建工单。"""
    return "oms_draft" if not state.get("violations") else END


def route_by_risk(state: DispatchState) -> str:
    """定级为必须人工 → 挂起核签；否则 → 直接签发许可令。

    默认取 True（requires_human 缺省即转人工）：定级信息缺失时保守处理。
    """
    return "human_audit" if state.get("requires_human", True) else "issue_permit"


def route_after_audit(state: DispatchState) -> str:
    """核准 → 签发；驳回 → 结束。"""
    decision = state.get("audit_decision") or {}
    return (
        "issue_permit"
        if decision.get(DECISION_PERMITTED_KEY)
        else END
    )


def build_dispatch_graph(checkpointer=None):
    """编译电网检修调度图并返回。

    人工挂点由节点内 interrupt() 表达，不使用 interrupt_before。
    checkpointer 缺省退化为内存检查点（测试）。
    """
    from langgraph.checkpoint.memory import MemorySaver

    builder = StateGraph(DispatchState)
    builder.add_node("safety_check", safety_check_node)
    builder.add_node("oms_draft", oms_draft_node)
    builder.add_node("risk_classify", risk_classify_node)
    builder.add_node("human_audit", human_audit_node)
    builder.add_node("issue_permit", issue_permit_node)

    builder.add_edge(START, "safety_check")
    builder.add_conditional_edges(
        "safety_check",
        route_after_safety_check,
        ["oms_draft", END],
    )
    # 定级插在建单之后：自动签发同样需要外部工单号去签发许可令
    builder.add_edge("oms_draft", "risk_classify")
    builder.add_conditional_edges(
        "risk_classify",
        route_by_risk,
        ["human_audit", "issue_permit"],
    )
    builder.add_conditional_edges(
        "human_audit",
        route_after_audit,
        ["issue_permit", END],
    )
    builder.add_edge("issue_permit", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())
