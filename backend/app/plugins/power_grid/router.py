"""电网检修调度业务路由（垂直插件）。

仅做 HTTP 编排：鉴权、参数校验、驱动图、调用通用底座。
本文件不含任何 SQL（建档 / CAS / 待办 / 消息持久化 / 终态回写
全部委托 core.workflow）。
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.security import get_current_user, require_roles
from app.core.workflow import (
    EVENT_STATUS_CHANGED,
    WORKFLOW_EVENT_BUS,
    GraphRegistry,
    HITLController,
    MessageRecorder,
    ThreadMappingStore,
    TraceRecorder,
    WorkflowEvent,
    assert_thread_visible,
)
from app.plugins.power_grid.adapters import OMSGatewayAdapter
from app.plugins.power_grid.constants import (
    POWER_AUTO_ACTOR,
    POWER_CARD_TYPE_DISPATCHER_AUDIT,
    POWER_CARD_TYPE_PERMIT_RESULT,
    POWER_GRAPH_NAME,
    POWER_ROLE_APPLICANT,
    POWER_ROLE_DISPATCHER,
    POWER_STATUS_FAILED,
    POWER_STATUS_IN_PROGRESS,
    POWER_STATUS_PERMITTED,
    POWER_STATUS_REJECTED,
    POWER_STATUS_SAFETY_REJECTED,
    POWER_STATUS_WAITING_HUMAN,
    POWER_TRACE_NODE_APPROVE,
    POWER_TRACE_NODE_RESUME_FALLBACK,
)

logger = logging.getLogger(__name__)

# 路由自身不带前缀：由通用层按 manifest.router_prefix 统一挂载
power_dispatch_router = APIRouter(tags=["Power-Dispatch"])


class ApplyRequest(BaseModel):
    """检修提报请求。"""

    session_id: str
    substation: str
    voltage_level: str
    operation_task: str
    safety_measures: list[str]


class AuditRequest(BaseModel):
    """调度长核签请求。

    字段名遵循通用底座的审批动作契约（approved / opinion），
    插件内部再映射到自身状态机的判定键。

    card_version 是审核卡的乐观锁期望版本：卡片被原地改写时版本递增，
    版本不符说明本单已被他人决策，改写将被拒绝（不覆盖他人结论）。
    """

    thread_id: str
    lock_version: int
    approved: bool
    opinion: str = ""
    card_version: int = 1


async def _thread_summary(thread_id: str) -> dict:
    """线程摘要（供前端左栏与服务端对齐，字段一律以服务端为准）。"""
    mapping = await ThreadMappingStore.get(thread_id)
    if mapping is None:
        return {}
    return {
        "thread_id": mapping["thread_id"],
        "session_id": mapping["session_id"],
        "title": mapping["title"],
        "status": mapping["status"],
        "lock_version": mapping["lock_version"],
        "current_node": mapping["current_node"],
    }


async def _publish_status_changed(
    thread_id: str,
    status: str,
    actor_id: str,
) -> None:
    """广播线程状态变更事件（跨端实时同步）。

    【时序铁律】本函数必须在数据库 commit() 之后调用：
    底层写操作（建档 / 改写卡片 / 落卡 / 终态回写）各自独立提交，
    只有在它们全部返回后再广播，订阅方才可能读到已落库的状态。

    受众由插件填充（提报人 + 受理角色 + 本次操作人）：
    core 底座只做字符串匹配，不理解这些标识的业务含义。
    """
    mapping = await ThreadMappingStore.get(thread_id)
    audience: set[str] = set()
    if mapping:
        for key in ("creator_id", "assigned_role", "operator_id"):
            value = mapping.get(key)
            if value:
                audience.add(str(value))
    audience.add(actor_id)

    WORKFLOW_EVENT_BUS.publish(
        WorkflowEvent(
            event=EVENT_STATUS_CHANGED,
            thread_id=thread_id,
            status=status,
            actor_id=actor_id,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            audience=tuple(sorted(audience)),
            # 会话归属：会话级列表据此定位到具体会话行
            session_id=str(mapping.get("session_id") or "") if mapping else "",
        )
    )


async def _sync_oms_status(
    biz_order_id: str | None,
    final_status: str,
    opinion: str | None = None,
) -> None:
    """把终态同步回外部工单系统（经防腐层调用）。

    【失败仅告警】外部工单系统不可用不得让本地流程卡在中间态：
    本地终态已落库，外部同步失败只记告警，由人工或对账任务补偿。
    """
    if not biz_order_id:
        return
    try:
        await OMSGatewayAdapter.update_order_status(
            biz_order_id, final_status, opinion
        )
    except Exception as exc:  # noqa: BLE001 —— 外部同步失败不阻断本地终态
        logger.warning(
            "[电网调度] OMS 工单状态同步失败 order=%s status=%s err=%s",
            biz_order_id,
            final_status,
            exc,
        )


async def _clear_current_node(thread_id: str) -> None:
    """清空状态机节点指针（流程已终结，不再停留在任何节点上）。"""
    try:
        await ThreadMappingStore.update_current_node(thread_id, None)
    except Exception as exc:  # noqa: BLE001 —— 指针属旁路观测，失败不阻断
        logger.warning(
            "[电网调度] current_node 清空失败 thread=%s err=%s", thread_id, exc
        )


async def _settle_auto_issued(
    thread_id: str,
    state_values: dict,
) -> dict:
    """落定"自动签发"终态：回写终态 + 落许可令卡，返回许可令卡片载荷。

    【落卡防重】请求重试或流程重放时，若该线程已是终态 PERMITTED 或已存在
    许可令卡，则跳过重复落卡 —— 同一张工单不得出现两张调度许可令。
    """
    permit_info = state_values.get("permit_info") or {}
    risk_reason = state_values.get("risk_reason") or ""

    mapping = await ThreadMappingStore.get(thread_id)
    already_final = (
        mapping is not None and mapping.get("status") == POWER_STATUS_PERMITTED
    )
    card_exists = await MessageRecorder.exists_card(
        thread_id, POWER_CARD_TYPE_PERMIT_RESULT
    )

    if not already_final:
        await HITLController.finalize_status(
            thread_id=thread_id,
            final_status=POWER_STATUS_PERMITTED,
            signature_hash=permit_info.get("ca_signature_hash"),
            operator_id=POWER_AUTO_ACTOR,
            audit_opinion=risk_reason or None,
        )

    # 流程已终结：清空节点指针，并把终态同步回外部工单系统
    await _clear_current_node(thread_id)
    await _sync_oms_status(
        state_values.get("biz_order_id"),
        POWER_STATUS_PERMITTED,
        risk_reason or None,
    )

    permit_card_payload = {
        "card_type": POWER_CARD_TYPE_PERMIT_RESULT,
        "thread_id": thread_id,
        "status": POWER_STATUS_PERMITTED,
        "biz_order_id": state_values.get("biz_order_id"),
        "permit_info": permit_info,
        "opinion": risk_reason,
        "auto_issued": True,
    }
    if not card_exists:
        await MessageRecorder.record_card(
            thread_id=thread_id,
            role="assistant",
            card_type=POWER_CARD_TYPE_PERMIT_RESULT,
            card_payload=permit_card_payload,
            content="检修申请已按规则自动签发调度许可令，无需人工核签。",
        )
    return permit_card_payload


# ---------------------------------------------------------------------------
# POST /apply
# ---------------------------------------------------------------------------
@power_dispatch_router.post("/apply")
async def apply_dispatch(
    req: ApplyRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """派生 thread → 建初始映射 → 驱动图 → 按落点落待审卡 / 终态凭证卡。"""
    user_id = current_user["sub"]
    thread_id = f"{req.session_id}_{uuid.uuid4().hex[:12]}"

    # 业务标题（标题语义属插件私有，核心层只当不透明字符串存取）
    title = f"{req.substation} {req.voltage_level} {req.operation_task}".strip()[:128]

    # 建档（通用层封装持久化，Router 不直接写 ORM）
    # 建档态用"进行中"：图执行期间（含 RAG 检索，约 1~2 秒）该单不应以
    # "等待人工"身份出现在审批人待办池里，落点确定后再写实际状态。
    await ThreadMappingStore.create_mapping(
        thread_id=thread_id,
        session_id=req.session_id,
        creator_id=user_id,
        creator_role=POWER_ROLE_APPLICANT,
        assigned_role=POWER_ROLE_DISPATCHER,
        creator_dept=current_user.get("dept"),
        status=POWER_STATUS_IN_PROGRESS,
        title=title,
    )

    # 取已注册的电网图（lifespan / 测试 bootstrap 时按插件名注册）
    graph = GraphRegistry.get_graph(POWER_GRAPH_NAME)

    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(
        {
            "thread_id": thread_id,
            "session_id": req.session_id,
            "user_id": user_id,
            "status": POWER_STATUS_IN_PROGRESS,
            "error": None,
            "substation": req.substation,
            "voltage_level": req.voltage_level,
            "operation_task": req.operation_task,
            "safety_measures": req.safety_measures,
            "violations": [],
            "biz_order_id": None,
            "audit_decision": None,
            "permit_info": None,
            "risk_level": "",
            "risk_reason": "",
            "requires_human": True,
            "risk_matched_rules": [],
            "auto_issued": False,
        },
        config=config,
    )

    snapshot = await graph.aget_state(config)
    state_values = dict(snapshot.values)

    # 回写外部工单号（弱引用）：建单成功后立即落库，
    # 使线程与外部工单从此可双向追溯
    biz_order_id = state_values.get("biz_order_id")
    if biz_order_id:
        await ThreadMappingStore.update_biz_order_id(thread_id, biz_order_id)

    # 图是否真的挂起在人工审计节点上：
    # - 挂起（interrupts 非空）→ 本单确实在等人核签，留在待办池；
    # - 未挂起 → 流程已在图内终止，成因有二且必须区分：安规校验未通过（否决），
    #   或定级为可自动放行（已签发许可令）。二者若混为一谈，
    #   自动签发单会被误判成废单而归档。
    interrupted = bool(snapshot.interrupts)
    violations = state_values.get("violations", []) or []
    permit_info = state_values.get("permit_info")

    if not interrupted:
        # ---- 成因 ①：安规校验未通过（与电压等级无关，优先判定）----
        if violations:
            await HITLController.finalize_status(
                thread_id=thread_id,
                final_status=POWER_STATUS_SAFETY_REJECTED,
            )
            await _clear_current_node(thread_id)
            await _sync_oms_status(
                biz_order_id, POWER_STATUS_SAFETY_REJECTED, "；".join(violations)
            )
            await ThreadMappingStore.archive(thread_id)
            detail = "；".join(violations) if violations else "未登记具体违规项"
            content = (
                f"安规校验未通过，检修申请已自动否决，不进入调度长审批池。{detail}"
            )
            await MessageRecorder.record_text(
                thread_id=thread_id,
                role="assistant",
                content=content,
            )
            # 广播状态变更（终态已提交，订阅方才读得到）
            await _publish_status_changed(
                thread_id, POWER_STATUS_SAFETY_REJECTED, user_id
            )
            return {
                "thread_id": thread_id,
                "status": POWER_STATUS_SAFETY_REJECTED,
                "interrupted": False,
                "message": content,
                "card": {},
                "session_id": req.session_id,
                "card_type": None,
                "thread": await _thread_summary(thread_id),
            }

        # ---- 成因 ②：定级为可自动放行，许可令已签发 ----
        if permit_info:
            permit_card_payload = await _settle_auto_issued(
                thread_id, state_values
            )
            # 广播状态变更（终态与许可令卡均已提交）
            await _publish_status_changed(
                thread_id, POWER_STATUS_PERMITTED, POWER_AUTO_ACTOR
            )
            # 响应只带卡片、不带 message：前端按 message 优先渲染纯文本，
            # 带上 message 会让许可凭证卡（含现场核验二维码）不渲染。
            return {
                "thread_id": thread_id,
                "status": POWER_STATUS_PERMITTED,
                "interrupted": False,
                "card": permit_card_payload,
                "session_id": req.session_id,
                "card_type": POWER_CARD_TYPE_PERMIT_RESULT,
                "thread": await _thread_summary(thread_id),
            }

        # ---- 兜底：图已终止但既无违规项也无许可令（异常路径）----
        # 不得停留在"等待人工"：否则该单会以待办身份永久滞留审批池。
        await HITLController.finalize_status(
            thread_id=thread_id,
            final_status=POWER_STATUS_REJECTED,
            audit_opinion="流程异常终止：未产生许可令",
        )
        await _clear_current_node(thread_id)
        await _sync_oms_status(
            biz_order_id, POWER_STATUS_REJECTED, "流程异常终止：未产生许可令"
        )
        content = "检修申请流程异常终止（未产生许可令），请修改后重新提报。"
        await MessageRecorder.record_text(
            thread_id=thread_id,
            role="assistant",
            content=content,
        )
        # 广播状态变更（终态已提交）
        await _publish_status_changed(thread_id, POWER_STATUS_REJECTED, user_id)
        return {
            "thread_id": thread_id,
            "status": POWER_STATUS_REJECTED,
            "interrupted": False,
            "message": content,
            "card": {},
            "session_id": req.session_id,
            "card_type": None,
            "thread": await _thread_summary(thread_id),
        }

    # ---- 挂起等待调度长核签：落"等待人工"状态，进入待办池 ----
    await ThreadMappingStore.update_status(thread_id, POWER_STATUS_WAITING_HUMAN)
    card_payload = {
        "card_type": POWER_CARD_TYPE_DISPATCHER_AUDIT,
        "thread_id": thread_id,
        "biz_order_id": state_values.get("biz_order_id"),
        "substation": req.substation,
        "voltage_level": req.voltage_level,
        "operation_task": req.operation_task,
        "violations": state_values.get("violations", []),
        # 卡片状态与线程状态同源：审批后由 /audit 原地改写为终态
        "status": POWER_STATUS_WAITING_HUMAN,
        "lock_version": 1,
    }
    await MessageRecorder.record_card(
        thread_id=thread_id,
        role="assistant",
        card_type=POWER_CARD_TYPE_DISPATCHER_AUDIT,
        card_payload=card_payload,
        retry_payload={"thread_id": thread_id, "lock_version": 1},
        content="检修申请已提交，等待调度长核签。",
    )

    # 广播状态变更：调度长端的待办池据此实时出现本单（终态已落库）
    await _publish_status_changed(thread_id, POWER_STATUS_WAITING_HUMAN, user_id)

    return {
        "thread_id": thread_id,
        "status": POWER_STATUS_WAITING_HUMAN,
        "interrupted": True,
        "card": card_payload,
        "session_id": req.session_id,
        "card_type": POWER_CARD_TYPE_DISPATCHER_AUDIT,
        "thread": await _thread_summary(thread_id),
    }


# ---------------------------------------------------------------------------
# GET /tasks
# ---------------------------------------------------------------------------
@power_dispatch_router.get("/tasks")
async def list_tasks(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """待办任务审批池（Pending Task Pool）。

    角色由后端依据 JWT 判定，忽略任何前端传入的角色参数：
    - DISPATCHER：返回其名下 WAITING_HUMAN 待审工单；
    - APPLICANT（及其它非审批角色）：允许访问，安全返回空列表，不抛 403。
    """
    if current_user.get("role") == POWER_ROLE_DISPATCHER:
        items = await HITLController.get_pending_tasks(POWER_ROLE_DISPATCHER)
    else:
        items = []
    return {
        "total": len(items),
        "reviewer_id": current_user["sub"],
        "items": items,
    }


# ---------------------------------------------------------------------------
# GET /messages
# ---------------------------------------------------------------------------
@power_dispatch_router.get("/messages")
async def list_thread_messages(
    thread_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """按 thread_id 读取消息 / 卡片快照（只读）；无记录返回空列表。

    可见性由通用底座统一判定：不可见与不存在统一 404，
    不泄露"存在但无权"这一事实。
    """
    await assert_thread_visible(
        thread_id,
        current_user["sub"],
        current_user.get("role") or "",
    )
    items = await MessageRecorder.list_by_thread(thread_id)
    return {
        "thread_id": thread_id,
        "requester_id": current_user["sub"],
        "items": items,
    }


# ---------------------------------------------------------------------------
# POST /audit
# ---------------------------------------------------------------------------
async def _settle_audit_result(
    thread_id: str,
    reviewer_id: str,
    opinion: str,
    approved: bool,
    state_values: dict,
    expected_card_version: int | None = None,
) -> dict:
    """落定核签结论：原地改写审核卡 → 落终态凭证卡 → 回写终态 → 广播。

    审核卡是**可变视图投影**：审批结论直接改写原卡（status / 核签人 / 时间 /
    意见），而不是再插一张新卡 —— 否则提报员侧永远只看到"等待核签中"的旧卡。
    改写前的旧载荷由 TraceRecorder 写入 ai_execution_traces（append-only 真相）。

    正常路径与"图恢复失败但已产出终态产物"的兜底路径共用此段落定逻辑，
    保证两条路径产出的卡片内容、线程终态与外部工单状态完全一致。
    """
    permit_info = state_values.get("permit_info")
    final_status = POWER_STATUS_PERMITTED if approved else POWER_STATUS_REJECTED
    signature_hash = (
        permit_info.get("ca_signature_hash") if permit_info else None
    )
    decided_at = datetime.now(timezone.utc).isoformat()

    # CAS 已把线程锁版本自增：回写卡片时以库内最新锁版本为准
    mapping = await ThreadMappingStore.get(thread_id)
    current_lock_version = int(mapping.get("lock_version") or 0) if mapping else 1

    # ---- 原地改写审核卡：先留痕（改写后旧载荷即消失）----
    previous_card = await MessageRecorder.get_latest_card(
        thread_id, POWER_CARD_TYPE_DISPATCHER_AUDIT
    )
    await TraceRecorder.record(
        thread_id,
        POWER_TRACE_NODE_APPROVE,
        tool_calls={
            "approved": approved,
            "reviewer_id": reviewer_id,
            "opinion": opinion,
            "final_status": final_status,
            "previous_card_payload": (previous_card or {}).get("card_payload"),
            "previous_card_version": (previous_card or {}).get("card_version"),
        },
    )

    card_updated = await MessageRecorder.update_card_payload(
        thread_id=thread_id,
        card_type=POWER_CARD_TYPE_DISPATCHER_AUDIT,
        patch={
            "status": final_status,
            "decided_by": reviewer_id,
            "decided_at": decided_at,
            "opinion": opinion,
            "lock_version": current_lock_version,
        },
        expected_card_version=expected_card_version,
    )
    if not card_updated:
        # 卡片版本与期望不符（他端已改写）：不覆盖他人结论，
        # 但不阻断审批 —— 终态仍以线程状态与凭证卡为准，卡片终态由回读兜底
        logger.warning(
            "[电网调度] 审核卡原地改写未命中 thread=%s expected_card_version=%s",
            thread_id,
            expected_card_version,
        )

    # 终态凭证卡片（幂等：已存在则不重复落卡）
    if not await MessageRecorder.exists_card(
        thread_id, POWER_CARD_TYPE_PERMIT_RESULT
    ):
        await MessageRecorder.record_card(
            thread_id=thread_id,
            role="assistant",
            card_type=POWER_CARD_TYPE_PERMIT_RESULT,
            card_payload={
                "thread_id": thread_id,
                "status": final_status,
                "biz_order_id": state_values.get("biz_order_id"),
                "permit_info": permit_info,
                "opinion": opinion,
            },
            content=(
                "调度许可令已签发。"
                if approved
                else "调度长已驳回本次检修申请。"
            ),
        )

    # 终态回写（委托 HITLController，Router 不写 UPDATE SQL）
    await HITLController.finalize_status(
        thread_id=thread_id,
        final_status=final_status,
        signature_hash=signature_hash,
    )
    # 流程已终结：清空节点指针，并把终态同步回外部工单系统
    await _clear_current_node(thread_id)
    await _sync_oms_status(
        state_values.get("biz_order_id"), final_status, opinion or None
    )

    # 广播状态变更：提报端无需刷新即可看到终态卡片
    # （卡片改写 / 凭证卡落库 / 终态回写均已提交，此处严格在 commit 之后）
    await _publish_status_changed(thread_id, final_status, reviewer_id)

    # 回读改写后的审核卡（含最新 card_version），供前端当场更新
    latest_card = await MessageRecorder.get_latest_card(
        thread_id, POWER_CARD_TYPE_DISPATCHER_AUDIT
    )

    return {
        "thread_id": thread_id,
        "status": final_status,
        "permit_info": permit_info,
        "card": (latest_card or {}).get("card_payload") or {},
        "card_type": POWER_CARD_TYPE_DISPATCHER_AUDIT,
        "card_version": (latest_card or {}).get("card_version"),
        "decided_by": reviewer_id,
        "decided_at": decided_at,
        "thread": await _thread_summary(thread_id),
    }


async def _settle_resume_failure(
    req: AuditRequest,
    reviewer_id: str,
    error: Exception,
) -> dict:
    """图恢复失败兜底落定：终态不回退，也绝不把线程留在运行中中间态。

    CAS 抢占与图恢复分属两个事务：图恢复抛错时线程已被置为运行中，
    若就此返回将永久滞留。兜底依据图内**是否已产出终态产物**判定：

    - 已产出（如许可令已签发）：终态保持不变，**不置失败**，仅补记错误留痕
      —— 不可逆的许可既已生效，回退只会造成"许可已发但单据被驳"的矛盾态；
    - 未产出：置失败终态并留痕，线程不得继续停留在运行中。

    未产出终态产物时向调用方抛 500，如实告知失败；已产出时按成功返回，
    使前端拿到与库内一致的终态卡片。
    """
    thread_id = req.thread_id
    graph = GraphRegistry.get_graph(POWER_GRAPH_NAME)
    config = {"configurable": {"thread_id": thread_id}}
    state_values: dict = {}
    try:
        snapshot = await graph.aget_state(config)
        state_values = dict(snapshot.values or {})
    except Exception:  # noqa: BLE001 —— 读不到快照即按"未产出产物"处理
        logger.warning(
            "[电网调度] 兜底读取图状态失败 thread=%s", thread_id, exc_info=True
        )

    artifact_produced = bool(state_values.get("permit_info"))
    final_status = (
        POWER_STATUS_PERMITTED if artifact_produced else POWER_STATUS_FAILED
    )

    await TraceRecorder.record(
        thread_id,
        POWER_TRACE_NODE_RESUME_FALLBACK,
        error_log=f"{type(error).__name__}: {error}",
        tool_calls={
            "artifact_produced": artifact_produced,
            "final_status": final_status,
            "approved": req.approved,
            "reviewer_id": reviewer_id,
        },
    )

    if not artifact_produced:
        await HITLController.finalize_status(
            thread_id=thread_id,
            final_status=POWER_STATUS_FAILED,
            operator_id=reviewer_id,
            audit_opinion=f"流程执行失败：{type(error).__name__}",
        )
        await _clear_current_node(thread_id)
        await _sync_oms_status(
            state_values.get("biz_order_id"), POWER_STATUS_FAILED, "流程执行失败"
        )
        await _publish_status_changed(thread_id, POWER_STATUS_FAILED, reviewer_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="流程执行失败，该单已置为失败终态，请重新提报。",
        ) from error

    # 已产出终态产物：终态不回退、不置失败，按正常路径落定
    logger.warning(
        "[电网调度] 图恢复失败但已产出许可令，保持终态不回退 thread=%s", thread_id
    )
    return await _settle_audit_result(
        thread_id,
        reviewer_id,
        req.opinion,
        req.approved,
        state_values,
        expected_card_version=req.card_version or None,
    )


@power_dispatch_router.post("/audit")
async def audit_dispatch(
    req: AuditRequest,
    current_user: dict = Depends(require_roles([POWER_ROLE_DISPATCHER])),
) -> dict:
    """CAS 抢占 + Command 唤醒 → 原地改写审核卡 → 落终态凭证卡 → 回写终态。

    审核卡是**可变视图投影**：审批结论直接改写原卡（status / 核签人 / 时间 /
    意见），而不是再插一张新卡 —— 否则提报员侧永远只看到"等待核签中"的旧卡。
    改写前的旧载荷由 TraceRecorder 写入 ai_execution_traces（append-only 真相）。
    """
    reviewer_id = current_user["sub"]

    # 通用底座：CAS + 唤醒（409 = 已被他人处理，原样上抛）
    try:
        await HITLController.resume_with_cas(
            thread_id=req.thread_id,
            lock_version=req.lock_version,
            resume_payload={
                "is_permitted": req.approved,
                "opinion": req.opinion,
                "reviewer_id": reviewer_id,
            },
            operator_id=reviewer_id,
            audit_opinion=req.opinion or None,
            graph_name=POWER_GRAPH_NAME,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            raise
        logger.exception("[电网调度] 图恢复失败 thread=%s", req.thread_id)
        return await _settle_resume_failure(req, reviewer_id, exc)
    except Exception as exc:  # noqa: BLE001 —— 失败必须落终态，不得滞留中间态
        logger.exception("[电网调度] 图恢复失败 thread=%s", req.thread_id)
        return await _settle_resume_failure(req, reviewer_id, exc)

    # 读终态快照（图已完成）
    graph = GraphRegistry.get_graph(POWER_GRAPH_NAME)
    config = {"configurable": {"thread_id": req.thread_id}}
    final_snapshot = await graph.aget_state(config)

    return await _settle_audit_result(
        req.thread_id,
        reviewer_id,
        req.opinion,
        req.approved,
        dict(final_snapshot.values),
        expected_card_version=req.card_version or None,
    )
