"""Add reviewed VetBiz minutes imports and sourced relationship intelligence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "vetbiz_import_records" not in tables:
        op.create_table(
            "vetbiz_import_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("meeting_title", sa.String(length=300), nullable=False),
            sa.Column("meeting_date", sa.Date(), nullable=False),
            sa.Column("source_type", sa.String(length=50), nullable=False),
            sa.Column("review_confirmed", sa.Boolean(), nullable=False),
            sa.Column("review_notes", sa.Text(), nullable=True),
            sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("import_status", sa.String(length=30), nullable=False),
            sa.Column("raw_text", sa.Text(), nullable=False),
            sa.Column("checksum", sa.String(length=64), nullable=False),
            sa.Column(
                "revision_of_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_records.id"),
                nullable=True,
            ),
            sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_vetbiz_import_records_checksum",
            "vetbiz_import_records",
            ["checksum"],
            unique=True,
        )
        op.create_index(
            "ix_vetbiz_import_records_import_status",
            "vetbiz_import_records",
            ["import_status"],
        )
        op.create_index(
            "ix_vetbiz_import_records_meeting_date",
            "vetbiz_import_records",
            ["meeting_date"],
        )
        op.create_index(
            "ix_vetbiz_import_records_revision_of_id",
            "vetbiz_import_records",
            ["revision_of_id"],
        )
        op.create_index(
            "ix_vetbiz_import_records_source_type",
            "vetbiz_import_records",
            ["source_type"],
        )

    inspector = inspect(bind)
    if "vetbiz_import_candidates" not in inspector.get_table_names():
        op.create_table(
            "vetbiz_import_candidates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "import_record_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_records.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("candidate_type", sa.String(length=40), nullable=False),
            sa.Column("extracted_data", sa.JSON(), nullable=False),
            sa.Column("source_excerpt", sa.Text(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("matched_entity_id", sa.String(length=64), nullable=True),
            sa.Column("match_reason", sa.Text(), nullable=True),
            sa.Column("resolution_notes", sa.Text(), nullable=True),
            sa.Column("committed_entity_type", sa.String(length=40), nullable=True),
            sa.Column("committed_entity_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
        for column in ("candidate_type", "import_record_id", "matched_entity_id", "status"):
            op.create_index(
                f"ix_vetbiz_import_candidates_{column}",
                "vetbiz_import_candidates",
                [column],
            )

    inspector = inspect(bind)
    if "organizations" not in inspector.get_table_names():
        op.create_table(
            "organizations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=250), nullable=False),
            sa.Column("normalized_name", sa.String(length=250), nullable=False),
            sa.Column("website", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "source_import_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_records.id"),
                nullable=True,
            ),
            sa.Column(
                "source_candidate_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_candidates.id"),
                nullable=True,
                unique=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_organizations_name", "organizations", ["name"])
        op.create_index(
            "ix_organizations_normalized_name",
            "organizations",
            ["normalized_name"],
            unique=True,
        )
        op.create_index(
            "ix_organizations_source_import_id", "organizations", ["source_import_id"]
        )

    inspector = inspect(bind)
    if "relationship_signals" not in inspector.get_table_names():
        op.create_table(
            "relationship_signals",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "person_id",
                sa.String(length=36),
                sa.ForeignKey("people.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id"),
                nullable=True,
            ),
            sa.Column("signal_type", sa.String(length=50), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("meeting_date", sa.Date(), nullable=False),
            sa.Column(
                "source_import_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_records.id"),
                nullable=False,
            ),
            sa.Column(
                "source_candidate_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_candidates.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("source_excerpt", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in (
            "meeting_date",
            "organization_id",
            "person_id",
            "signal_type",
            "source_import_id",
        ):
            op.create_index(
                f"ix_relationship_signals_{column}", "relationship_signals", [column]
            )

    inspector = inspect(bind)
    if "opportunities" not in inspector.get_table_names():
        op.create_table(
            "opportunities",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column(
                "person_id",
                sa.String(length=36),
                sa.ForeignKey("people.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id"),
                nullable=True,
            ),
            sa.Column("product", sa.String(length=80), nullable=True),
            sa.Column("stage", sa.String(length=50), nullable=False),
            sa.Column("next_action", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "source_signal_id",
                sa.Integer(),
                sa.ForeignKey("relationship_signals.id"),
                nullable=True,
            ),
            sa.Column(
                "source_import_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_records.id"),
                nullable=False,
            ),
            sa.Column(
                "source_candidate_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_candidates.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("source_excerpt", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in (
            "organization_id",
            "person_id",
            "product",
            "source_import_id",
            "source_signal_id",
            "stage",
        ):
            op.create_index(f"ix_opportunities_{column}", "opportunities", [column])

    inspector = inspect(bind)
    if "connection_suggestions" not in inspector.get_table_names():
        op.create_table(
            "connection_suggestions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "source_person_id",
                sa.String(length=36),
                sa.ForeignKey("people.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "target_person_id",
                sa.String(length=36),
                sa.ForeignKey("people.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("supporting_signal_ids", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column(
                "source_import_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_records.id"),
                nullable=False,
            ),
            sa.Column(
                "source_candidate_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_candidates.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("source_excerpt", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in (
            "source_import_id",
            "source_person_id",
            "status",
            "target_person_id",
        ):
            op.create_index(
                f"ix_connection_suggestions_{column}",
                "connection_suggestions",
                [column],
            )

    inspector = inspect(bind)
    if "follow_up_suggestions" not in inspector.get_table_names():
        op.create_table(
            "follow_up_suggestions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "person_id",
                sa.String(length=36),
                sa.ForeignKey("people.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("due_date", sa.Date(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column(
                "source_import_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_records.id"),
                nullable=False,
            ),
            sa.Column(
                "source_candidate_id",
                sa.Integer(),
                sa.ForeignKey("vetbiz_import_candidates.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("source_excerpt", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in ("due_date", "person_id", "source_import_id", "status"):
            op.create_index(
                f"ix_follow_up_suggestions_{column}",
                "follow_up_suggestions",
                [column],
            )

    interaction_columns = {
        column["name"] for column in inspect(bind).get_columns("interactions")
    }
    missing_interaction_columns = {
        "source_import_id",
        "source_candidate_id",
        "source_excerpt",
    } - interaction_columns
    if missing_interaction_columns:
        # Batch mode is required for adding foreign-key columns to an existing
        # SQLite table and also keeps upgrades repeatable after a downgrade.
        with op.batch_alter_table("interactions", recreate="always") as batch_op:
            if "source_import_id" in missing_interaction_columns:
                batch_op.add_column(
                    sa.Column(
                        "source_import_id",
                        sa.Integer(),
                        sa.ForeignKey(
                            "vetbiz_import_records.id",
                            name="fk_interactions_source_import_id",
                        ),
                        nullable=True,
                    )
                )
                batch_op.create_index(
                    "ix_interactions_source_import_id", ["source_import_id"]
                )
            if "source_candidate_id" in missing_interaction_columns:
                batch_op.add_column(
                    sa.Column(
                        "source_candidate_id",
                        sa.Integer(),
                        sa.ForeignKey(
                            "vetbiz_import_candidates.id",
                            name="fk_interactions_source_candidate_id",
                        ),
                        nullable=True,
                    )
                )
                batch_op.create_index(
                    "ix_interactions_source_candidate_id",
                    ["source_candidate_id"],
                    unique=True,
                )
            if "source_excerpt" in missing_interaction_columns:
                batch_op.add_column(
                    sa.Column("source_excerpt", sa.Text(), nullable=True)
                )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    interaction_columns = {
        column["name"] for column in inspector.get_columns("interactions")
    }
    interaction_indexes = {
        index["name"] for index in inspector.get_indexes("interactions")
    }
    # SQLite implements DROP COLUMN by rebuilding the table. Rebuild it once so
    # the provenance columns and their indexes disappear together without
    # leaving an intermediate schema that references a removed column.
    with op.batch_alter_table("interactions", recreate="always") as batch_op:
        if "ix_interactions_source_candidate_id" in interaction_indexes:
            batch_op.drop_index("ix_interactions_source_candidate_id")
        if "ix_interactions_source_import_id" in interaction_indexes:
            batch_op.drop_index("ix_interactions_source_import_id")
        if "source_excerpt" in interaction_columns:
            batch_op.drop_column("source_excerpt")
        if "source_candidate_id" in interaction_columns:
            batch_op.drop_column("source_candidate_id")
        if "source_import_id" in interaction_columns:
            batch_op.drop_column("source_import_id")
    for table in (
        "follow_up_suggestions",
        "connection_suggestions",
        "opportunities",
        "relationship_signals",
        "organizations",
        "vetbiz_import_candidates",
        "vetbiz_import_records",
    ):
        if table in inspect(bind).get_table_names():
            op.drop_table(table)
