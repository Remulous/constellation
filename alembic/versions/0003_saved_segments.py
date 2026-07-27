"""Add reusable saved people segments."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    inspector = inspect(op.get_bind())
    if "saved_segments" in inspector.get_table_names():
        return
    op.create_table(
        "saved_segments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_saved_segments_name", "saved_segments", ["name"], unique=True)


def downgrade():
    inspector = inspect(op.get_bind())
    if "saved_segments" in inspector.get_table_names():
        op.drop_table("saved_segments")
