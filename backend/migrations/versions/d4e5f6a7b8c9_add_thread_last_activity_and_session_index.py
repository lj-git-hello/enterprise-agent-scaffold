"""add_thread_last_activity_and_session_index

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-20 18:20:00.000000

会话级聚合排序的索引支撑：

1. ai_thread_mappings.last_activity_at —— 线程最近活跃时间（物化列）。
   此前该值只在列表查询期由 LEFT JOIN LATERAL 派生
   （max(ai_chat_messages.created_at)），派生表达式无法参与复合索引，
   索引要求必须先把该列物化。
   消息写入入口唯一（core 层 MessageRecorder），故由核心层在写消息的同一
   事务内维护本列；工作流状态推进（CAS 抢占 / 终态回写 / 状态更新）
   同样属于实质活动，一并提升。读侧改用 COALESCE(last_activity_at, created_at)。
   存量行由 max(ai_chat_messages.created_at) 回填，无消息线程回填 created_at。

2. 复合索引 (creator_id, session_id, last_activity_at DESC)：
   支撑"可见性前缀 → 会话聚合 → 最近活跃排序"的查询形态。
   另建 (assigned_role, session_id, last_activity_at DESC)
   覆盖"角色可见"分支的同形查询。

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """物化 last_activity_at 并建立会话聚合复合索引。"""
    # 存量行先由 PG 侧默认值兜底，再按真实消息时间回填
    op.add_column(
        'ai_thread_mappings',
        sa.Column(
            'last_activity_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
    )
    op.execute(
        "UPDATE ai_thread_mappings t "
        "SET last_activity_at = COALESCE("
        "  (SELECT max(m.created_at) FROM ai_chat_messages m "
        "   WHERE m.thread_id = t.thread_id), "
        "  t.created_at"
        ")"
    )
    op.create_index(
        'ix_ai_thread_mappings_creator_session_activity',
        'ai_thread_mappings',
        ['creator_id', 'session_id', sa.text('last_activity_at DESC')],
    )
    op.create_index(
        'ix_ai_thread_mappings_role_session_activity',
        'ai_thread_mappings',
        ['assigned_role', 'session_id', sa.text('last_activity_at DESC')],
    )


def downgrade() -> None:
    """移除复合索引与 last_activity_at 列。"""
    op.drop_index(
        'ix_ai_thread_mappings_role_session_activity',
        table_name='ai_thread_mappings',
    )
    op.drop_index(
        'ix_ai_thread_mappings_creator_session_activity',
        table_name='ai_thread_mappings',
    )
    op.drop_column('ai_thread_mappings', 'last_activity_at')