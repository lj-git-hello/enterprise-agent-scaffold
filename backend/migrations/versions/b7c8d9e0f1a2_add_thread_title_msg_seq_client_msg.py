"""add_thread_title_and_message_seq

Revision ID: b7c8d9e0f1a2
Revises: 844a9489b970
Create Date: 2026-11-16 10:20:00.000000

新增三处权威存储能力（前端本地存储降级为离线兜底后，服务端须成为唯一权威源）：

1. ai_thread_mappings.title：线程展示标题。业务语义由插件注入，核心层只当
   不透明字符串存取；存量行为 NULL，列表查询回退到首条文本消息摘要。
2. ai_chat_messages.seq：全局单调序号。created_at 的 server_default 是 now()
   即 transaction timestamp，同一事务内多行同值，且 PG 不保证同值行的物理
   顺序 —— 跨线程（对话线程 + 工单线程）消息排序必须靠确定性序号。
3. ai_chat_messages.client_msg_id：客户端消息幂等键。配合部分唯一索引，
   SSE 断线重连整条 POST 重放时不会重复落库。

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = '844a9489b970'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 显式命名序列：ORM 的 server_default 与迁移必须引用同一名字
SEQ_NAME = 'ai_chat_messages_seq_seq'


def upgrade() -> None:
    """新增 title / seq / client_msg_id 三列及其索引。"""
    # ---- 1. 线程标题（可空：存量行回退展示 thread_id）----
    op.add_column(
        'ai_thread_mappings',
        sa.Column('title', sa.String(length=128), nullable=True),
    )

    # ---- 2. 消息全局单调序号 ----
    op.execute(
        f"CREATE SEQUENCE IF NOT EXISTS {SEQ_NAME} START WITH 1 INCREMENT BY 1;"
    )
    # 带默认值新增列：存量行由 PG 侧 nextval 直接填值，不留 NULL 窗口
    op.add_column(
        'ai_chat_messages',
        sa.Column(
            'seq',
            sa.BigInteger(),
            nullable=True,
            server_default=sa.text(f"nextval('{SEQ_NAME}'::regclass)"),
        ),
    )
    # 存量行的 nextval 分配顺序 = 物理顺序，与真实产生顺序未必一致；
    # 按 (created_at, id) 重排，使 seq 表达"消息产生顺序"
    op.execute(
        "WITH numbered AS ("
        "  SELECT id, row_number() OVER (ORDER BY created_at, id) AS n "
        "  FROM ai_chat_messages"
        ") "
        "UPDATE ai_chat_messages m SET seq = numbered.n "
        "FROM numbered WHERE m.id = numbered.id;"
    )
    op.execute(
        f"SELECT setval('{SEQ_NAME}', "
        "COALESCE((SELECT MAX(seq) FROM ai_chat_messages), 0) + 1, false);"
    )
    op.execute(
        "ALTER TABLE ai_chat_messages ALTER COLUMN seq SET NOT NULL;"
    )
    op.create_index('idx_chat_seq', 'ai_chat_messages', ['seq'], unique=False)

    # ---- 3. 客户端幂等键（存量行为 NULL，故用部分唯一索引）----
    op.add_column(
        'ai_chat_messages',
        sa.Column('client_msg_id', sa.String(length=64), nullable=True),
    )
    op.create_index(
        'uq_chat_thread_client_msg',
        'ai_chat_messages',
        ['thread_id', 'client_msg_id'],
        unique=True,
        postgresql_where=sa.text("client_msg_id IS NOT NULL"),
    )


def downgrade() -> None:
    """移除 title / seq / client_msg_id。"""
    op.drop_index('uq_chat_thread_client_msg', table_name='ai_chat_messages')
    op.drop_index('idx_chat_seq', table_name='ai_chat_messages')
    op.drop_column('ai_chat_messages', 'client_msg_id')
    op.drop_column('ai_chat_messages', 'seq')
    op.execute(f"DROP SEQUENCE IF EXISTS {SEQ_NAME};")
    op.drop_column('ai_thread_mappings', 'title')
