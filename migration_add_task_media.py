"""add task_media and task_media_cache

Revision ID: a1b2c3d4e5f6
Revises: 
Create Date: 2025-05-24

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '0b58c37dcdfd'  # замените на последний revision вашего проекта
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── task_media ────────────────────────────────────────────────────────────
    existing_tables = inspector.get_table_names()

    if 'task_media' not in existing_tables:
        op.create_table(
            'task_media',
            sa.Column('id',         sa.Integer(),     primary_key=True, autoincrement=True),
            sa.Column('task_id',    sa.Integer(),     sa.ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False),
            sa.Column('index',      sa.Integer(),     nullable=False, server_default='0'),
            sa.Column('data',       sa.LargeBinary(), nullable=False),
            sa.Column('mime',       sa.String(32),    nullable=False, server_default='image/jpeg'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index('ix_task_media_task_id', 'task_media', ['task_id'])

    if 'task_media_cache' not in existing_tables:
        op.create_table(
            'task_media_cache',
            sa.Column('id',         sa.Integer(),    primary_key=True, autoincrement=True),
            sa.Column('task_id',    sa.Integer(),    sa.ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False),
            sa.Column('account_id', sa.Integer(),    sa.ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False),
            sa.Column('index',      sa.Integer(),    nullable=False, server_default='0'),
            sa.Column('file_id',    sa.String(512),  nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index('ix_task_media_cache_task_account', 'task_media_cache', ['task_id', 'account_id'])

    # ── has_media в tasks ─────────────────────────────────────────────────────
    existing_columns = [c['name'] for c in inspector.get_columns('tasks')]
    if 'has_media' not in existing_columns:
        op.add_column('tasks', sa.Column('has_media', sa.Boolean(), nullable=False, server_default='false'))

def downgrade() -> None:
    op.drop_index('ix_task_media_cache_task_account', table_name='task_media_cache')
    op.drop_table('task_media_cache')

    op.drop_index('ix_task_media_task_id', table_name='task_media')
    op.drop_table('task_media')

    op.drop_column('tasks', 'has_media')
    # op.add_column('tasks', sa.Column('photo_file_ids', sa.Text(), server_default='[]'))
