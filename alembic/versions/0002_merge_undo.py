"""Add merge history metadata required for one-click undo."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("merge_history")}
    indexes = {index["name"] for index in inspector.get_indexes("merge_history")}
    if "candidate_id" not in columns:
        op.add_column("merge_history", sa.Column("candidate_id", sa.Integer(), nullable=True))
    if "undone_at" not in columns:
        op.add_column("merge_history", sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True))
    if "ix_merge_history_candidate_id" not in indexes:
        op.create_index("ix_merge_history_candidate_id", "merge_history", ["candidate_id"])


def downgrade():
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("merge_history")}
    indexes = {index["name"] for index in inspector.get_indexes("merge_history")}
    if "ix_merge_history_candidate_id" in indexes:
        op.drop_index("ix_merge_history_candidate_id", table_name="merge_history")
    if "undone_at" in columns:
        op.drop_column("merge_history", "undone_at")
    if "candidate_id" in columns:
        op.drop_column("merge_history", "candidate_id")
