"""add_card_version_and_updated_at

Revision ID: c3d4e5f6a7b8
Revises: b7c8d9e0f1a2
Create Date: 2026-11-20 10:30:00.000000

卡片终态一致性的存储支撑：

1. ai_chat_messages.card_version：卡片视图投影版本，卡片被原地改写时递增，
   作为卡片级乐观锁依据（防止并发审批互相覆盖）。存量行统一为 1。
2. ai_chat_messages.updated_at：最近一次改写时间。纯文本消息与未改写卡片
   等于写入时间；存量行由 now() 回填。

说明：ai_chat_messages 的消息语义仍是 append-only（新增行），卡片载荷的
原地改写属于"视图投影"更新，改写前的旧载荷由调用方写入 ai_execution_traces
留痕，保证审计链路的 append-only 真相不被破坏。

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 card_version / updated_at 两列。"""
    # 存量行由 PG 侧默认值回填，不留 NULL 窗口
    op.add_column(
        'ai_chat_messages',
        sa.Column(
            'card_version',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('1'),
        ),
    )
    op.add_column(
        'ai_chat_messages',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
    )


def downgrade() -> None:
    """移除 card_version / updated_at。"""
    op.drop_column('ai_chat_messages', 'updated_at')
    op.drop_column('ai_chat_messages', 'card_version')