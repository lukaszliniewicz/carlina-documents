import uuid
from typing import Any, cast

from sqlalchemy import DDL, event
from sqlalchemy import inspect as sqlalchemy_inspect

from carlina.core.utc import utc_now_db
from iarf_assistant.database.types import ContentType, EncryptedType
from iarf_assistant.extensions import db

from .block_contract import BlockContractError
from .block_notes import render_note_body


class TranslationProject(db.Model):
    __tablename__ = "translation_project"
    __table_args__ = (
        db.Index(
            "ix_translation_project_user_updated",
            "user_id",
            "updated_at",
        ),
        # NULL keys retain the historical non-idempotent create behavior;
        # SQLite permits multiple NULLs in this unique index.
        db.Index(
            "uq_translation_project_user_creation_key",
            "user_id",
            "creation_idempotency_key",
            unique=True,
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    name = db.Column(db.String(255), nullable=False)

    source_lang = db.Column(db.String(50), nullable=False)
    target_lang = db.Column(db.String(50), nullable=False)

    document_id = db.Column(db.String(36), nullable=True)
    creation_idempotency_key = db.Column(db.String(128), nullable=True)
    creation_request_fingerprint = db.Column(db.String(64), nullable=True)

    status = db.Column(db.String(50), default="created", nullable=False)
    # ``NULL`` preserves the historical segment engine.  The block engine is
    # selected explicitly by a later workflow contract; row shape must never
    # be used to infer it.
    engine_contract = db.Column(db.String(64), nullable=True)

    skeleton_html = db.Column(ContentType(db.Text, strategy="system"), nullable=True)

    settings = db.Column(db.JSON, nullable=True)
    working_memory = db.Column(ContentType(db.Text, strategy="system"), nullable=True)
    # A document provider is allowed to change its current output after a
    # project is created.  Translation workers therefore operate on this
    # policy-encoded, content-addressed snapshot instead of re-reading mutable
    # provider state from a queue payload.
    source_snapshot = db.Column(
        ContentType(db.Text, strategy="system"), nullable=True
    )
    source_content_hash = db.Column(db.String(64), nullable=True, index=True)
    source_captured_at = db.Column(db.DateTime, nullable=True)
    share_token = db.Column(db.String(64), unique=True, nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=utc_now_db)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db)

    user = db.relationship(
        "User",
        backref=db.backref(
            "translation_projects", lazy=True, cascade="all, delete-orphan"
        ),
    )
    segments = db.relationship(
        "TranslationSegment",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="TranslationSegment.order_index",
    )
    glossary_terms = db.relationship(
        "GlossaryTerm", back_populates="project", cascade="all, delete-orphan"
    )
    operations = db.relationship(
        "TranslationOperation",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="desc(TranslationOperation.created_at)",
    )
    source_plans = db.relationship(
        "TranslationSourcePlan",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="TranslationSourcePlan.created_at",
    )
    epub_edition = db.relationship(
        "TranslationEpubEdition",
        back_populates="project",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "document_id": self.document_id,
            "status": self.status,
            "settings": self.settings,
            "share_token": self.share_token,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class TranslationRevision(db.Model):
    __tablename__ = "translation_revision"
    __table_args__ = (
        db.Index(
            "ix_translation_revision_segment_created",
            "segment_id",
            "created_at",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    segment_id = db.Column(
        db.String(36),
        db.ForeignKey("translation_segment.id", ondelete="CASCADE"),
        nullable=False,
    )

    translated_text = db.Column(
        ContentType(db.Text, strategy="system"), nullable=True
    )

    author_type = db.Column(db.String(20), nullable=False)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    created_at = db.Column(db.DateTime, default=utc_now_db)

    segment = db.relationship(
        "TranslationSegment",
        backref=db.backref(
            "revisions",
            lazy=True,
            cascade="all, delete-orphan",
            order_by="desc(TranslationRevision.created_at)",
        ),
    )


class TranslationSegment(db.Model):
    __tablename__ = "translation_segment"
    __table_args__ = (
        db.CheckConstraint(
            "version >= 1",
            name="ck_translation_segment_version",
        ),
        db.Index(
            "ix_translation_segment_project_order",
            "project_id",
            "order_index",
        ),
        db.Index(
            "ix_translation_segment_project_status_order",
            "project_id",
            "status",
            "order_index",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36),
        db.ForeignKey("translation_project.id", ondelete="CASCADE"),
        nullable=False,
    )

    order_index = db.Column(db.Integer, nullable=False)
    block_index = db.Column(db.Integer, nullable=False, default=0)
    merged_into_id = db.Column(
        db.String(36),
        db.ForeignKey("translation_segment.id", ondelete="SET NULL"),
        nullable=True,
    )

    html_tag = db.Column(db.String(50), nullable=False)
    outer_tag = db.Column(db.String(50), nullable=True)
    vec_id = db.Column(db.Integer, index=True, unique=True, nullable=True)

    original_text = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    translated_text = db.Column(
        ContentType(db.Text, strategy="system"), nullable=True
    )

    status = db.Column(db.String(20), default="pending", nullable=False)
    context_note = db.Column(db.Text, nullable=True)
    # Browser edits and worker results use this monotonic fence.  It is
    # intentionally separate from translation revisions: revisions preserve
    # history, while ``version`` protects the current row from stale writes.
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    project = db.relationship("TranslationProject", back_populates="segments")
    translator_notes = db.relationship(
        "TranslationNote",
        back_populates="segment",
        cascade="all, delete-orphan",
        order_by="TranslationNote.order_index",
    )

    def to_dict(self):
        current_notes = [
            note
            for note in self.translator_notes
            if note.state == "active" and note.segment_version == self.version
        ]
        return {
            "id": self.id,
            "order_index": self.order_index,
            "block_index": self.block_index,
            "merged_into_id": self.merged_into_id,
            "html_tag": self.html_tag,
            "outer_tag": self.outer_tag,
            "original_text": self.original_text,
            "translated_text": self.translated_text,
            "status": self.status,
            "context_note": self.context_note,
            "version": self.version,
            "translator_notes": [note.to_dict() for note in current_notes],
        }


class TranslationNote(db.Model):
    """A model-authored reader-facing note attached to one segment version."""

    __tablename__ = "translation_note"
    __table_args__ = (
        db.UniqueConstraint(
            "segment_id",
            "segment_version",
            "order_index",
            name="uq_translation_note_segment_version_order",
        ),
        db.CheckConstraint(
            "order_index >= 0",
            name="ck_translation_note_order_index",
        ),
        db.CheckConstraint(
            "segment_version >= 1",
            name="ck_translation_note_segment_version",
        ),
        db.CheckConstraint(
            "version >= 1",
            name="ck_translation_note_version",
        ),
        db.CheckConstraint(
            "author_type IN ('model', 'human')",
            name="ck_translation_note_author_type",
        ),
        db.CheckConstraint(
            "state IN ('active', 'superseded', 'suppressed')",
            name="ck_translation_note_state",
        ),
        db.Index(
            "ix_translation_note_segment_state_version_order",
            "segment_id",
            "state",
            "segment_version",
            "order_index",
        ),
        db.Index(
            "ix_translation_note_project_segment",
            "project_id",
            "segment_id",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36),
        db.ForeignKey("translation_project.id", ondelete="CASCADE"),
        nullable=False,
    )
    segment_id = db.Column(
        db.String(36),
        db.ForeignKey("translation_segment.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_index = db.Column(db.Integer, nullable=False)
    anchor_text = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    body = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    author_type = db.Column(db.String(20), nullable=False, default="model")
    state = db.Column(db.String(20), nullable=False, default="active")
    segment_version = db.Column(db.Integer, nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=utc_now_db,
        onupdate=utc_now_db,
        nullable=False,
    )

    segment = db.relationship("TranslationSegment", back_populates="translator_notes")
    project = db.relationship("TranslationProject")

    def to_dict(self):
        return {
            "id": self.id,
            "anchor_text": self.anchor_text,
            "body": self.body,
            "body_html": render_note_body(self.body),
            "author_type": self.author_type,
            "state": self.state,
            "segment_version": self.segment_version,
            "version": self.version,
        }


class GlossaryTerm(db.Model):
    __tablename__ = "translation_glossary"
    __table_args__ = (
        db.Index(
            "ix_translation_glossary_project_source",
            "project_id",
            "source_term",
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.String(36),
        db.ForeignKey("translation_project.id", ondelete="CASCADE"),
        nullable=False,
    )

    source_term = db.Column(db.String(255), nullable=False)
    target_term = db.Column(db.String(255), nullable=False)
    context_notes = db.Column(db.Text, nullable=True)

    is_user_defined = db.Column(db.Boolean, default=True, nullable=False)
    needs_review = db.Column(db.Boolean, default=False, nullable=False)

    domain = db.Column(db.String(100), nullable=True)
    part_of_speech = db.Column(db.String(50), nullable=True)
    status = db.Column(db.String(20), default="approved")

    created_at = db.Column(db.DateTime, default=utc_now_db)

    project = db.relationship("TranslationProject", back_populates="glossary_terms")

    def to_dict(self):
        return {
            "id": self.id,
            "source_term": self.source_term,
            "target_term": self.target_term,
            "context_notes": self.context_notes,
            "is_user_defined": self.is_user_defined,
            "needs_review": self.needs_review,
        }


class TranslationOperation(db.Model):
    """Durable state for one translator workflow execution.

    TaskQueue records delivery and retry mechanics only.  This row is the
    translator's owner-scoped source of truth for progress, cancellation,
    terminal outcome, and idempotency. Source-derived checkpoints and model
    failures follow the root's ordinary-content policy.
    """

    __tablename__ = "translation_operation"
    __table_args__ = (
        db.UniqueConstraint(
            "id", "project_id", name="uq_translation_operation_id_project"
        ),
        db.UniqueConstraint(
            "owner_user_id",
            "project_id",
            "kind",
            "idempotency_key",
            name="uq_translation_operation_owner_project_kind_key",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_operation_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_operation_owner",
        ),
        db.CheckConstraint(
            "state IN ('queued', 'running', 'cancel_requested', 'completed', "
            "'partial', 'failed', 'cancelled')",
            name="ck_translation_operation_state",
        ),
        db.CheckConstraint(
            "version >= 1",
            name="ck_translation_operation_version",
        ),
        db.CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 100)",
            name="ck_translation_operation_progress",
        ),
        db.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_translation_operation_request_fingerprint",
        ),
        db.CheckConstraint(
            "source_content_hash IS NULL OR length(source_content_hash) = 64",
            name="ck_translation_operation_source_hash",
        ),
        db.CheckConstraint(
            "(source_plan_id IS NULL AND source_plan_hash IS NULL) OR "
            "(source_content_hash IS NOT NULL AND source_plan_id IS NOT NULL AND "
            "source_plan_hash IS NOT NULL AND length(source_content_hash) = 64 AND "
            "length(source_plan_hash) = 64)",
            name="ck_translation_operation_source_plan_pin",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_operation_source_plan_project",
        ),
        db.CheckConstraint(
            "error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 AND "
            "substr(error_code, 1, 1) GLOB '[a-z]' AND "
            "error_code NOT GLOB '*[^a-z0-9_]*')",
            name="ck_translation_operation_error_code",
        ),
        db.CheckConstraint(
            "state NOT IN ('cancel_requested', 'cancelled') "
            "OR cancel_requested_at IS NOT NULL",
            name="ck_translation_operation_cancellation_timestamp",
        ),
        db.Index(
            "ix_translation_operation_project_state_updated",
            "project_id",
            "state",
            "updated_at",
        ),
        db.Index(
            "ix_translation_operation_owner_kind_created",
            "owner_user_id",
            "kind",
            "created_at",
        ),
        db.Index(
            "ix_translation_operation_queue_job",
            "queue_job_id",
        ),
        db.Index(
            "ix_translation_operation_owner_state",
            "owner_user_id",
            "state",
        ),
        db.Index("ix_translation_operation_source_plan", "source_plan_id"),
        db.Index(
            "uq_translation_operation_active_project",
            "project_id",
            unique=True,
            sqlite_where=db.text("state IN ('queued', 'running', 'cancel_requested')"),
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(64), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    queue_job_id = db.Column(db.String(128), nullable=True)

    state = db.Column(
        db.String(32), nullable=False, default="queued", server_default="queued"
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    progress = db.Column(db.Float, nullable=True)
    message = db.Column(db.String(255), nullable=True)
    error_code = db.Column(db.String(64), nullable=True)
    error_detail = db.Column(ContentType(db.Text, strategy="system"), nullable=True)
    checkpoint_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=True
    )
    result_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=True)
    source_content_hash = db.Column(db.String(64), nullable=True)
    # Phase-5 evidence never infers its plan from identical source bytes.  Old
    # segment-engine rows predate this contract and may legitimately be NULL.
    source_plan_id = db.Column(db.String(36), nullable=True)
    source_plan_hash = db.Column(db.String(64), nullable=True)
    cancel_requested_at = db.Column(db.DateTime, nullable=True)
    # A worker owns this lease while running.  It is deliberately nullable so
    # queued and terminal operations do not look active to recovery code.
    lease_expires_at = db.Column(db.DateTime, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )

    project = db.relationship("TranslationProject", back_populates="operations")
    owner = db.relationship("User", foreign_keys=[owner_user_id])

    def to_dict(self):
        # Keep this import local so models remain importable before the service
        # module has loaded its transition constants.
        from .operations import serialize_operation

        return serialize_operation(self)


_OPERATION_PIN_COLUMNS = (
    "source_content_hash",
    "source_plan_id",
    "source_plan_hash",
)
_OPERATION_PIN_TRIGGER = "trg_translation_operation_source_pins_immutable"


@event.listens_for(TranslationOperation, "before_update")
def _reject_translation_operation_pin_update(_mapper, _connection, target):
    """Keep an established source identity immutable through the ORM."""

    state = sqlalchemy_inspect(target)
    if any(state.attrs[name].history.has_changes() for name in _OPERATION_PIN_COLUMNS):
        raise BlockContractError("operation-pin-immutable")


event.listen(
    cast(Any, TranslationOperation).__table__,
    "after_create",
    DDL(
        f"""
        CREATE TRIGGER {_OPERATION_PIN_TRIGGER}
        BEFORE UPDATE OF source_content_hash, source_plan_id, source_plan_hash
        ON translation_operation
        FOR EACH ROW
        WHEN NEW.source_content_hash IS NOT OLD.source_content_hash
          OR NEW.source_plan_id IS NOT OLD.source_plan_id
          OR NEW.source_plan_hash IS NOT OLD.source_plan_hash
        BEGIN
            SELECT RAISE(ABORT, 'translation_operation source pins are immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    cast(Any, TranslationOperation).__table__,
    "after_drop",
    DDL(f"DROP TRIGGER IF EXISTS {_OPERATION_PIN_TRIGGER}").execute_if(
        dialect="sqlite"
    ),
)


class TranslationSourcePlan(db.Model):
    """Immutable, deterministic plan derived from one source snapshot."""

    __tablename__ = "translation_source_plan"
    __table_args__ = (
        db.UniqueConstraint(
            "project_id",
            "plan_hash",
            name="uq_translation_source_plan_project_plan_hash",
        ),
        db.Index(
            "uq_translation_source_plan_id_project",
            "id",
            "project_id",
            unique=True,
        ),
        db.CheckConstraint(
            "length(semantic_content_hash) = 64",
            name="ck_translation_source_plan_semantic_content_hash",
        ),
        db.CheckConstraint(
            "length(source_content_hash) = 64",
            name="ck_translation_source_plan_source_content_hash",
        ),
        db.CheckConstraint(
            "length(plan_hash) = 64",
            name="ck_translation_source_plan_plan_hash",
        ),
        db.CheckConstraint(
            "length(planner_settings_hash) = 64",
            name="ck_translation_source_plan_planner_settings_hash",
        ),
        db.CheckConstraint(
            "block_count >= 0",
            name="ck_translation_source_plan_block_count",
        ),
        db.CheckConstraint(
            "anchor_count >= 0",
            name="ck_translation_source_plan_anchor_count",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_source_plan_project",
        ),
        db.Index(
            "ix_translation_source_plan_project_created",
            "project_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    contract_version = db.Column(db.String(64), nullable=False)
    source_document_id = db.Column(db.String(256), nullable=False)
    source_filename = db.Column(
        ContentType(db.Text, strategy="system"), nullable=True
    )
    source_language = db.Column(db.String(64), nullable=False)
    language_profile = db.Column(db.String(128), nullable=False)
    namespace = db.Column(db.String(36), nullable=False)
    snapshot_contract_version = db.Column(db.String(64), nullable=False)
    semantic_contract_version = db.Column(db.String(64), nullable=False)
    semantic_contract_mode = db.Column(db.String(32), nullable=False)
    semantic_content_hash = db.Column(db.String(64), nullable=False)
    source_content_hash = db.Column(db.String(64), nullable=False)
    snapshot_captured_at = db.Column(db.DateTime, nullable=False)
    source_updated_at = db.Column(db.DateTime, nullable=True)
    planner_version = db.Column(db.String(64), nullable=False)
    planner_settings_hash = db.Column(db.String(64), nullable=False)
    plan_hash = db.Column(db.String(64), nullable=False)

    source_metadata = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=True
    )
    source_attestation = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    planner_settings = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    source_provenance = db.Column(db.JSON, nullable=True)
    coverage_witness = db.Column(db.JSON, nullable=False)
    block_count = db.Column(db.Integer, nullable=False)
    anchor_count = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    project = db.relationship("TranslationProject", back_populates="source_plans")
    blocks = db.relationship(
        "TranslationBlock",
        back_populates="source_plan",
        cascade="all, delete-orphan",
        order_by="TranslationBlock.order_index",
    )
    anchors = db.relationship(
        "TranslationSourceAnchor",
        back_populates="source_plan",
        cascade="all, delete-orphan",
        order_by="TranslationSourceAnchor.order_index",
    )
    target_revisions = db.relationship(
        "TranslationTargetBlockRevision",
        back_populates="source_plan",
        cascade="all, delete-orphan",
        order_by="TranslationTargetBlockRevision.created_at",
    )
    active_targets = db.relationship(
        "TranslationTargetBlockActive",
        back_populates="source_plan",
        cascade="all, delete-orphan",
        order_by="TranslationTargetBlockActive.block_id",
    )


class TranslationSourcePlanActive(db.Model):
    """CAS pointer to the one source plan currently editable by a project."""

    __tablename__ = "translation_source_plan_active"
    __table_args__ = (
        db.PrimaryKeyConstraint("project_id", name="pk_translation_source_plan_active"),
        db.UniqueConstraint(
            "source_plan_id", name="uq_translation_source_plan_active_plan"
        ),
        db.CheckConstraint(
            "version >= 1", name="ck_translation_source_plan_active_version"
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_source_plan_active_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_source_plan_active_plan",
        ),
    )

    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )


class TranslationNumberingOverlay(db.Model):
    """Owner-scoped numbering projection for one pinned source plan."""

    __tablename__ = "translation_numbering_overlay"
    __table_args__ = (
        db.CheckConstraint(
            "revision >= 1", name="ck_translation_numbering_overlay_revision"
        ),
        db.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_numbering_overlay_plan_hash",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_numbering_overlay_plan_project",
        ),
    )

    project_id = db.Column(
        db.String(36),
        db.ForeignKey("translation_project.id", ondelete="CASCADE"),
        primary_key=True,
    )
    owner_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    revision = db.Column(db.Integer, nullable=False)
    payload = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )


class TranslationSourcePlanCorrection(db.Model):
    """Immutable owner-authored receipt for one active-plan transition."""

    __tablename__ = "translation_source_plan_correction"
    __table_args__ = (
        db.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_translation_source_plan_correction_idempotency",
        ),
        db.CheckConstraint(
            "contract_version = 'translation-source-plan-correction-v1'",
            name="ck_translation_source_plan_correction_contract",
        ),
        db.CheckConstraint(
            "action IN ('merge_previous', 'merge_next', 'exclude')",
            name="ck_translation_source_plan_correction_action",
        ),
        db.CheckConstraint(
            "active_version >= 2",
            name="ck_translation_source_plan_correction_version",
        ),
        db.CheckConstraint(
            "predecessor_source_plan_id != successor_source_plan_id",
            name="ck_translation_source_plan_correction_distinct",
        ),
        db.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_translation_source_plan_correction_request_hash",
        ),
        db.CheckConstraint(
            "length(record_receipt) = 64",
            name="ck_translation_source_plan_correction_receipt",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_source_plan_correction_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_source_plan_correction_owner",
        ),
        db.ForeignKeyConstraint(
            ["predecessor_source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_source_plan_correction_predecessor",
        ),
        db.ForeignKeyConstraint(
            ["successor_source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_source_plan_correction_successor",
        ),
        db.Index(
            "ix_translation_source_plan_correction_project_created",
            "project_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    contract_version = db.Column(
        db.String(64),
        nullable=False,
        default="translation-source-plan-correction-v1",
        server_default="translation-source-plan-correction-v1",
    )
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    predecessor_source_plan_id = db.Column(db.String(36), nullable=False)
    successor_source_plan_id = db.Column(db.String(36), nullable=False)
    active_version = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(32), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    change_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    target_reuse_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    record_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationBlock(db.Model):
    """Immutable semantic block owned by a source plan."""

    __tablename__ = "translation_block"
    __table_args__ = (
        db.UniqueConstraint(
            "source_plan_id",
            "order_index",
            name="uq_translation_block_plan_order",
        ),
        # The composite key is the parent key used by SourceAnchor.  Keeping
        # the plan in the key prevents an anchor from crossing plan versions.
        db.UniqueConstraint(
            "id",
            "source_plan_id",
            name="uq_translation_block_id_source_plan",
        ),
        db.CheckConstraint(
            "order_index >= 1",
            name="ck_translation_block_order_index",
        ),
        db.CheckConstraint(
            "length(fragment_hash) = 64",
            name="ck_translation_block_fragment_hash",
        ),
        db.CheckConstraint(
            "length(projection_hash) = 64",
            name="ck_translation_block_projection_hash",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_block_source_plan",
        ),
        db.Index(
            "ix_translation_block_plan_order",
            "source_plan_id",
            "order_index",
        ),
    )

    id = db.Column(db.String(128), primary_key=True)
    source_plan_id = db.Column(db.String(36), nullable=False)
    order_index = db.Column(db.Integer, nullable=False)
    role = db.Column(db.String(64), nullable=False)
    locator = db.Column(db.JSON, nullable=False)
    classification_summary = db.Column(db.JSON, nullable=False)
    source_fragment = db.Column(
        ContentType(db.Text, strategy="system"), nullable=False
    )
    source_projection = db.Column(
        ContentType(db.Text, strategy="system"), nullable=False
    )
    source_slots = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    protected_inline = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    fragment_hash = db.Column(db.String(64), nullable=False)
    projection_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    source_plan = db.relationship("TranslationSourcePlan", back_populates="blocks")
    anchors = db.relationship(
        "TranslationSourceAnchor",
        back_populates="block",
        cascade="all, delete-orphan",
        order_by="TranslationSourceAnchor.block_order_index",
        overlaps="anchors,source_plan",
    )


class TranslationSourceAnchor(db.Model):
    """Immutable sentence-like source span within one semantic block."""

    __tablename__ = "translation_source_anchor"
    __table_args__ = (
        db.UniqueConstraint(
            "source_plan_id",
            "order_index",
            name="uq_translation_source_anchor_plan_order",
        ),
        db.UniqueConstraint(
            "block_id",
            "block_order_index",
            name="uq_translation_source_anchor_block_order",
        ),
        # Migration 011 adds this identity as a named unique index because
        # SQLite cannot add a named unique constraint to the existing table
        # without rebuilding it.  Keep ORM and migration shapes identical.
        db.Index(
            "uq_translation_source_anchor_id_plan",
            "id",
            "source_plan_id",
            unique=True,
        ),
        db.CheckConstraint(
            "order_index >= 1",
            name="ck_translation_source_anchor_order_index",
        ),
        db.CheckConstraint(
            "block_order_index >= 1",
            name="ck_translation_source_anchor_block_order_index",
        ),
        db.CheckConstraint(
            "start_offset >= 0",
            name="ck_translation_source_anchor_start_offset",
        ),
        db.CheckConstraint(
            "end_offset > start_offset",
            name="ck_translation_source_anchor_end_offset",
        ),
        db.CheckConstraint(
            "length(source_hash) = 64",
            name="ck_translation_source_anchor_source_hash",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_source_anchor_source_plan",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_source_anchor_block_plan",
        ),
        db.Index(
            "ix_translation_source_anchor_plan_order",
            "source_plan_id",
            "order_index",
        ),
        db.Index(
            "ix_translation_source_anchor_block_order",
            "block_id",
            "block_order_index",
        ),
    )

    id = db.Column(db.String(128), primary_key=True)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    order_index = db.Column(db.Integer, nullable=False)
    block_order_index = db.Column(db.Integer, nullable=False)
    start_offset = db.Column(db.Integer, nullable=False)
    end_offset = db.Column(db.Integer, nullable=False)
    splitter_profile = db.Column(db.String(64), nullable=False)
    splitter_version = db.Column(db.String(64), nullable=False)
    source_text = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    source_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    source_plan = db.relationship(
        "TranslationSourcePlan",
        back_populates="anchors",
        overlaps="anchors,block",
    )
    block = db.relationship(
        "TranslationBlock",
        back_populates="anchors",
        overlaps="anchors,source_plan",
    )


class TranslationTargetBlockRevision(db.Model):
    """Immutable validated target material for exactly one source block."""

    __tablename__ = "translation_target_block_revision"
    __table_args__ = (
        db.UniqueConstraint(
            "id",
            "source_plan_id",
            "block_id",
            name="uq_translation_target_revision_id_plan_block",
        ),
        db.CheckConstraint(
            "validation_state IN ('valid', 'rejected')",
            name="ck_translation_target_revision_validation_state",
        ),
        db.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_target_revision_plan_hash",
        ),
        db.CheckConstraint(
            "length(structural_fingerprint) = 64",
            name="ck_translation_target_revision_structural_fingerprint",
        ),
        db.CheckConstraint(
            "length(candidate_hash) = 64",
            name="ck_translation_target_revision_candidate_hash",
        ),
        db.CheckConstraint(
            "length(normalized_hash) = 64",
            name="ck_translation_target_revision_normalized_hash",
        ),
        db.CheckConstraint(
            "length(fragment_hash) = 64",
            name="ck_translation_target_revision_fragment_hash",
        ),
        db.CheckConstraint(
            "length(evidence_hash) = 64",
            name="ck_translation_target_revision_evidence_hash",
        ),
        db.CheckConstraint(
            "length(revision_record_hash) = 64",
            name="ck_translation_target_revision_record_hash",
        ),
        db.CheckConstraint(
            "parent_revision_hash IS NULL OR length(parent_revision_hash) = 64",
            name="ck_translation_target_revision_parent_hash",
        ),
        db.CheckConstraint(
            "(parent_revision_id IS NULL AND parent_revision_hash IS NULL) OR "
            "(parent_revision_id IS NOT NULL AND parent_revision_hash IS NOT NULL)",
            name="ck_translation_target_revision_parent_witness",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_target_revision_source_plan",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_revision_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["translation_target_block_revision.id"],
            ondelete="RESTRICT",
            name="fk_translation_target_revision_parent",
        ),
        db.ForeignKeyConstraint(
            ["author_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_target_revision_author",
        ),
        db.Index(
            "ix_translation_target_revision_plan_block_created",
            "source_plan_id",
            "block_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    structural_fingerprint = db.Column(db.String(64), nullable=False)
    contract_version = db.Column(db.String(64), nullable=False)
    validator_version = db.Column(db.String(64), nullable=False)
    parent_revision_id = db.Column(db.String(36), nullable=True)
    parent_revision_hash = db.Column(db.String(64), nullable=True)
    author_type = db.Column(db.String(32), nullable=False)
    author_user_id = db.Column(db.Integer, nullable=True)
    validation_state = db.Column(db.String(16), nullable=False)
    target_language = db.Column(db.String(64), nullable=False)
    normalized_candidate = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    rendered_fragment = db.Column(
        ContentType(db.Text, strategy="system"), nullable=False
    )
    validation_evidence = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    source_derived_target_text = db.Column(
        ContentType(db.Text, strategy="system"), nullable=False
    )
    candidate_hash = db.Column(db.String(64), nullable=False)
    normalized_hash = db.Column(db.String(64), nullable=False)
    fragment_hash = db.Column(db.String(64), nullable=False)
    evidence_hash = db.Column(db.String(64), nullable=False)
    revision_record_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    source_plan = db.relationship(
        "TranslationSourcePlan", back_populates="target_revisions"
    )
    block = db.relationship("TranslationBlock", overlaps="source_plan,target_revisions")
    parent_revision = db.relationship(
        "TranslationTargetBlockRevision",
        remote_side=[id],
        foreign_keys=[parent_revision_id],
    )
    author = db.relationship("User", foreign_keys=[author_user_id])
    notes = db.relationship(
        "TranslationTargetBlockNote",
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="TranslationTargetBlockNote.order_index",
    )
    edit_idempotency_receipts = db.relationship(
        "TranslationTargetEditIdempotency",
        cascade="all, delete-orphan",
        foreign_keys="TranslationTargetEditIdempotency.target_revision_id",
    )


class TranslationTargetBlockNote(db.Model):
    """Immutable model note attached to one target-block revision."""

    __tablename__ = "translation_target_block_note"
    __table_args__ = (
        db.UniqueConstraint(
            "revision_id",
            "order_index",
            name="uq_translation_target_block_note_revision_order",
        ),
        db.CheckConstraint(
            "order_index >= 0",
            name="ck_translation_target_block_note_order",
        ),
        db.CheckConstraint(
            "author_type = 'model'",
            name="ck_translation_target_block_note_author_type",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_target_block_note_record_hash",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_target_block_note_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_target_block_note_source_plan_project",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_block_note_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_target_block_note_revision",
        ),
        db.Index(
            "ix_translation_target_block_note_revision_order",
            "source_plan_id",
            "block_id",
            "revision_id",
            "order_index",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    order_index = db.Column(db.Integer, nullable=False)
    anchor_text = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    body = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    author_type = db.Column(db.String(20), nullable=False, default="model")
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)

    project = db.relationship("TranslationProject", overlaps="source_plan")
    source_plan = db.relationship(
        "TranslationSourcePlan", overlaps="project,block,notes"
    )
    block = db.relationship(
        "TranslationBlock", overlaps="source_plan,revision,notes"
    )
    revision = db.relationship(
        "TranslationTargetBlockRevision",
        back_populates="notes",
        foreign_keys=[revision_id],
        overlaps="source_plan,block",
    )

    @property
    def integrity_receipt(self) -> str:
        """Compatibility name for callers describing receipts, not hashes."""

        return self.record_hash

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "anchor_text": self.anchor_text,
            "body": self.body,
            "author_type": self.author_type,
        }


class TranslationTargetBlockActive(db.Model):
    """Replaceable optimistic active-revision projection, never target truth."""

    __tablename__ = "translation_target_block_active"
    __table_args__ = (
        db.PrimaryKeyConstraint(
            "source_plan_id", "block_id", name="pk_translation_target_active"
        ),
        db.CheckConstraint("version >= 1", name="ck_translation_target_active_version"),
        db.CheckConstraint(
            "length(pair_receipt) = 64",
            name="ck_translation_target_active_pair_receipt",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_target_active_source_plan",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_active_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_target_active_revision",
        ),
    )

    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    pair_receipt = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )

    source_plan = db.relationship(
        "TranslationSourcePlan", back_populates="active_targets"
    )
    block = db.relationship("TranslationBlock", overlaps="source_plan,active_targets")
    revision = db.relationship(
        "TranslationTargetBlockRevision", foreign_keys=[revision_id]
    )


class TranslationTargetEditIdempotency(db.Model):
    """Durable receipt for one actor-scoped manual target edit."""

    __tablename__ = "translation_target_edit_idempotency"
    __table_args__ = (
        db.UniqueConstraint(
            "project_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_translation_target_edit_idempotency_actor_key",
        ),
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            "block_id",
            "target_revision_id",
            name="uq_translation_target_edit_idempotency_scope",
        ),
        db.CheckConstraint(
            "actor_type IN ('owner', 'reviewer')",
            name="ck_translation_target_edit_idempotency_actor_type",
        ),
        db.CheckConstraint(
            "length(actor_id) BETWEEN 1 AND 128",
            name="ck_translation_target_edit_idempotency_actor_id",
        ),
        db.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_translation_target_edit_idempotency_key",
        ),
        db.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_translation_target_edit_idempotency_fingerprint",
        ),
        db.CheckConstraint(
            "expected_active_version >= 0",
            name="ck_translation_target_edit_idempotency_expected_version",
        ),
        db.CheckConstraint(
            "active_version >= 1",
            name="ck_translation_target_edit_idempotency_active_version",
        ),
        db.CheckConstraint(
            "actor_type = 'owner' OR actor_id LIKE 'grant:%'",
            name="ck_translation_target_edit_idempotency_reviewer_actor",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_target_edit_idempotency_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_target_edit_idempotency_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_edit_idempotency_block_scope",
        ),
        db.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_target_edit_idempotency_target_revision",
        ),
        db.Index(
            "ix_translation_target_edit_idempotency_project_created",
            "project_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    target_revision_id = db.Column(db.String(36), nullable=False)
    actor_type = db.Column(db.String(16), nullable=False)
    actor_id = db.Column(db.String(128), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    expected_active_version = db.Column(db.Integer, nullable=False)
    active_version = db.Column(db.Integer, nullable=False)
    safe_result_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationTargetSpanSet(db.Model):
    """Immutable alignment projection for one target revision."""

    __tablename__ = "translation_target_span_set"
    __table_args__ = (
        db.UniqueConstraint(
            "id",
            "source_plan_id",
            "block_id",
            "revision_id",
            name="uq_translation_target_span_set_identity",
        ),
        db.UniqueConstraint(
            "source_plan_id",
            "block_id",
            "revision_id",
            "projection_scope",
            "profile_id",
            "profile_version",
            "projection_hash",
            name="uq_translation_target_span_set_material",
        ),
        db.CheckConstraint(
            "span_count >= 0", name="ck_translation_target_span_set_count"
        ),
        db.CheckConstraint(
            "length(target_revision_hash) = 64",
            name="ck_translation_target_span_set_revision_hash",
        ),
        db.CheckConstraint(
            "length(projection_hash) = 64",
            name="ck_translation_target_span_set_projection_hash",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_target_span_set_record_hash",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_target_span_set_plan",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_span_set_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_target_span_set_revision_plan_block",
        ),
        db.Index(
            "ix_translation_target_span_set_revision",
            "source_plan_id",
            "block_id",
            "revision_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    target_revision_hash = db.Column(db.String(64), nullable=False)
    projection_scope = db.Column(db.String(160), nullable=False)
    profile_id = db.Column(db.String(64), nullable=False)
    profile_version = db.Column(db.String(64), nullable=False)
    projection = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    projection_hash = db.Column(db.String(64), nullable=False)
    span_count = db.Column(db.Integer, nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    revision = db.relationship(
        "TranslationTargetBlockRevision", foreign_keys=[revision_id]
    )
    spans = db.relationship(
        "TranslationTargetSpan",
        back_populates="span_set",
        cascade="all, delete-orphan",
        order_by="TranslationTargetSpan.order_index",
    )


class TranslationTargetSpan(db.Model):
    """One exact offset range in a policy-encoded target span-set projection."""

    __tablename__ = "translation_target_span"
    __table_args__ = (
        db.UniqueConstraint(
            "span_set_id", "order_index", name="uq_translation_target_span_set_order"
        ),
        db.UniqueConstraint(
            "id",
            "span_set_id",
            "source_plan_id",
            "block_id",
            "revision_id",
            name="uq_translation_target_span_identity",
        ),
        db.CheckConstraint("order_index >= 1", name="ck_translation_target_span_order"),
        db.CheckConstraint(
            "start_offset >= 0", name="ck_translation_target_span_start"
        ),
        db.CheckConstraint(
            "end_offset > start_offset", name="ck_translation_target_span_end"
        ),
        db.CheckConstraint(
            "length(text_hash) = 64", name="ck_translation_target_span_text_hash"
        ),
        db.CheckConstraint(
            "length(span_hash) = 64", name="ck_translation_target_span_hash"
        ),
        db.ForeignKeyConstraint(
            ["span_set_id", "source_plan_id", "block_id", "revision_id"],
            [
                "translation_target_span_set.id",
                "translation_target_span_set.source_plan_id",
                "translation_target_span_set.block_id",
                "translation_target_span_set.revision_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_target_span_set_identity",
        ),
        db.Index("ix_translation_target_span_set", "span_set_id", "order_index"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    span_set_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    order_index = db.Column(db.Integer, nullable=False)
    start_offset = db.Column(db.Integer, nullable=False)
    end_offset = db.Column(db.Integer, nullable=False)
    text_hash = db.Column(db.String(64), nullable=False)
    span_hash = db.Column(db.String(64), nullable=False)

    span_set = db.relationship(
        "TranslationTargetSpanSet", back_populates="spans", foreign_keys=[span_set_id]
    )


class TranslationAlignmentRevision(db.Model):
    """Append-only source-anchor to target-span partition for one revision."""

    __tablename__ = "translation_alignment_revision"
    __table_args__ = (
        db.UniqueConstraint(
            "id",
            "source_plan_id",
            "block_id",
            "revision_id",
            name="uq_translation_alignment_revision_identity",
        ),
        db.UniqueConstraint(
            "id",
            "source_plan_id",
            "block_id",
            "revision_id",
            "span_set_id",
            name="uq_translation_alignment_revision_span_set_identity",
        ),
        db.CheckConstraint(
            "group_count >= 0", name="ck_translation_alignment_revision_group_count"
        ),
        db.CheckConstraint(
            "review_state IN ('approved', 'needs_review')",
            name="ck_translation_alignment_revision_review_state",
        ),
        db.CheckConstraint(
            "length(target_revision_hash) = 64",
            name="ck_translation_alignment_revision_target_hash",
        ),
        db.CheckConstraint(
            "length(span_set_hash) = 64",
            name="ck_translation_alignment_revision_span_set_hash",
        ),
        db.CheckConstraint(
            "length(groups_hash) = 64",
            name="ck_translation_alignment_revision_groups_hash",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_alignment_revision_record_hash",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_alignment_revision_plan",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_alignment_revision_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_alignment_revision_target_plan_block",
        ),
        db.ForeignKeyConstraint(
            ["span_set_id", "source_plan_id", "block_id", "revision_id"],
            [
                "translation_target_span_set.id",
                "translation_target_span_set.source_plan_id",
                "translation_target_span_set.block_id",
                "translation_target_span_set.revision_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_alignment_revision_span_set_identity",
        ),
        db.Index(
            "ix_translation_alignment_revision_target",
            "source_plan_id",
            "block_id",
            "revision_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    span_set_id = db.Column(db.String(36), nullable=False)
    target_revision_hash = db.Column(db.String(64), nullable=False)
    span_set_hash = db.Column(db.String(64), nullable=False)
    method = db.Column(db.String(64), nullable=False)
    confidence_profile = db.Column(db.String(64), nullable=False)
    review_state = db.Column(db.String(32), nullable=False)
    group_count = db.Column(db.Integer, nullable=False)
    payload = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    groups_hash = db.Column(db.String(64), nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    span_set = db.relationship("TranslationTargetSpanSet", foreign_keys=[span_set_id])
    groups = db.relationship(
        "TranslationAlignmentGroup",
        back_populates="alignment",
        cascade="all, delete-orphan",
        order_by="TranslationAlignmentGroup.order_index",
    )


class TranslationAlignmentGroup(db.Model):
    """Encrypted membership and evidence for one monotonic alignment group."""

    __tablename__ = "translation_alignment_group"
    __table_args__ = (
        db.UniqueConstraint(
            "alignment_revision_id",
            "order_index",
            name="uq_translation_alignment_group_order",
        ),
        db.CheckConstraint(
            "order_index >= 1", name="ck_translation_alignment_group_order"
        ),
        db.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_translation_alignment_group_confidence",
        ),
        db.CheckConstraint(
            "review_state IN ('approved', 'needs_review')",
            name="ck_translation_alignment_group_review_state",
        ),
        db.CheckConstraint(
            "length(source_witness_hash) = 64",
            name="ck_translation_alignment_group_source_witness",
        ),
        db.CheckConstraint(
            "length(target_witness_hash) = 64",
            name="ck_translation_alignment_group_target_witness",
        ),
        db.CheckConstraint(
            "length(evidence_hash) = 64",
            name="ck_translation_alignment_group_evidence_hash",
        ),
        db.CheckConstraint(
            "length(group_hash) = 64", name="ck_translation_alignment_group_hash"
        ),
        db.ForeignKeyConstraint(
            [
                "alignment_revision_id",
                "source_plan_id",
                "block_id",
                "revision_id",
                "span_set_id",
            ],
            [
                "translation_alignment_revision.id",
                "translation_alignment_revision.source_plan_id",
                "translation_alignment_revision.block_id",
                "translation_alignment_revision.revision_id",
                "translation_alignment_revision.span_set_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_alignment_group_revision_identity",
        ),
        db.Index(
            "ix_translation_alignment_group_revision",
            "alignment_revision_id",
            "order_index",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    alignment_revision_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    span_set_id = db.Column(db.String(36), nullable=False)
    order_index = db.Column(db.Integer, nullable=False)
    source_anchor_ids = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    target_span_ids = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    source_witness_hash = db.Column(db.String(64), nullable=False)
    target_witness_hash = db.Column(db.String(64), nullable=False)
    method = db.Column(db.String(64), nullable=False)
    confidence = db.Column(db.Float, nullable=False)
    review_state = db.Column(db.String(32), nullable=False)
    evidence = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    evidence_hash = db.Column(db.String(64), nullable=False)
    group_hash = db.Column(db.String(64), nullable=False)

    alignment = db.relationship(
        "TranslationAlignmentRevision",
        back_populates="groups",
        foreign_keys=[alignment_revision_id],
    )


class TranslationTargetAlignmentActive(db.Model):
    """CAS projection pairing every active target pointer with an alignment witness."""

    __tablename__ = "translation_target_alignment_active"
    __table_args__ = (
        db.PrimaryKeyConstraint(
            "source_plan_id", "block_id", name="pk_translation_target_alignment_active"
        ),
        db.CheckConstraint(
            "version >= 1", name="ck_translation_target_alignment_active_version"
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_target_alignment_active_plan",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_alignment_active_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_target_alignment_active_target",
        ),
        db.ForeignKeyConstraint(
            ["alignment_revision_id", "source_plan_id", "block_id", "revision_id"],
            [
                "translation_alignment_revision.id",
                "translation_alignment_revision.source_plan_id",
                "translation_alignment_revision.block_id",
                "translation_alignment_revision.revision_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_target_alignment_active_alignment",
        ),
    )

    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    alignment_revision_id = db.Column(db.String(36), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )


class TranslationWindowAttempt(db.Model):
    """Immutable, provider-ready evidence for one internal block window."""

    __tablename__ = "translation_window_attempt"
    __table_args__ = (
        db.UniqueConstraint(
            "id", "source_plan_id", name="uq_translation_window_attempt_id_plan"
        ),
        db.CheckConstraint(
            "window_order >= 1", name="ck_translation_window_attempt_order"
        ),
        db.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_window_attempt_plan_hash",
        ),
        db.CheckConstraint(
            "length(source_content_hash) = 64",
            name="ck_translation_window_attempt_source_hash",
        ),
        db.CheckConstraint(
            "operation_version >= 1",
            name="ck_translation_window_attempt_operation_version",
        ),
        db.CheckConstraint(
            "length(queue_job_id) BETWEEN 1 AND 128 AND queue_job_id = trim(queue_job_id)",
            name="ck_translation_window_attempt_queue_job",
        ),
        db.CheckConstraint(
            "length(lease_fence) = 64", name="ck_translation_window_attempt_lease_fence"
        ),
        db.CheckConstraint(
            "length(packet_hash) = 64", name="ck_translation_window_attempt_packet_hash"
        ),
        db.CheckConstraint(
            "length(prompt_hash) = 64", name="ck_translation_window_attempt_prompt_hash"
        ),
        db.CheckConstraint(
            "length(settings_hash) = 64",
            name="ck_translation_window_attempt_settings_hash",
        ),
        db.CheckConstraint(
            "length(context_hash) = 64",
            name="ck_translation_window_attempt_context_hash",
        ),
        db.CheckConstraint(
            "length(context_manifest_hash) = 64",
            name="ck_translation_window_attempt_context_manifest_hash",
        ),
        db.CheckConstraint(
            "length(model_profile_hash) = 64",
            name="ck_translation_window_attempt_model_hash",
        ),
        db.CheckConstraint(
            "length(predecessor_hash) = 64",
            name="ck_translation_window_attempt_predecessor_hash",
        ),
        db.CheckConstraint(
            "length(attempt_record_hash) = 64",
            name="ck_translation_window_attempt_record_hash",
        ),
        db.CheckConstraint(
            "length(state_head_receipt) = 64",
            name="ck_translation_window_attempt_state_head_receipt",
        ),
        db.CheckConstraint(
            "retry_of_attempt_hash IS NULL OR length(retry_of_attempt_hash) = 64",
            name="ck_translation_window_attempt_retry_hash",
        ),
        db.CheckConstraint(
            "rebase_of_attempt_hash IS NULL OR length(rebase_of_attempt_hash) = 64",
            name="ck_translation_window_attempt_rebase_hash",
        ),
        db.CheckConstraint(
            "(retry_of_attempt_id IS NULL AND retry_of_attempt_hash IS NULL) OR "
            "(retry_of_attempt_id IS NOT NULL AND retry_of_attempt_hash IS NOT NULL)",
            name="ck_translation_window_attempt_retry_witness",
        ),
        db.CheckConstraint(
            "(rebase_of_attempt_id IS NULL AND rebase_of_attempt_hash IS NULL) OR "
            "(rebase_of_attempt_id IS NOT NULL AND rebase_of_attempt_hash IS NOT NULL)",
            name="ck_translation_window_attempt_rebase_witness",
        ),
        db.CheckConstraint(
            "retry_of_attempt_id IS NULL OR rebase_of_attempt_id IS NULL",
            name="ck_translation_window_attempt_single_lineage",
        ),
        db.CheckConstraint(
            "dispatch_state IN ('prepared', 'dispatched', 'external_pending', "
            "'response_received', 'provider_failed', 'normalization_failed', 'fence_failed', "
            "'staging_failed', 'staged', 'provider_outcome_uncertain')",
            name="ck_translation_window_attempt_dispatch_state",
        ),
        db.CheckConstraint(
            "dispatch_version >= 1",
            name="ck_translation_window_attempt_dispatch_version",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_window_attempt_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_window_attempt_owner",
        ),
        db.ForeignKeyConstraint(
            ["operation_id"],
            ["translation_operation.id"],
            ondelete="CASCADE",
            name="fk_translation_window_attempt_operation",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_window_attempt_plan",
        ),
        db.ForeignKeyConstraint(
            ["retry_of_attempt_id"],
            ["translation_window_attempt.id"],
            ondelete="RESTRICT",
            name="fk_translation_window_attempt_retry",
        ),
        db.ForeignKeyConstraint(
            ["rebase_of_attempt_id"],
            ["translation_window_attempt.id"],
            ondelete="RESTRICT",
            name="fk_translation_window_attempt_rebase",
        ),
        db.Index(
            "ix_translation_window_attempt_operation_created",
            "operation_id",
            "created_at",
        ),
        db.Index(
            "ix_translation_window_attempt_plan_created", "source_plan_id", "created_at"
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    operation_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    # Exact operation fence captured when the provider request was prepared.
    # Recovery may reconcile an uncertain result only after this original
    # fence is demonstrably no longer live.
    source_content_hash = db.Column(db.String(64), nullable=False)
    operation_version = db.Column(db.Integer, nullable=False)
    queue_job_id = db.Column(db.String(128), nullable=False)
    operation_lease_expires_at = db.Column(db.DateTime, nullable=False)
    lease_fence = db.Column(db.String(64), nullable=False)
    window_order = db.Column(db.Integer, nullable=False)
    packet_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    settings_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    block_ids_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    block_fingerprints_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    predecessor_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    model_profile_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    admission_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    context_manifest_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    packet_hash = db.Column(db.String(64), nullable=False)
    prompt_hash = db.Column(db.String(64), nullable=False)
    settings_hash = db.Column(db.String(64), nullable=False)
    context_hash = db.Column(db.String(64), nullable=False)
    context_manifest_hash = db.Column(db.String(64), nullable=False)
    model_profile_hash = db.Column(db.String(64), nullable=False)
    predecessor_hash = db.Column(db.String(64), nullable=False)
    retry_of_attempt_id = db.Column(db.String(36), nullable=True)
    retry_of_attempt_hash = db.Column(db.String(64), nullable=True)
    rebase_of_attempt_id = db.Column(db.String(36), nullable=True)
    rebase_of_attempt_hash = db.Column(db.String(64), nullable=True)
    attempt_record_hash = db.Column(db.String(64), nullable=False)
    state_head_receipt = db.Column(db.String(64), nullable=False)
    dispatch_state = db.Column(
        db.String(32), nullable=False, default="prepared", server_default="prepared"
    )
    dispatch_version = db.Column(
        db.Integer, nullable=False, default=1, server_default="1"
    )
    dispatch_claimed_at = db.Column(db.DateTime, nullable=True)
    dispatch_finished_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    project = db.relationship("TranslationProject")
    operation = db.relationship("TranslationOperation")
    source_plan = db.relationship("TranslationSourcePlan")
    owner = db.relationship("User", foreign_keys=[owner_user_id])
    events = db.relationship(
        "TranslationWindowAttemptEvent",
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="TranslationWindowAttemptEvent.sequence",
    )
    revision_links = db.relationship(
        "TranslationWindowAttemptRevision",
        back_populates="attempt",
        cascade="all, delete-orphan",
    )


class TranslationWindowAttemptEvent(db.Model):
    """Encrypted append-only attempt transcript with an explicit hash chain."""

    __tablename__ = "translation_window_attempt_event"
    __table_args__ = (
        db.UniqueConstraint(
            "attempt_id",
            "sequence",
            name="uq_translation_window_attempt_event_sequence",
        ),
        db.CheckConstraint(
            "sequence >= 1", name="ck_translation_window_attempt_event_sequence"
        ),
        db.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_translation_window_attempt_event_payload_hash",
        ),
        db.CheckConstraint(
            "length(event_hash) = 64", name="ck_translation_window_attempt_event_hash"
        ),
        db.CheckConstraint(
            "predecessor_event_hash IS NULL OR length(predecessor_event_hash) = 64",
            name="ck_translation_window_attempt_event_predecessor_hash",
        ),
        db.ForeignKeyConstraint(
            ["attempt_id"],
            ["translation_window_attempt.id"],
            ondelete="CASCADE",
            name="fk_translation_window_attempt_event_attempt",
        ),
        db.Index(
            "ix_translation_window_attempt_event_attempt_created",
            "attempt_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    attempt_id = db.Column(db.String(36), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    event_type = db.Column(db.String(64), nullable=False)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    predecessor_event_hash = db.Column(db.String(64), nullable=True)
    event_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    attempt = db.relationship("TranslationWindowAttempt", back_populates="events")


class TranslationWindowAttemptRevision(db.Model):
    """Immutable audit association for model-produced inactive revisions."""

    __tablename__ = "translation_window_attempt_revision"
    __table_args__ = (
        db.UniqueConstraint(
            "attempt_id",
            "block_id",
            name="uq_translation_window_attempt_revision_block",
        ),
        db.UniqueConstraint(
            "revision_id", name="uq_translation_window_attempt_revision_revision"
        ),
        db.CheckConstraint(
            "length(candidate_hash) = 64",
            name="ck_translation_window_attempt_revision_candidate_hash",
        ),
        db.CheckConstraint(
            "length(revision_record_hash) = 64",
            name="ck_translation_window_attempt_revision_record_hash",
        ),
        db.ForeignKeyConstraint(
            ["attempt_id", "source_plan_id"],
            [
                "translation_window_attempt.id",
                "translation_window_attempt.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_window_attempt_revision_attempt_plan",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_window_attempt_revision_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_window_attempt_revision_revision_plan_block",
        ),
        db.Index(
            "ix_translation_window_attempt_revision_plan", "source_plan_id", "block_id"
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    attempt_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    candidate_hash = db.Column(db.String(64), nullable=False)
    revision_record_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    attempt = db.relationship(
        "TranslationWindowAttempt", back_populates="revision_links"
    )
    revision = db.relationship(
        "TranslationTargetBlockRevision", foreign_keys=[revision_id]
    )


# Phase 5A deliberately starts a separate, policy-encoded evidence namespace. The
# legacy glossary and working-memory fields remain readable compatibility data;
# they are never silently adopted by this block engine.
class TranslationSemanticCensus(db.Model):
    """Immutable, deterministic occurrence census for one rehydrated source plan."""

    __tablename__ = "translation_semantic_census"
    __table_args__ = (
        db.UniqueConstraint(
            "id", "source_plan_id", name="uq_translation_semantic_census_id_plan"
        ),
        db.UniqueConstraint(
            "source_plan_id",
            "profile_id",
            "profile_version",
            "census_hash",
            name="uq_translation_semantic_census_material",
        ),
        db.CheckConstraint(
            "occurrence_count >= 0", name="ck_translation_semantic_census_count"
        ),
        db.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_semantic_census_plan_hash",
        ),
        db.CheckConstraint(
            "length(census_hash) = 64", name="ck_translation_semantic_census_hash"
        ),
        db.CheckConstraint(
            "length(coverage_hash) = 64",
            name="ck_translation_semantic_census_coverage_hash",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_semantic_census_record_hash",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_semantic_census_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_semantic_census_plan",
        ),
        db.Index(
            "ix_translation_semantic_census_plan_created",
            "source_plan_id",
            "created_at",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    profile_id = db.Column(db.String(64), nullable=False)
    profile_version = db.Column(db.String(64), nullable=False)
    occurrence_count = db.Column(db.Integer, nullable=False)
    census_hash = db.Column(db.String(64), nullable=False)
    coverage_hash = db.Column(db.String(64), nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    occurrences = db.relationship(
        "TranslationSemanticOccurrence",
        back_populates="census",
        cascade="all, delete-orphan",
        order_by="TranslationSemanticOccurrence.order_index",
    )


class TranslationSemanticOccurrence(db.Model):
    """Encrypted source-bearing occurrence with exact plan/block provenance."""

    __tablename__ = "translation_semantic_occurrence"
    __table_args__ = (
        db.UniqueConstraint(
            "census_id", "order_index", name="uq_translation_semantic_occurrence_order"
        ),
        db.CheckConstraint(
            "order_index >= 1", name="ck_translation_semantic_occurrence_order"
        ),
        db.CheckConstraint(
            "start_offset IS NULL OR start_offset >= 0",
            name="ck_translation_semantic_occurrence_start",
        ),
        db.CheckConstraint(
            "end_offset IS NULL OR (start_offset IS NOT NULL AND end_offset >= start_offset)",
            name="ck_translation_semantic_occurrence_end",
        ),
        db.CheckConstraint(
            "length(value_hash) = 64",
            name="ck_translation_semantic_occurrence_value_hash",
        ),
        db.CheckConstraint(
            "length(occurrence_hash) = 64",
            name="ck_translation_semantic_occurrence_hash",
        ),
        db.ForeignKeyConstraint(
            ["census_id", "source_plan_id"],
            [
                "translation_semantic_census.id",
                "translation_semantic_census.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_semantic_occurrence_census_plan",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_semantic_occurrence_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["anchor_id", "source_plan_id"],
            [
                "translation_source_anchor.id",
                "translation_source_anchor.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_semantic_occurrence_anchor_plan",
        ),
        db.Index(
            "ix_translation_semantic_occurrence_plan_block",
            "source_plan_id",
            "block_id",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    census_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=True)
    anchor_id = db.Column(db.String(128), nullable=True)
    order_index = db.Column(db.Integer, nullable=False)
    occurrence_kind = db.Column(db.String(64), nullable=False)
    slot_id = db.Column(db.String(128), nullable=True)
    slot_kind = db.Column(db.String(32), nullable=True)
    disposition = db.Column(db.String(64), nullable=True)
    start_offset = db.Column(db.Integer, nullable=True)
    end_offset = db.Column(db.Integer, nullable=True)
    structural_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    value_text = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    value_hash = db.Column(db.String(64), nullable=False)
    occurrence_hash = db.Column(db.String(64), nullable=False)
    census = db.relationship("TranslationSemanticCensus", back_populates="occurrences")


class TranslationContextArtifact(db.Model):
    """Stable identity for one typed Phase-5 context artifact."""

    __tablename__ = "translation_context_artifact"
    __table_args__ = (
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            name="uq_translation_context_artifact_scope",
        ),
        db.CheckConstraint(
            "artifact_kind IN ('document_brief', 'style_guide', 'entity_register')",
            name="ck_translation_context_artifact_kind",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_context_artifact_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_context_artifact_plan",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_context_artifact_owner",
        ),
        db.Index(
            "ix_translation_context_artifact_plan_kind",
            "source_plan_id",
            "artifact_kind",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    artifact_kind = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    revisions = db.relationship(
        "TranslationContextArtifactRevision",
        back_populates="artifact",
        cascade="all, delete-orphan",
        order_by="TranslationContextArtifactRevision.revision_number",
    )


class TranslationContextArtifactRevision(db.Model):
    """Append-only typed analysis output and exact input evidence."""

    __tablename__ = "translation_context_artifact_revision"
    __table_args__ = (
        db.UniqueConstraint(
            "artifact_id",
            "revision_number",
            name="uq_translation_context_artifact_revision_number",
        ),
        db.UniqueConstraint(
            "id",
            "source_plan_id",
            name="uq_translation_context_artifact_revision_id_plan",
        ),
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            name="uq_translation_context_artifact_revision_id_project_plan",
        ),
        db.UniqueConstraint(
            "id",
            "artifact_id",
            "source_plan_id",
            name="uq_translation_context_artifact_revision_scope",
        ),
        db.CheckConstraint(
            "revision_number >= 1",
            name="ck_translation_context_artifact_revision_number",
        ),
        db.CheckConstraint(
            "analysis_state IN ('human', 'model', 'system')",
            name="ck_translation_context_artifact_revision_state",
        ),
        *[
            db.CheckConstraint(
                f"length({field}) = 64",
                name=f"ck_translation_context_artifact_revision_{name}",
            )
            for field, name in (
                ("source_plan_hash", "plan_hash"),
                ("schema_hash", "schema_hash"),
                ("profile_hash", "profile_hash"),
                ("model_hash", "model_hash"),
                ("prompt_hash", "prompt_hash"),
                ("settings_hash", "settings_hash"),
                ("input_hash", "input_hash"),
                ("output_hash", "output_hash"),
                ("evidence_hash", "evidence_hash"),
                ("record_hash", "record_hash"),
            )
        ],
        db.ForeignKeyConstraint(
            ["artifact_id", "project_id", "source_plan_id"],
            [
                "translation_context_artifact.id",
                "translation_context_artifact.project_id",
                "translation_context_artifact.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_context_artifact_revision_artifact_scope",
        ),
        db.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["translation_context_artifact_revision.id"],
            ondelete="RESTRICT",
            name="fk_translation_context_artifact_revision_parent",
        ),
        db.Index(
            "ix_translation_context_artifact_revision_plan_kind",
            "source_plan_id",
            "created_at",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    artifact_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    revision_number = db.Column(db.Integer, nullable=False)
    parent_revision_id = db.Column(db.String(36), nullable=True)
    analysis_state = db.Column(db.String(16), nullable=False)
    schema_version = db.Column(db.String(64), nullable=False)
    profile_id = db.Column(db.String(64), nullable=False)
    profile_version = db.Column(db.String(64), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    schema_hash = db.Column(db.String(64), nullable=False)
    profile_hash = db.Column(db.String(64), nullable=False)
    model_hash = db.Column(db.String(64), nullable=False)
    prompt_hash = db.Column(db.String(64), nullable=False)
    settings_hash = db.Column(db.String(64), nullable=False)
    input_hash = db.Column(db.String(64), nullable=False)
    output_hash = db.Column(db.String(64), nullable=False)
    evidence_hash = db.Column(db.String(64), nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    # Frozen transport identities can include credentials in provider URLs.
    model_json = db.Column(EncryptedType(db.JSON, strategy="system"), nullable=False)
    prompt_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    settings_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    input_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    output_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    evidence_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    artifact = db.relationship("TranslationContextArtifact", back_populates="revisions")


class TranslationGlossaryConcept(db.Model):
    """Opaque concept identity; plaintext terminology never appears in this row."""

    __tablename__ = "translation_block_glossary_concept"
    __table_args__ = (
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            name="uq_translation_block_glossary_concept_scope",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_concept_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_concept_plan",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_concept_owner",
        ),
        db.Index(
            "ix_translation_block_glossary_concept_plan", "source_plan_id", "created_at"
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    concept_scope = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    revisions = db.relationship(
        "TranslationGlossaryRevision",
        back_populates="concept",
        cascade="all, delete-orphan",
        order_by="TranslationGlossaryRevision.revision_number",
    )


class TranslationGlossaryRevision(db.Model):
    """Encrypted, append-only term revision. Only human locked rows bind."""

    __tablename__ = "translation_block_glossary_revision"
    __table_args__ = (
        db.UniqueConstraint(
            "concept_id",
            "revision_number",
            name="uq_translation_block_glossary_revision_number",
        ),
        db.UniqueConstraint(
            "concept_id", "id", name="uq_translation_block_glossary_revision_concept_id"
        ),
        db.UniqueConstraint(
            "id",
            "source_plan_id",
            name="uq_translation_block_glossary_revision_id_plan",
        ),
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            name="uq_translation_block_glossary_revision_id_project_plan",
        ),
        db.UniqueConstraint(
            "approval_request_id",
            name="uq_translation_block_glossary_revision_approval",
        ),
        db.UniqueConstraint(
            "id",
            "concept_id",
            "project_id",
            "source_plan_id",
            name="uq_translation_block_glossary_revision_scope",
        ),
        db.CheckConstraint(
            "revision_number >= 1", name="ck_translation_block_glossary_revision_number"
        ),
        db.CheckConstraint(
            "revision_state IN ('proposal', 'reviewed', 'locked')",
            name="ck_translation_block_glossary_revision_state",
        ),
        db.CheckConstraint(
            "author_type IN ('human', 'model', 'system')",
            name="ck_translation_block_glossary_revision_author",
        ),
        db.CheckConstraint(
            "NOT (revision_state = 'locked' AND author_type <> 'human')",
            name="ck_translation_block_glossary_revision_human_lock",
        ),
        db.CheckConstraint(
            "(revision_state = 'locked' AND author_type = 'human' "
            "AND parent_revision_id IS NOT NULL AND approval_request_id IS NOT NULL "
            "AND approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND approval_receipt IS NOT NULL) OR "
            "(revision_state <> 'locked' AND approval_request_id IS NULL "
            "AND approved_by_user_id IS NULL AND approved_at IS NULL "
            "AND approval_receipt IS NULL)",
            name="ck_translation_block_glossary_revision_authority_shape",
        ),
        db.CheckConstraint(
            "approval_receipt IS NULL OR length(approval_receipt) = 64",
            name="ck_translation_block_glossary_revision_receipt",
        ),
        *[
            db.CheckConstraint(
                f"length({field}) = 64",
                name=f"ck_translation_block_glossary_revision_{name}",
            )
            for field, name in (
                ("normalization_hash", "normalization_hash"),
                ("source_digest", "source_digest"),
                ("target_digest", "target_digest"),
                ("payload_hash", "payload_hash"),
                ("record_hash", "record_hash"),
            )
        ],
        db.ForeignKeyConstraint(
            ["concept_id", "project_id", "source_plan_id"],
            [
                "translation_block_glossary_concept.id",
                "translation_block_glossary_concept.project_id",
                "translation_block_glossary_concept.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_revision_concept_scope",
        ),
        db.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["translation_block_glossary_revision.id"],
            ondelete="RESTRICT",
            name="fk_translation_block_glossary_revision_parent",
        ),
        db.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["user.id"],
            ondelete="RESTRICT",
            name="fk_translation_block_glossary_revision_approver",
        ),
        db.ForeignKeyConstraint(
            ["approval_request_id"],
            ["runtime_approval_request.id"],
            ondelete="RESTRICT",
            name="fk_translation_block_glossary_revision_approval",
        ),
        db.Index(
            "ix_translation_block_glossary_revision_lookup",
            "source_plan_id",
            "keyed_lookup_digest",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    concept_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    revision_number = db.Column(db.Integer, nullable=False)
    parent_revision_id = db.Column(db.String(36), nullable=True)
    approval_request_id = db.Column(db.String(36), nullable=True)
    approved_by_user_id = db.Column(db.Integer, nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    approval_receipt = db.Column(db.String(64), nullable=True)
    author_type = db.Column(db.String(16), nullable=False)
    revision_state = db.Column(db.String(16), nullable=False)
    normalization_profile = db.Column(db.String(64), nullable=False)
    normalization_version = db.Column(db.String(64), nullable=False)
    normalization_hash = db.Column(db.String(64), nullable=False)
    keyed_lookup_digest = db.Column(db.String(128), nullable=False)
    source_digest = db.Column(db.String(64), nullable=False)
    target_digest = db.Column(db.String(64), nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    concept = db.relationship("TranslationGlossaryConcept", back_populates="revisions")


class TranslationGlossaryActive(db.Model):
    """Replaceable CAS pointer; historical glossary revisions remain immutable."""

    __tablename__ = "translation_block_glossary_active"
    __table_args__ = (
        db.PrimaryKeyConstraint(
            "concept_id", name="pk_translation_block_glossary_active"
        ),
        db.CheckConstraint(
            "version >= 1", name="ck_translation_block_glossary_active_version"
        ),
        db.CheckConstraint(
            "length(active_receipt) = 64",
            name="ck_translation_block_glossary_active_receipt",
        ),
        db.ForeignKeyConstraint(
            ["concept_id", "revision_id"],
            [
                "translation_block_glossary_revision.concept_id",
                "translation_block_glossary_revision.id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_glossary_active_revision",
        ),
    )
    concept_id = db.Column(db.String(36), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    active_receipt = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )


class TranslationGlossaryOccurrence(db.Model):
    __tablename__ = "translation_block_glossary_occurrence"
    __table_args__ = (
        db.UniqueConstraint(
            "revision_id",
            "block_id",
            "anchor_id",
            "start_offset",
            "end_offset",
            name="uq_translation_block_glossary_occurrence_exact",
        ),
        # The migration uses the SQLite-compatible named unique index form.
        db.Index(
            "uq_translation_block_glossary_occurrence_hash",
            "revision_id",
            "occurrence_hash",
            unique=True,
        ),
        db.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="ck_translation_block_glossary_occurrence_span",
        ),
        db.CheckConstraint(
            "length(record_receipt) = 64",
            name="ck_translation_block_glossary_occurrence_receipt",
        ),
        db.ForeignKeyConstraint(
            ["revision_id", "concept_id", "project_id", "source_plan_id"],
            [
                "translation_block_glossary_revision.id",
                "translation_block_glossary_revision.concept_id",
                "translation_block_glossary_revision.project_id",
                "translation_block_glossary_revision.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_occurrence_revision_scope",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_occurrence_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["anchor_id", "source_plan_id"],
            [
                "translation_source_anchor.id",
                "translation_source_anchor.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_occurrence_anchor_plan",
        ),
        db.Index(
            "ix_translation_block_glossary_occurrence_plan_block",
            "source_plan_id",
            "block_id",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    revision_id = db.Column(db.String(36), nullable=False)
    concept_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    anchor_id = db.Column(db.String(128), nullable=True)
    start_offset = db.Column(db.Integer, nullable=False)
    end_offset = db.Column(db.Integer, nullable=False)
    occurrence_hash = db.Column(db.String(64), nullable=False)
    record_receipt = db.Column(db.String(64), nullable=False)


class TranslationContextBaseline(db.Model):
    """Immutable operation-bound selection of context evidence."""

    __tablename__ = "translation_context_baseline"
    __table_args__ = (
        db.UniqueConstraint(
            "operation_id", name="uq_translation_context_baseline_operation"
        ),
        db.UniqueConstraint(
            "id",
            "operation_id",
            "source_plan_id",
            name="uq_translation_context_baseline_scope",
        ),
        db.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_context_baseline_plan_hash",
        ),
        db.CheckConstraint(
            "length(manifest_hash) = 64",
            name="ck_translation_context_baseline_manifest_hash",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_context_baseline_record_hash",
        ),
        db.ForeignKeyConstraint(
            ["operation_id"],
            ["translation_operation.id"],
            ondelete="CASCADE",
            name="fk_translation_context_baseline_operation",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_context_baseline_plan",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_context_baseline_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_context_baseline_owner",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    manifest_hash = db.Column(db.String(64), nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    binding_attestation = db.Column(db.String(128), nullable=False)
    manifest_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    items = db.relationship(
        "TranslationContextBaselineItem",
        back_populates="baseline",
        cascade="all, delete-orphan",
        order_by="TranslationContextBaselineItem.order_index",
    )


class TranslationContextBaselineItem(db.Model):
    __tablename__ = "translation_context_baseline_item"
    __table_args__ = (
        db.UniqueConstraint(
            "baseline_id",
            "order_index",
            name="uq_translation_context_baseline_item_order",
        ),
        db.CheckConstraint(
            "order_index >= 1", name="ck_translation_context_baseline_item_order"
        ),
        db.CheckConstraint(
            "item_kind IN ('document_brief', 'style_guide', 'entity_register', 'glossary_locked', 'research_evidence', 'translation_memory', 'research_dossier')",
            name="ck_translation_context_baseline_item_kind",
        ),
        db.CheckConstraint(
            "(item_kind IN ('document_brief', 'style_guide', 'entity_register') "
            "AND artifact_revision_id IS NOT NULL AND glossary_revision_id IS NULL "
            "AND research_run_id IS NULL AND embedding_generation_id IS NULL "
            "AND research_dossier_revision_id IS NULL "
            "AND reference_json IS NULL) OR "
            "(item_kind = 'glossary_locked' "
            "AND artifact_revision_id IS NULL AND glossary_revision_id IS NOT NULL "
            "AND research_run_id IS NULL AND embedding_generation_id IS NULL "
            "AND research_dossier_revision_id IS NULL "
            "AND reference_json IS NULL) OR "
            "(item_kind = 'research_evidence' "
            "AND artifact_revision_id IS NULL AND glossary_revision_id IS NULL "
            "AND research_run_id IS NOT NULL AND embedding_generation_id IS NULL "
            "AND research_dossier_revision_id IS NULL "
            "AND reference_json IS NOT NULL) OR "
            "(item_kind = 'translation_memory' "
            "AND artifact_revision_id IS NULL AND glossary_revision_id IS NULL "
            "AND research_run_id IS NULL AND embedding_generation_id IS NOT NULL "
            "AND research_dossier_revision_id IS NULL "
            "AND reference_json IS NOT NULL) OR "
            "(item_kind = 'research_dossier' "
            "AND artifact_revision_id IS NULL AND glossary_revision_id IS NULL "
            "AND research_run_id IS NULL AND embedding_generation_id IS NULL "
            "AND research_dossier_revision_id IS NOT NULL "
            "AND reference_json IS NOT NULL)",
            name="ck_translation_context_baseline_item_reference_kind",
        ),
        db.CheckConstraint(
            "length(revision_hash) = 64",
            name="ck_translation_context_baseline_item_revision_hash",
        ),
        db.ForeignKeyConstraint(
            ["baseline_id", "operation_id", "source_plan_id"],
            [
                "translation_context_baseline.id",
                "translation_context_baseline.operation_id",
                "translation_context_baseline.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_context_baseline_item_baseline_scope",
        ),
        db.ForeignKeyConstraint(
            ["artifact_revision_id", "project_id", "source_plan_id"],
            [
                "translation_context_artifact_revision.id",
                "translation_context_artifact_revision.project_id",
                "translation_context_artifact_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_context_baseline_item_artifact_scope",
        ),
        db.ForeignKeyConstraint(
            ["glossary_revision_id", "project_id", "source_plan_id"],
            [
                "translation_block_glossary_revision.id",
                "translation_block_glossary_revision.project_id",
                "translation_block_glossary_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_context_baseline_item_glossary_scope",
        ),
        db.ForeignKeyConstraint(
            ["research_run_id", "source_plan_id"],
            ["translation_research_run.id", "translation_research_run.source_plan_id"],
            ondelete="RESTRICT",
            name="fk_translation_context_baseline_item_research_run_scope",
        ),
        db.ForeignKeyConstraint(
            ["embedding_generation_id", "project_id", "source_plan_id"],
            [
                "translation_embedding_generation.id",
                "translation_embedding_generation.project_id",
                "translation_embedding_generation.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_context_baseline_item_embedding_generation_scope",
        ),
        db.ForeignKeyConstraint(
            ["research_dossier_revision_id", "project_id", "source_plan_id"],
            [
                "translation_research_dossier_revision.id",
                "translation_research_dossier_revision.project_id",
                "translation_research_dossier_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_context_baseline_item_research_dossier_scope",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    baseline_id = db.Column(db.String(36), nullable=False)
    operation_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    order_index = db.Column(db.Integer, nullable=False)
    item_kind = db.Column(db.String(32), nullable=False)
    artifact_revision_id = db.Column(db.String(36), nullable=True)
    glossary_revision_id = db.Column(db.String(36), nullable=True)
    research_run_id = db.Column(db.String(36), nullable=True)
    embedding_generation_id = db.Column(db.String(36), nullable=True)
    reference_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=True)
    revision_hash = db.Column(db.String(64), nullable=False)
    slot_state = db.Column(
        db.String(32), nullable=False, default="enabled", server_default="enabled"
    )
    research_dossier_revision_id = db.Column(db.String(36), nullable=True)
    baseline = db.relationship("TranslationContextBaseline", back_populates="items")
    research_run = db.relationship("TranslationResearchRun", viewonly=True)
    embedding_generation = db.relationship(
        "TranslationEmbeddingGeneration", viewonly=True
    )
    research_dossier_revision = db.relationship(
        "TranslationResearchDossierRevision",
        foreign_keys=[research_dossier_revision_id, project_id, source_plan_id],
        viewonly=True,
    )


class TranslationSharedGlossary(db.Model):
    """Owner-scoped, independently retained terminology asset."""

    __tablename__ = "translation_shared_glossary"
    __table_args__ = (db.CheckConstraint("current_revision >= 1", name="ck_shared_glossary_revision"),)
    id = db.Column(db.String(36), primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    current_revision = db.Column(db.Integer, nullable=False)
    metadata_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    metadata_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)


class TranslationSharedGlossaryRevision(db.Model):
    __tablename__ = "translation_shared_glossary_revision"
    __table_args__ = (db.UniqueConstraint("asset_id", "revision", name="uq_translation_shared_glossary_revision"),)
    id = db.Column(db.String(36), primary_key=True)
    asset_id = db.Column(db.String(36), db.ForeignKey("translation_shared_glossary.id", ondelete="CASCADE"), nullable=False)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    revision = db.Column(db.Integer, nullable=False)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationSharedGlossaryProposal(db.Model):
    __tablename__ = "translation_shared_glossary_proposal"
    __table_args__ = (db.CheckConstraint("state IN ('pending','accepted','rejected')", name="ck_shared_glossary_proposal_state"),)
    id = db.Column(db.String(36), primary_key=True)
    asset_id = db.Column(db.String(36), db.ForeignKey("translation_shared_glossary.id", ondelete="CASCADE"), nullable=False)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    base_revision = db.Column(db.Integer, nullable=False)
    state = db.Column(db.String(16), nullable=False)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationProjectGlossaryBinding(db.Model):
    __tablename__ = "translation_project_glossary_binding"
    __table_args__ = (db.CheckConstraint("version >= 1", name="ck_project_glossary_binding_version"),)
    project_id = db.Column(db.String(36), db.ForeignKey("translation_project.id", ondelete="CASCADE"), primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    asset_id = db.Column(db.String(36), db.ForeignKey("translation_shared_glossary.id", ondelete="RESTRICT"), nullable=True)
    adopted_revision = db.Column(db.Integer, nullable=True)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    binding_receipt = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)


class TranslationSharedGlossaryReceipt(db.Model):
    __tablename__ = "translation_shared_glossary_receipt"
    __table_args__ = (db.UniqueConstraint("owner_user_id", "action", "retry_key", name="uq_translation_shared_glossary_retry"),)
    id = db.Column(db.String(36), primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    action = db.Column(db.String(32), nullable=False)
    retry_key = db.Column(db.String(160), nullable=False)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    integrity_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationResearchDossier(db.Model):
    """Stable identity for one owner-scoped research dossier."""

    __tablename__ = "translation_research_dossier"
    __table_args__ = (
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            name="uq_translation_research_dossier_scope",
        ),
        db.CheckConstraint(
            "scope_kind IN ('document', 'blocks')",
            name="ck_translation_research_dossier_scope_kind",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_owner",
        ),
        db.Index(
            "ix_translation_research_dossier_plan_created",
            "source_plan_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    scope_kind = db.Column(db.String(16), nullable=False)
    scope_block_ids = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    revisions = db.relationship(
        "TranslationResearchDossierRevision",
        back_populates="dossier",
        cascade="all, delete-orphan",
        order_by="TranslationResearchDossierRevision.revision_number",
    )


class TranslationResearchDossierRevision(db.Model):
    """Append-only, provenance-bearing revision of a research dossier."""

    __tablename__ = "translation_research_dossier_revision"
    __table_args__ = (
        db.UniqueConstraint(
            "dossier_id",
            "revision_number",
            name="uq_translation_research_dossier_revision_number",
        ),
        db.UniqueConstraint(
            "project_id",
            "client_submission_id",
            name="uq_translation_research_dossier_revision_submission",
        ),
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            name="uq_translation_research_dossier_revision_scope",
        ),
        db.CheckConstraint(
            "revision_number >= 1",
            name="ck_translation_research_dossier_revision_number",
        ),
        db.CheckConstraint(
            "length(client_submission_id) BETWEEN 1 AND 128",
            name="ck_translation_research_dossier_revision_submission",
        ),
        db.CheckConstraint(
            "length(content_digest) = 64",
            name="ck_translation_research_dossier_revision_content_digest",
        ),
        db.CheckConstraint(
            "conclusion_provenance IN ('external_unverified', 'managed_verified')",
            name="ck_translation_research_dossier_revision_conclusion_provenance",
        ),
        db.CheckConstraint(
            "evidence_verification_state IN ('unverified', 'partial', 'verified')",
            name="ck_translation_research_dossier_revision_verification_state",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_research_dossier_revision_record_hash",
        ),
        db.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_research_dossier_revision_integrity_receipt",
        ),
        db.ForeignKeyConstraint(
            ["dossier_id", "project_id", "source_plan_id"],
            [
                "translation_research_dossier.id",
                "translation_research_dossier.project_id",
                "translation_research_dossier.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_revision_dossier_scope",
        ),
        db.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["translation_research_dossier_revision.id"],
            ondelete="RESTRICT",
            name="fk_translation_research_dossier_revision_parent",
        ),
        db.Index(
            "ix_translation_research_dossier_revision_dossier_number",
            "dossier_id",
            "revision_number",
        ),
        db.Index(
            "ix_translation_research_dossier_revision_plan_created",
            "source_plan_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    dossier_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    revision_number = db.Column(db.Integer, nullable=False)
    parent_revision_id = db.Column(db.String(36), nullable=True)
    client_submission_id = db.Column(db.String(128), nullable=False)
    content_digest = db.Column(db.String(64), nullable=False)
    conclusion_provenance = db.Column(db.String(24), nullable=False)
    evidence_verification_state = db.Column(db.String(16), nullable=False)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    provenance_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    record_hash = db.Column(db.String(64), nullable=False)
    integrity_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    dossier = db.relationship(
        "TranslationResearchDossier",
        back_populates="revisions",
        foreign_keys=[dossier_id, project_id, source_plan_id],
    )


class TranslationShareGrant(db.Model):
    """Revocable capability grant represented only by a token digest."""

    __tablename__ = "translation_share_grant"
    __table_args__ = (
        db.CheckConstraint(
            "length(label) BETWEEN 1 AND 128",
            name="ck_translation_share_grant_label",
        ),
        db.CheckConstraint(
            "role IN ('viewer', 'reviewer')",
            name="ck_translation_share_grant_role",
        ),
        db.CheckConstraint(
            "length(token_digest) = 128",
            name="ck_translation_share_grant_token_digest",
        ),
        db.CheckConstraint(
            "length(token_hint) BETWEEN 1 AND 8",
            name="ck_translation_share_grant_token_hint",
        ),
        db.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_translation_share_grant_status",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_share_grant_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_share_grant_owner",
        ),
        db.Index(
            "uq_translation_share_grant_token_digest",
            "token_digest",
            unique=True,
        ),
        db.Index(
            "ix_translation_share_grant_project_status",
            "project_id",
            "status",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    label = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(16), nullable=False)
    token_digest = db.Column(db.String(128), nullable=False)
    token_hint = db.Column(db.String(8), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="active")
    expires_at = db.Column(db.DateTime, nullable=True)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationShareSession(db.Model):
    """Short-lived share session bound to a grant and a CSRF digest."""

    __tablename__ = "translation_share_session"
    __table_args__ = (
        db.CheckConstraint(
            "length(session_digest) = 128",
            name="ck_translation_share_session_digest",
        ),
        db.CheckConstraint(
            "length(csrf_digest) = 128",
            name="ck_translation_share_session_csrf_digest",
        ),
        db.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_translation_share_session_status",
        ),
        db.ForeignKeyConstraint(
            ["grant_id"],
            ["translation_share_grant.id"],
            ondelete="CASCADE",
            name="fk_translation_share_session_grant",
        ),
        db.Index(
            "uq_translation_share_session_digest",
            "session_digest",
            unique=True,
        ),
        db.Index(
            "ix_translation_share_session_grant_status",
            "grant_id",
            "status",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    grant_id = db.Column(db.String(36), nullable=False)
    session_digest = db.Column(db.String(128), nullable=False)
    csrf_digest = db.Column(db.String(128), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="active")
    expires_at = db.Column(db.DateTime, nullable=False)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationReaderNote(db.Model):
    """Stable identity for reader annotations attached to a source block."""

    __tablename__ = "translation_reader_note"
    __table_args__ = (
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            "block_id",
            name="uq_translation_reader_note_scope",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_reader_note_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_reader_note_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_reader_note_block_scope",
        ),
        db.Index(
            "ix_translation_reader_note_plan_block_created",
            "source_plan_id",
            "block_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    revisions = db.relationship(
        "TranslationReaderNoteRevision",
        back_populates="note",
        cascade="all, delete-orphan",
        order_by="TranslationReaderNoteRevision.revision_number",
    )


class TranslationReaderNoteRevision(db.Model):
    """Append-only reader note revision, including share provenance."""

    __tablename__ = "translation_reader_note_revision"
    __table_args__ = (
        db.UniqueConstraint(
            "id", "note_id", name="uq_translation_reader_note_revision_note"
        ),
        db.UniqueConstraint(
            "note_id",
            "revision_number",
            name="uq_translation_reader_note_revision_number",
        ),
        db.CheckConstraint(
            "revision_number >= 1",
            name="ck_translation_reader_note_revision_number",
        ),
        db.CheckConstraint(
            "state IN ('proposed', 'active', 'retired', 'needs_review')",
            name="ck_translation_reader_note_revision_state",
        ),
        db.CheckConstraint(
            "anchor_kind IN ('block', 'source', 'target')",
            name="ck_translation_reader_note_revision_anchor_kind",
        ),
        db.CheckConstraint(
            "occurrence_index IS NULL OR occurrence_index >= 0",
            name="ck_translation_reader_note_revision_occurrence",
        ),
        db.CheckConstraint(
            "(anchor_kind = 'block' AND target_revision_id IS NULL AND occurrence_index IS NULL) OR "
            "(anchor_kind = 'source' AND target_revision_id IS NULL) OR "
            "(anchor_kind = 'target' AND target_revision_id IS NOT NULL "
            "AND anchor_text IS NOT NULL AND occurrence_index IS NOT NULL)",
            name="ck_translation_reader_note_revision_anchor_shape",
        ),
        db.CheckConstraint(
            "author_type IN ('owner', 'reviewer', 'model', 'system')",
            name="ck_translation_reader_note_revision_author_type",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_reader_note_revision_record_hash",
        ),
        db.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_reader_note_revision_integrity_receipt",
        ),
        db.ForeignKeyConstraint(
            ["note_id", "project_id", "source_plan_id", "block_id"],
            [
                "translation_reader_note.id",
                "translation_reader_note.project_id",
                "translation_reader_note.source_plan_id",
                "translation_reader_note.block_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_reader_note_revision_note_scope",
        ),
        db.ForeignKeyConstraint(
            ["parent_revision_id", "note_id"],
            [
                "translation_reader_note_revision.id",
                "translation_reader_note_revision.note_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_reader_note_revision_parent",
        ),
        db.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_reader_note_revision_target",
        ),
        db.ForeignKeyConstraint(
            ["author_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_reader_note_revision_author",
        ),
        db.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_reader_note_revision_share_grant",
        ),
        db.Index(
            "ix_translation_reader_note_revision_plan_block_created",
            "source_plan_id",
            "block_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    note_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_number = db.Column(db.Integer, nullable=False)
    parent_revision_id = db.Column(db.String(36), nullable=True)
    state = db.Column(db.String(16), nullable=False)
    anchor_kind = db.Column(db.String(16), nullable=False)
    target_revision_id = db.Column(db.String(36), nullable=True)
    anchor_text = db.Column(ContentType(db.Text, strategy="system"), nullable=True)
    occurrence_index = db.Column(db.Integer, nullable=True)
    witness_before = db.Column(ContentType(db.Text, strategy="system"), nullable=True)
    witness_after = db.Column(ContentType(db.Text, strategy="system"), nullable=True)
    body = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    citations_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    author_type = db.Column(db.String(16), nullable=False)
    author_user_id = db.Column(db.Integer, nullable=True)
    share_grant_id = db.Column(db.String(36), nullable=True)
    record_hash = db.Column(db.String(64), nullable=False)
    integrity_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    note = db.relationship(
        "TranslationReaderNote",
        back_populates="revisions",
        foreign_keys=[note_id, project_id, source_plan_id, block_id],
    )


class TranslationReaderNoteHead(db.Model):
    """Current pointer; retired and needs-review revisions remain addressable."""

    __tablename__ = "translation_reader_note_head"
    __table_args__ = (
        db.PrimaryKeyConstraint("note_id", name="pk_translation_reader_note_head"),
        db.UniqueConstraint("revision_id", name="uq_translation_reader_note_head_revision"),
        db.CheckConstraint("version >= 1", name="ck_translation_reader_note_head_version"),
        db.ForeignKeyConstraint(
            ["note_id"],
            ["translation_reader_note.id"],
            ondelete="CASCADE",
            name="fk_translation_reader_note_head_note",
        ),
        db.ForeignKeyConstraint(
            ["revision_id", "note_id"],
            [
                "translation_reader_note_revision.id",
                "translation_reader_note_revision.note_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_reader_note_head_revision",
        ),
    )

    note_id = db.Column(db.String(36), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )


class TranslationResearchDossierGlossaryProposal(db.Model):
    """Explicit link from external dossier evidence to a glossary revision."""

    __tablename__ = "translation_research_dossier_glossary_proposal"
    __table_args__ = (
        db.UniqueConstraint(
            "dossier_revision_id",
            "glossary_revision_id",
            name="uq_translation_research_dossier_glossary_proposal_pair",
        ),
        db.CheckConstraint(
            "order_index >= 0",
            name="ck_translation_research_dossier_glossary_proposal_order",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_glossary_proposal_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_glossary_proposal_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["dossier_revision_id", "project_id", "source_plan_id"],
            [
                "translation_research_dossier_revision.id",
                "translation_research_dossier_revision.project_id",
                "translation_research_dossier_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_research_dossier_glossary_proposal_dossier",
        ),
        db.ForeignKeyConstraint(
            ["glossary_revision_id", "project_id", "source_plan_id"],
            [
                "translation_block_glossary_revision.id",
                "translation_block_glossary_revision.project_id",
                "translation_block_glossary_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_research_dossier_glossary_proposal_glossary",
        ),
        db.Index(
            "ix_translation_research_dossier_glossary_proposal_plan",
            "source_plan_id",
            "dossier_revision_id",
            "order_index",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    dossier_revision_id = db.Column(db.String(36), nullable=False)
    glossary_revision_id = db.Column(db.String(36), nullable=False)
    order_index = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationGlossarySuggestion(db.Model):
    """Reviewer/external terminology proposal awaiting glossary authority."""

    __tablename__ = "translation_block_glossary_suggestion"
    __table_args__ = (
        db.CheckConstraint(
            "proposer_kind IN ('reviewer', 'external')",
            name="ck_translation_block_glossary_suggestion_proposer_kind",
        ),
        db.CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected')",
            name="ck_translation_block_glossary_suggestion_state",
        ),
        db.CheckConstraint(
            "version >= 1",
            name="ck_translation_block_glossary_suggestion_version",
        ),
        db.CheckConstraint(
            "(proposer_kind = 'reviewer' AND share_grant_id IS NOT NULL "
            "AND dossier_revision_id IS NULL) OR "
            "(proposer_kind = 'external' AND dossier_revision_id IS NOT NULL "
            "AND share_grant_id IS NULL)",
            name="ck_translation_block_glossary_suggestion_provenance",
        ),
        db.CheckConstraint(
            "(state = 'accepted' AND accepted_revision_id IS NOT NULL) OR "
            "(state <> 'accepted' AND accepted_revision_id IS NULL)",
            name="ck_translation_block_glossary_suggestion_acceptance",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_suggestion_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_suggestion_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["proposer_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_block_glossary_suggestion_proposer",
        ),
        db.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_block_glossary_suggestion_share_grant",
        ),
        db.ForeignKeyConstraint(
            ["dossier_revision_id", "project_id", "source_plan_id"],
            [
                "translation_research_dossier_revision.id",
                "translation_research_dossier_revision.project_id",
                "translation_research_dossier_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_glossary_suggestion_dossier",
        ),
        db.ForeignKeyConstraint(
            ["accepted_revision_id", "project_id", "source_plan_id"],
            [
                "translation_block_glossary_revision.id",
                "translation_block_glossary_revision.project_id",
                "translation_block_glossary_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_glossary_suggestion_accepted_revision",
        ),
        db.Index(
            "ix_translation_block_glossary_suggestion_plan_state_created",
            "source_plan_id",
            "state",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_term = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    target_term = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    notes = db.Column(ContentType(db.Text, strategy="system"), nullable=True)
    proposer_kind = db.Column(db.String(16), nullable=False)
    proposer_user_id = db.Column(db.Integer, nullable=True)
    share_grant_id = db.Column(db.String(36), nullable=True)
    dossier_revision_id = db.Column(db.String(36), nullable=True)
    state = db.Column(db.String(16), nullable=False, default="proposed")
    accepted_revision_id = db.Column(db.String(36), nullable=True)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )


class TranslationBlockReviewRevision(db.Model):
    """Append-only owner/reviewer decision for one translated block."""

    __tablename__ = "translation_block_review_revision"
    __table_args__ = (
        db.UniqueConstraint(
            "project_id",
            "source_plan_id",
            "block_id",
            "revision_number",
            name="uq_translation_block_review_revision_number",
        ),
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            "block_id",
            name="uq_translation_block_review_revision_scope",
        ),
        db.CheckConstraint(
            "revision_number >= 1",
            name="ck_translation_block_review_revision_number",
        ),
        db.CheckConstraint(
            "decision IN ('pending', 'approved', 'needs_changes')",
            name="ck_translation_block_review_revision_decision",
        ),
        db.CheckConstraint(
            "author_type IN ('owner', 'reviewer')",
            name="ck_translation_block_review_revision_author_type",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_block_review_revision_record_hash",
        ),
        db.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_block_review_revision_integrity_receipt",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_revision_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_revision_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_revision_block_scope",
        ),
        db.ForeignKeyConstraint(
            ["parent_revision_id", "project_id", "source_plan_id", "block_id"],
            [
                "translation_block_review_revision.id",
                "translation_block_review_revision.project_id",
                "translation_block_review_revision.source_plan_id",
                "translation_block_review_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_review_revision_parent",
        ),
        db.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_review_revision_target",
        ),
        db.ForeignKeyConstraint(
            ["author_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_block_review_revision_author",
        ),
        db.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_block_review_revision_share_grant",
        ),
        db.Index(
            "ix_translation_block_review_revision_plan_block_created",
            "source_plan_id",
            "block_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_number = db.Column(db.Integer, nullable=False)
    parent_revision_id = db.Column(db.String(36), nullable=True)
    target_revision_id = db.Column(db.String(36), nullable=True)
    decision = db.Column(db.String(16), nullable=False, default="pending")
    comment = db.Column(ContentType(db.Text, strategy="system"), nullable=True)
    author_type = db.Column(db.String(16), nullable=False)
    author_user_id = db.Column(db.Integer, nullable=True)
    share_grant_id = db.Column(db.String(36), nullable=True)
    record_hash = db.Column(db.String(64), nullable=False)
    integrity_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationBlockReviewHead(db.Model):
    """Replaceable current pointer for block review state."""

    __tablename__ = "translation_block_review_head"
    __table_args__ = (
        db.PrimaryKeyConstraint(
            "project_id",
            "source_plan_id",
            "block_id",
            name="pk_translation_block_review_head",
        ),
        db.UniqueConstraint(
            "revision_id", name="uq_translation_block_review_head_revision"
        ),
        db.CheckConstraint(
            "version >= 1", name="ck_translation_block_review_head_version"
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_head_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_head_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_head_block_scope",
        ),
        db.ForeignKeyConstraint(
            ["revision_id", "project_id", "source_plan_id", "block_id"],
            [
                "translation_block_review_revision.id",
                "translation_block_review_revision.project_id",
                "translation_block_review_revision.source_plan_id",
                "translation_block_review_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_review_head_revision",
        ),
    )

    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    revision_id = db.Column(db.String(36), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )


class TranslationReviewCoverage(db.Model):
    """Append-only coverage evidence for one reviewed batch."""

    __tablename__ = "translation_review_coverage"
    __table_args__ = (
        db.CheckConstraint(
            "kind IN ('structural', 'target_reading', 'bilingual')",
            name="ck_translation_review_coverage_kind",
        ),
        db.CheckConstraint(
            "reviewer_kind IN ('agent', 'human')",
            name="ck_translation_review_coverage_reviewer_kind",
        ),
        db.CheckConstraint(
            "actor_type IN ('owner', 'reviewer')",
            name="ck_translation_review_coverage_actor_type",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_review_coverage_record_hash",
        ),
        db.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_review_coverage_integrity_receipt",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_review_coverage_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_review_coverage_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_review_coverage_owner",
        ),
        db.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_review_coverage_actor",
        ),
        db.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_review_coverage_share_grant",
        ),
        db.Index(
            "ix_translation_review_coverage_plan_created",
            "source_plan_id",
            "created_at",
        ),
    )

    # The primary key is a deterministic UUID5 over the caller's idempotency
    # key and this project/source-plan scope.  The raw key is not persisted.
    id = db.Column(db.String(36), primary_key=True)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(24), nullable=False)
    reviewer_kind = db.Column(db.String(16), nullable=False)
    actor_type = db.Column(db.String(16), nullable=False)
    actor_user_id = db.Column(db.Integer, nullable=True)
    share_grant_id = db.Column(db.String(36), nullable=True)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    integrity_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationRetranslationRequest(db.Model):
    """Owner/share-actor request to retranslate a bounded source scope."""

    __tablename__ = "translation_retranslation_request"
    __table_args__ = (
        db.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_translation_retranslation_request_idempotency",
        ),
        db.UniqueConstraint(
            "operation_id",
            name="uq_translation_retranslation_request_operation",
        ),
        db.CheckConstraint(
            "scope_kind IN ('blocks', 'order_range')",
            name="ck_translation_retranslation_request_scope_kind",
        ),
        db.CheckConstraint(
            "annotation_profile IN ('none', 'restrained', 'study')",
            name="ck_translation_retranslation_request_annotation_profile",
        ),
        db.CheckConstraint(
            "research_choice IN ('reuse_dossier', 'fresh_document', 'await_external')",
            name="ck_translation_retranslation_request_research_choice",
        ),
        db.CheckConstraint(
            "state IN ('requested', 'started', 'rejected', 'cancelled', 'completed')",
            name="ck_translation_retranslation_request_state",
        ),
        db.CheckConstraint(
            "version >= 1",
            name="ck_translation_retranslation_request_version",
        ),
        db.CheckConstraint(
            "(requester_user_id IS NOT NULL AND share_grant_id IS NULL) OR "
            "(requester_user_id IS NULL AND share_grant_id IS NOT NULL)",
            name="ck_translation_retranslation_request_actor",
        ),
        db.CheckConstraint(
            "(research_choice = 'reuse_dossier' AND dossier_revision_id IS NOT NULL) OR "
            "(research_choice IN ('fresh_document', 'await_external') AND dossier_revision_id IS NULL)",
            name="ck_translation_retranslation_request_research",
        ),
        db.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_translation_retranslation_request_idempotency_key",
        ),
        db.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_translation_retranslation_request_fingerprint",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_retranslation_request_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_retranslation_request_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["requester_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_retranslation_request_requester",
        ),
        db.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_retranslation_request_share_grant",
        ),
        db.ForeignKeyConstraint(
            ["dossier_revision_id", "project_id", "source_plan_id"],
            [
                "translation_research_dossier_revision.id",
                "translation_research_dossier_revision.project_id",
                "translation_research_dossier_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_retranslation_request_dossier_revision",
        ),
        db.ForeignKeyConstraint(
            ["operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_retranslation_request_operation",
        ),
        db.Index(
            "ix_translation_retranslation_request_plan_state_created",
            "source_plan_id",
            "state",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    requester_user_id = db.Column(db.Integer, nullable=True)
    share_grant_id = db.Column(db.String(36), nullable=True)
    scope_kind = db.Column(db.String(16), nullable=False)
    scope_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    custom_prompt = db.Column(ContentType(db.Text, strategy="system"), nullable=True)
    annotation_profile = db.Column(db.String(16), nullable=False)
    research_choice = db.Column(db.String(20), nullable=False)
    rationale = db.Column(ContentType(db.Text, strategy="system"), nullable=True)
    state = db.Column(db.String(16), nullable=False, default="requested")
    dossier_revision_id = db.Column(db.String(36), nullable=True)
    operation_id = db.Column(db.String(36), nullable=True)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )


class TranslationTargetRevisionShareAttribution(db.Model):
    """Immutable share provenance for accountless target revisions."""

    __tablename__ = "translation_target_revision_share_attribution"
    __table_args__ = (
        db.UniqueConstraint(
            "target_revision_id",
            name="uq_translation_target_revision_share_attribution_target",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_target_revision_share_attribution_record_hash",
        ),
        db.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_target_revision_share_attribution_integrity_receipt",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_target_revision_share_attribution_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_target_revision_share_attribution_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_revision_share_attribution_block_scope",
        ),
        db.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_target_revision_share_attribution_target",
        ),
        db.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="RESTRICT",
            name="fk_translation_target_revision_share_attribution_grant",
        ),
        db.Index(
            "ix_translation_target_revision_share_attribution_grant_created",
            "share_grant_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    target_revision_id = db.Column(db.String(36), nullable=False)
    share_grant_id = db.Column(db.String(36), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    integrity_receipt = db.Column(db.String(64), nullable=False)


class TranslationContextImpact(db.Model):
    """Append-only review/retranslation candidate evidence; never mutates truth."""

    __tablename__ = "translation_context_impact"
    __table_args__ = (
        db.UniqueConstraint(
            "source_plan_id", "impact_key", name="uq_translation_context_impact_key"
        ),
        db.CheckConstraint(
            "impact_state IN ('needs_review', 'retranslation_candidate')",
            name="ck_translation_context_impact_state",
        ),
        db.CheckConstraint(
            "length(record_receipt) = 64", name="ck_translation_context_impact_receipt"
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_context_impact_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_context_impact_plan",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_context_impact_block_plan",
        ),
        db.ForeignKeyConstraint(
            ["old_glossary_revision_id", "project_id", "source_plan_id"],
            [
                "translation_block_glossary_revision.id",
                "translation_block_glossary_revision.project_id",
                "translation_block_glossary_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_context_impact_old_glossary_scope",
        ),
        db.ForeignKeyConstraint(
            ["new_glossary_revision_id", "project_id", "source_plan_id"],
            [
                "translation_block_glossary_revision.id",
                "translation_block_glossary_revision.project_id",
                "translation_block_glossary_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_context_impact_new_glossary_scope",
        ),
        db.ForeignKeyConstraint(
            ["old_artifact_revision_id", "project_id", "source_plan_id"],
            [
                "translation_context_artifact_revision.id",
                "translation_context_artifact_revision.project_id",
                "translation_context_artifact_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_context_impact_old_artifact_scope",
        ),
        db.ForeignKeyConstraint(
            ["new_artifact_revision_id", "project_id", "source_plan_id"],
            [
                "translation_context_artifact_revision.id",
                "translation_context_artifact_revision.project_id",
                "translation_context_artifact_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_context_impact_new_artifact_scope",
        ),
        db.Index(
            "ix_translation_context_impact_plan_block", "source_plan_id", "block_id"
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    old_glossary_revision_id = db.Column(db.String(36), nullable=True)
    new_glossary_revision_id = db.Column(db.String(36), nullable=True)
    old_artifact_revision_id = db.Column(db.String(36), nullable=True)
    new_artifact_revision_id = db.Column(db.String(36), nullable=True)
    reason_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    impact_state = db.Column(db.String(32), nullable=False)
    impact_key = db.Column(db.String(64), nullable=False)
    record_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)


class TranslationAnalysisAttempt(db.Model):
    """Immutable, provider-agnostic evidence for one Phase-5 analysis call."""

    __tablename__ = "translation_analysis_attempt"
    __table_args__ = (
        db.UniqueConstraint(
            "id", "source_plan_id", name="uq_translation_analysis_attempt_id_plan"
        ),
        db.CheckConstraint(
            "analysis_kind IN ('document_brief', 'style_guide', 'entity_register', 'terminology_consolidation', 'research', 'embedding_projection', 'memory_derivation')",
            name="ck_translation_analysis_attempt_kind",
        ),
        db.CheckConstraint(
            "contract_version IN ('analysis-attempt-v1', 'analysis-attempt-v2')",
            name="ck_translation_analysis_attempt_contract_version",
        ),
        *[
            db.CheckConstraint(
                f"length({field}) = 64", name=f"ck_translation_analysis_attempt_{name}"
            )
            for field, name in (
                ("source_plan_hash", "plan_hash"),
                ("source_content_hash", "source_hash"),
                ("lease_fence", "lease_fence"),
                ("packet_hash", "packet_hash"),
                ("model_hash", "model_hash"),
                ("profile_hash", "profile_hash"),
                ("settings_hash", "settings_hash"),
                ("budget_hash", "budget_hash"),
                ("tools_hash", "tools_hash"),
                ("request_hash", "request_hash"),
                ("attempt_record_hash", "record_hash"),
                ("dispatch_receipt_hash", "dispatch_receipt_hash"),
            )
        ],
        db.CheckConstraint(
            "operation_version >= 1",
            name="ck_translation_analysis_attempt_operation_version",
        ),
        db.CheckConstraint(
            "dispatch_version >= 1",
            name="ck_translation_analysis_attempt_dispatch_version",
        ),
        db.CheckConstraint(
            "dispatch_state IN ('prepared', 'dispatched', 'response_received', 'completed', 'provider_failed', 'provider_outcome_uncertain', 'fence_failed', 'cancelled', 'failed')",
            name="ck_translation_analysis_attempt_dispatch_state",
        ),
        db.CheckConstraint(
            "retry_of_attempt_hash IS NULL OR length(retry_of_attempt_hash) = 64",
            name="ck_translation_analysis_attempt_retry_hash",
        ),
        db.CheckConstraint(
            "rebase_of_attempt_hash IS NULL OR length(rebase_of_attempt_hash) = 64",
            name="ck_translation_analysis_attempt_rebase_hash",
        ),
        db.CheckConstraint(
            "(retry_of_attempt_id IS NULL AND retry_of_attempt_hash IS NULL) OR (retry_of_attempt_id IS NOT NULL AND retry_of_attempt_hash IS NOT NULL)",
            name="ck_translation_analysis_attempt_retry_witness",
        ),
        db.CheckConstraint(
            "(rebase_of_attempt_id IS NULL AND rebase_of_attempt_hash IS NULL) OR (rebase_of_attempt_id IS NOT NULL AND rebase_of_attempt_hash IS NOT NULL)",
            name="ck_translation_analysis_attempt_rebase_witness",
        ),
        db.CheckConstraint(
            "retry_of_attempt_id IS NULL OR rebase_of_attempt_id IS NULL",
            name="ck_translation_analysis_attempt_single_lineage",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_owner",
        ),
        db.ForeignKeyConstraint(
            ["operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_operation_scope",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["retry_of_attempt_id"],
            ["translation_analysis_attempt.id"],
            ondelete="RESTRICT",
            name="fk_translation_analysis_attempt_retry",
        ),
        db.ForeignKeyConstraint(
            ["rebase_of_attempt_id"],
            ["translation_analysis_attempt.id"],
            ondelete="RESTRICT",
            name="fk_translation_analysis_attempt_rebase",
        ),
        db.Index(
            "ix_translation_analysis_attempt_operation_created",
            "operation_id",
            "created_at",
        ),
        db.Index(
            "ix_translation_analysis_attempt_plan_kind_created",
            "source_plan_id",
            "analysis_kind",
            "created_at",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    operation_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    analysis_kind = db.Column(db.String(32), nullable=False)
    contract_version = db.Column(db.String(64), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    source_content_hash = db.Column(db.String(64), nullable=False)
    operation_version = db.Column(db.Integer, nullable=False)
    queue_job_id = db.Column(db.String(128), nullable=False)
    operation_lease_expires_at = db.Column(db.DateTime, nullable=False)
    lease_fence = db.Column(db.String(64), nullable=False)
    packet_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    model_json = db.Column(EncryptedType(db.JSON, strategy="system"), nullable=False)
    profile_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    settings_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    budget_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    tool_declarations_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    packet_hash = db.Column(db.String(64), nullable=False)
    model_hash = db.Column(db.String(64), nullable=False)
    profile_hash = db.Column(db.String(64), nullable=False)
    settings_hash = db.Column(db.String(64), nullable=False)
    budget_hash = db.Column(db.String(64), nullable=False)
    tools_hash = db.Column(db.String(64), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    retry_of_attempt_id = db.Column(db.String(36), nullable=True)
    retry_of_attempt_hash = db.Column(db.String(64), nullable=True)
    rebase_of_attempt_id = db.Column(db.String(36), nullable=True)
    rebase_of_attempt_hash = db.Column(db.String(64), nullable=True)
    attempt_record_hash = db.Column(db.String(64), nullable=False)
    dispatch_receipt_hash = db.Column(db.String(64), nullable=False)
    dispatch_state = db.Column(
        db.String(32), nullable=False, default="prepared", server_default="prepared"
    )
    dispatch_version = db.Column(
        db.Integer, nullable=False, default=1, server_default="1"
    )
    dispatch_claimed_at = db.Column(db.DateTime, nullable=True)
    dispatch_finished_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    project = db.relationship("TranslationProject", viewonly=True)
    owner = db.relationship("User", foreign_keys=[owner_user_id], viewonly=True)
    operation = db.relationship("TranslationOperation", viewonly=True)
    source_plan = db.relationship("TranslationSourcePlan", viewonly=True)
    events = db.relationship(
        "TranslationAnalysisAttemptEvent",
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="TranslationAnalysisAttemptEvent.sequence",
    )


class TranslationAnalysisAttemptEvent(db.Model):
    """Encrypted append-only transcript with an explicit event hash chain."""

    __tablename__ = "translation_analysis_attempt_event"
    __table_args__ = (
        db.UniqueConstraint(
            "attempt_id",
            "sequence",
            name="uq_translation_analysis_attempt_event_sequence",
        ),
        db.CheckConstraint(
            "sequence >= 1", name="ck_translation_analysis_attempt_event_sequence"
        ),
        db.CheckConstraint(
            "event_type IN ('request_prepared', 'provider_dispatched', 'provider_response', 'provider_failure', 'provider_outcome_uncertain', 'post_response_fence_failed', 'cancelled', 'completed', 'failed')",
            name="ck_translation_analysis_attempt_event_type",
        ),
        db.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_translation_analysis_attempt_event_payload_hash",
        ),
        db.CheckConstraint(
            "length(event_hash) = 64", name="ck_translation_analysis_attempt_event_hash"
        ),
        db.CheckConstraint(
            "predecessor_event_hash IS NULL OR length(predecessor_event_hash) = 64",
            name="ck_translation_analysis_attempt_event_predecessor_hash",
        ),
        db.ForeignKeyConstraint(
            ["attempt_id"],
            ["translation_analysis_attempt.id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_event_attempt",
        ),
        db.Index(
            "ix_translation_analysis_attempt_event_attempt_created",
            "attempt_id",
            "created_at",
        ),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    attempt_id = db.Column(db.String(36), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    event_type = db.Column(db.String(64), nullable=False)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    predecessor_event_hash = db.Column(db.String(64), nullable=True)
    event_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    attempt = db.relationship("TranslationAnalysisAttempt", back_populates="events")


class TranslationResearchRun(db.Model):
    """Durable owner-scoped research orchestration evidence.

    This table records preparation only.  Provider and tool execution belong
    to a later runtime layer; keeping the frozen request and live-operation
    fence here means that layer cannot accidentally execute a different plan.
    """

    __tablename__ = "translation_research_run"
    __table_args__ = (
        db.UniqueConstraint(
            "id", "source_plan_id", name="uq_translation_research_run_id_plan"
        ),
        db.CheckConstraint(
            "contract_version = 'research-run-v1'",
            name="ck_translation_research_run_contract_version",
        ),
        db.CheckConstraint(
            "state IN ('prepared', 'running', 'completed', 'failed', 'cancelled', 'uncertain')",
            name="ck_translation_research_run_state",
        ),
        db.CheckConstraint(
            "state_version >= 1", name="ck_translation_research_run_state_version"
        ),
        db.CheckConstraint(
            "operation_version >= 1",
            name="ck_translation_research_run_operation_version",
        ),
        db.CheckConstraint(
            "(state = 'prepared' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(state = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(state IN ('completed', 'uncertain') AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(state IN ('failed', 'cancelled') AND finished_at IS NOT NULL)",
            name="ck_translation_research_run_timestamps",
        ),
        *[
            db.CheckConstraint(
                f"length({field}) = 64",
                name=f"ck_translation_research_run_{name}",
            )
            for field, name in (
                ("source_plan_hash", "plan_hash"),
                ("source_content_hash", "source_hash"),
                ("lease_fence", "lease_fence"),
                ("model_hash", "model_hash"),
                ("profile_hash", "profile_hash"),
                ("settings_hash", "settings_hash"),
                ("budget_hash", "budget_hash"),
                ("providers_hash", "providers_hash"),
                ("addons_hash", "addons_hash"),
                ("tools_hash", "tools_hash"),
                ("run_record_hash", "record_hash"),
                ("state_head_receipt", "state_head_receipt"),
            )
        ],
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_research_run_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_research_run_owner",
        ),
        db.ForeignKeyConstraint(
            ["operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="CASCADE",
            name="fk_translation_research_run_operation_scope",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_research_run_plan_scope",
        ),
        db.Index(
            "ix_translation_research_run_operation_created",
            "operation_id",
            "created_at",
        ),
        db.Index(
            "ix_translation_research_run_plan_state",
            "source_plan_id",
            "state",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    operation_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    source_content_hash = db.Column(db.String(64), nullable=False)
    contract_version = db.Column(
        db.String(64), nullable=False, default="research-run-v1"
    )
    operation_version = db.Column(db.Integer, nullable=False)
    queue_job_id = db.Column(db.String(128), nullable=False)
    operation_lease_expires_at = db.Column(db.DateTime, nullable=False)
    lease_fence = db.Column(db.String(64), nullable=False)
    model_json = db.Column(EncryptedType(db.JSON, strategy="system"), nullable=False)
    settings_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    budget_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    profile_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    selected_provider_ids_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    selected_addon_ids_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    tool_declarations_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    model_hash = db.Column(db.String(64), nullable=False)
    settings_hash = db.Column(db.String(64), nullable=False)
    budget_hash = db.Column(db.String(64), nullable=False)
    profile_hash = db.Column(db.String(64), nullable=False)
    providers_hash = db.Column(db.String(64), nullable=False)
    addons_hash = db.Column(db.String(64), nullable=False)
    tools_hash = db.Column(db.String(64), nullable=False)
    run_record_hash = db.Column(db.String(64), nullable=False)
    state = db.Column(
        db.String(32), nullable=False, default="prepared", server_default="prepared"
    )
    state_version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    state_head_receipt = db.Column(db.String(64), nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    turns = db.relationship(
        "TranslationResearchTurn",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="TranslationResearchTurn.sequence",
    )
    tool_calls = db.relationship(
        "TranslationResearchToolCall",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="TranslationResearchToolCall.call_index",
    )


class TranslationResearchTurn(db.Model):
    """Immutable ordered link from research context to exactly one attempt."""

    __tablename__ = "translation_research_turn"
    __table_args__ = (
        db.UniqueConstraint(
            "run_id", "sequence", name="uq_translation_research_turn_sequence"
        ),
        db.UniqueConstraint(
            "analysis_attempt_id", name="uq_translation_research_turn_attempt"
        ),
        db.UniqueConstraint(
            "id",
            "run_id",
            "source_plan_id",
            name="uq_translation_research_turn_id_scope",
        ),
        db.CheckConstraint(
            "sequence >= 1", name="ck_translation_research_turn_sequence"
        ),
        db.CheckConstraint(
            "length(attempt_record_hash) = 64",
            name="ck_translation_research_turn_attempt_record_hash",
        ),
        db.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_translation_research_turn_request_hash",
        ),
        db.CheckConstraint(
            "length(turn_record_hash) = 64",
            name="ck_translation_research_turn_record_hash",
        ),
        db.ForeignKeyConstraint(
            ["run_id", "source_plan_id"],
            ["translation_research_run.id", "translation_research_run.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_research_turn_run_plan",
        ),
        db.ForeignKeyConstraint(
            ["analysis_attempt_id", "source_plan_id"],
            [
                "translation_analysis_attempt.id",
                "translation_analysis_attempt.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_research_turn_attempt_plan",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_research_turn_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_research_turn_owner",
        ),
        db.Index("ix_translation_research_turn_run_sequence", "run_id", "sequence"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    operation_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    analysis_attempt_id = db.Column(db.String(36), nullable=False)
    attempt_record_hash = db.Column(db.String(64), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    turn_record_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    run = db.relationship("TranslationResearchRun", back_populates="turns")


class TranslationResearchToolCall(db.Model):
    """Prepared and durably witnessed one read-only research tool call."""

    __tablename__ = "translation_research_tool_call"
    __table_args__ = (
        db.UniqueConstraint(
            "run_id", "call_index", name="uq_translation_research_tool_call_index"
        ),
        db.UniqueConstraint(
            "turn_id",
            "turn_call_index",
            name="uq_translation_research_tool_call_turn_index",
        ),
        db.UniqueConstraint(
            "run_id",
            "provider_call_key",
            name="uq_translation_research_tool_call_provider_key",
        ),
        db.CheckConstraint(
            "call_index >= 1", name="ck_translation_research_tool_call_index"
        ),
        db.CheckConstraint(
            "turn_call_index >= 1", name="ck_translation_research_tool_call_turn_index"
        ),
        db.CheckConstraint(
            "operation_version IS NULL OR operation_version >= 1",
            name="ck_translation_research_tool_call_operation_version",
        ),
        db.CheckConstraint(
            "length(provider_call_key) = 64",
            name="ck_translation_research_tool_call_provider_key",
        ),
        db.CheckConstraint(
            "argument_state IN ('parsed', 'invalid')",
            name="ck_translation_research_tool_call_argument_state",
        ),
        db.CheckConstraint(
            "length(tool_name) <= 512",
            name="ck_translation_research_tool_call_tool_name_length",
        ),
        db.CheckConstraint(
            "length(tool_id) <= 128",
            name="ck_translation_research_tool_call_tool_id_length",
        ),
        db.CheckConstraint(
            "length(provider_id) <= 128",
            name="ck_translation_research_tool_call_provider_id_length",
        ),
        db.CheckConstraint(
            "length(addon_id) <= 128",
            name="ck_translation_research_tool_call_addon_id_length",
        ),
        db.CheckConstraint(
            "(operation_version IS NULL AND queue_job_id IS NULL AND "
            "operation_lease_expires_at IS NULL AND lease_fence IS NULL) OR "
            "(operation_version IS NOT NULL AND queue_job_id IS NOT NULL AND "
            "operation_lease_expires_at IS NOT NULL AND lease_fence IS NOT NULL)",
            name="ck_translation_research_tool_call_fence_shape",
        ),
        db.CheckConstraint(
            "(claimed_at IS NULL AND operation_version IS NULL) OR "
            "(claimed_at IS NOT NULL AND operation_version IS NOT NULL)",
            name="ck_translation_research_tool_call_claimed_fence",
        ),
        db.CheckConstraint(
            "(argument_state = 'parsed' AND argument_error_json IS NULL) OR "
            "(argument_state = 'invalid' AND argument_error_json IS NOT NULL)",
            name="ck_translation_research_tool_call_argument_evidence",
        ),
        db.CheckConstraint(
            "(state = 'prepared' AND claimed_at IS NULL AND finished_at IS NULL) OR "
            "(state = 'executing' AND claimed_at IS NOT NULL AND finished_at IS NULL) OR "
            "(state IN ('succeeded', 'failed', 'uncertain') AND claimed_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(state = 'blocked' AND finished_at IS NOT NULL) OR "
            "(state = 'cancelled' AND claimed_at IS NULL AND finished_at IS NOT NULL)",
            name="ck_translation_research_tool_call_timestamps",
        ),
        db.CheckConstraint(
            "(state IN ('prepared', 'executing') AND result_json IS NULL AND "
            "error_json IS NULL AND result_hash IS NULL AND error_hash IS NULL AND "
            "tool_message_content_json IS NULL AND tool_message_hash IS NULL) OR "
            "(state = 'succeeded' AND result_json IS NOT NULL AND error_json IS NULL AND "
            "result_hash IS NOT NULL AND error_hash IS NULL AND "
            "tool_message_content_json IS NOT NULL AND tool_message_hash IS NOT NULL) OR "
            "(state IN ('blocked', 'failed', 'uncertain', 'cancelled') AND "
            "result_json IS NULL AND error_json IS NOT NULL AND result_hash IS NULL AND "
            "error_hash IS NOT NULL AND tool_message_content_json IS NOT NULL AND "
            "tool_message_hash IS NOT NULL)",
            name="ck_translation_research_tool_call_terminal_evidence",
        ),
        db.CheckConstraint(
            "state IN ('prepared', 'executing', 'succeeded', 'blocked', 'failed', 'uncertain', 'cancelled')",
            name="ck_translation_research_tool_call_state",
        ),
        db.CheckConstraint(
            "state_version >= 1", name="ck_translation_research_tool_call_state_version"
        ),
        *[
            db.CheckConstraint(
                f"length({field}) = 64",
                name=f"ck_translation_research_tool_call_{name}",
            )
            for field, name in (
                ("source_plan_hash", "plan_hash"),
                ("lease_fence", "lease_fence"),
                ("declaration_hash", "declaration_hash"),
                ("raw_arguments_hash", "raw_arguments_hash"),
                ("arguments_hash", "arguments_hash"),
                ("call_record_hash", "record_hash"),
                ("state_head_receipt", "state_head_receipt"),
            )
        ],
        db.CheckConstraint(
            "result_hash IS NULL OR length(result_hash) = 64",
            name="ck_translation_research_tool_call_result_hash",
        ),
        db.CheckConstraint(
            "error_hash IS NULL OR length(error_hash) = 64",
            name="ck_translation_research_tool_call_error_hash",
        ),
        db.CheckConstraint(
            "tool_message_hash IS NULL OR length(tool_message_hash) = 64",
            name="ck_translation_research_tool_call_message_hash",
        ),
        db.ForeignKeyConstraint(
            ["run_id", "source_plan_id"],
            ["translation_research_run.id", "translation_research_run.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_research_tool_call_run_plan",
        ),
        db.ForeignKeyConstraint(
            ["turn_id", "run_id", "source_plan_id"],
            [
                "translation_research_turn.id",
                "translation_research_turn.run_id",
                "translation_research_turn.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_research_tool_call_turn_scope",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_research_tool_call_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_research_tool_call_owner",
        ),
        db.Index(
            "ix_translation_research_tool_call_turn_index", "turn_id", "call_index"
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = db.Column(db.String(36), nullable=False)
    turn_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    operation_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    call_index = db.Column(db.Integer, nullable=False)
    turn_call_index = db.Column(db.Integer, nullable=False)
    # Exact provider packet ids use the root policy; provider_call_key is the
    # retained-key lookup witness used for scoped uniqueness.
    provider_call_id_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    provider_call_key = db.Column(db.String(64), nullable=False)
    tool_name = db.Column(db.String(512), nullable=False)
    tool_id = db.Column(db.String(128), nullable=False)
    provider_id = db.Column(db.String(128), nullable=False)
    addon_id = db.Column(db.String(128), nullable=False)
    operation_version = db.Column(db.Integer, nullable=True)
    queue_job_id = db.Column(db.String(128), nullable=True)
    operation_lease_expires_at = db.Column(db.DateTime, nullable=True)
    lease_fence = db.Column(db.String(64), nullable=True)
    declaration_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    # Keep the provider's exact function.arguments value separate from the
    # parsed/validated arguments passed to a tool adapter.  JSON parse and
    # re-serialization are not lossless evidence operations.
    raw_arguments_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    arguments_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    argument_state = db.Column(
        db.String(16), nullable=False, default="parsed", server_default="parsed"
    )
    argument_error_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=True
    )
    declaration_hash = db.Column(db.String(64), nullable=False)
    raw_arguments_hash = db.Column(db.String(64), nullable=False)
    arguments_hash = db.Column(db.String(64), nullable=False)
    call_record_hash = db.Column(db.String(64), nullable=False)
    state = db.Column(
        db.String(32), nullable=False, default="prepared", server_default="prepared"
    )
    state_version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    state_head_receipt = db.Column(db.String(64), nullable=False)
    result_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=True)
    error_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=True)
    result_hash = db.Column(db.String(64), nullable=True)
    error_hash = db.Column(db.String(64), nullable=True)
    tool_message_content_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=True
    )
    tool_message_hash = db.Column(db.String(64), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    @property
    def provider_call_id(self) -> str:
        value = self.provider_call_id_json
        if not isinstance(value, str):
            raise TypeError("invalid provider call id evidence")
        return value

    @provider_call_id.setter
    def provider_call_id(self, value: str) -> None:
        self.provider_call_id_json = value

    run = db.relationship("TranslationResearchRun", back_populates="tool_calls")
    # The call is owned by the run; the turn link is an immutable lookup and
    # must not participate in ORM writes to the shared composite FK columns.
    turn = db.relationship("TranslationResearchTurn", viewonly=True)
    events = db.relationship(
        "TranslationResearchToolCallEvent",
        back_populates="tool_call",
        cascade="all, delete-orphan",
        order_by="TranslationResearchToolCallEvent.sequence",
    )
    result_blob = db.relationship(
        "TranslationResearchToolResultBlob",
        back_populates="tool_call",
        cascade="all, delete-orphan",
        uselist=False,
    )


class TranslationResearchToolResultBlob(db.Model):
    """Encrypted exact payload retained when a bounded envelope is insufficient."""

    __tablename__ = "translation_research_tool_result_blob"
    __table_args__ = (
        db.CheckConstraint(
            "payload_size_bytes >= 0",
            name="ck_translation_research_tool_result_blob_size",
        ),
        db.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_translation_research_tool_result_blob_payload_hash",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_research_tool_result_blob_record_hash",
        ),
        db.ForeignKeyConstraint(
            ["tool_call_id"],
            ["translation_research_tool_call.id"],
            ondelete="CASCADE",
            name="fk_translation_research_tool_result_blob_call",
        ),
        db.ForeignKeyConstraint(
            ["run_id", "source_plan_id"],
            [
                "translation_research_run.id",
                "translation_research_run.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_research_tool_result_blob_run_plan",
        ),
        db.Index(
            "ix_translation_research_tool_result_blob_run_created",
            "run_id",
            "created_at",
        ),
    )

    tool_call_id = db.Column(db.String(36), primary_key=True)
    run_id = db.Column(db.String(36), nullable=False)
    turn_id = db.Column(db.String(36), nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    operation_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    payload_size_bytes = db.Column(db.BigInteger, nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    tool_call = db.relationship(
        "TranslationResearchToolCall", back_populates="result_blob"
    )


class TranslationResearchToolCallEvent(db.Model):
    """Encrypted append-only tool-call transcript with strict state chain."""

    __tablename__ = "translation_research_tool_call_event"
    __table_args__ = (
        db.UniqueConstraint(
            "tool_call_id",
            "sequence",
            name="uq_translation_research_tool_call_event_sequence",
        ),
        db.CheckConstraint(
            "sequence >= 1", name="ck_translation_research_tool_call_event_sequence"
        ),
        db.CheckConstraint(
            "event_type IN ('prepared', 'executing', 'succeeded', 'blocked', 'failed', 'uncertain', 'cancelled')",
            name="ck_translation_research_tool_call_event_type",
        ),
        db.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_translation_research_tool_call_event_payload_hash",
        ),
        db.CheckConstraint(
            "length(event_hash) = 64",
            name="ck_translation_research_tool_call_event_hash",
        ),
        db.CheckConstraint(
            "predecessor_event_hash IS NULL OR length(predecessor_event_hash) = 64",
            name="ck_translation_research_tool_call_event_predecessor_hash",
        ),
        db.ForeignKeyConstraint(
            ["tool_call_id"],
            ["translation_research_tool_call.id"],
            ondelete="CASCADE",
            name="fk_translation_research_tool_call_event_call",
        ),
        db.Index(
            "ix_translation_research_tool_call_event_call_created",
            "tool_call_id",
            "created_at",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tool_call_id = db.Column(db.String(36), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    event_type = db.Column(db.String(32), nullable=False)
    payload_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    predecessor_event_hash = db.Column(db.String(64), nullable=True)
    event_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    tool_call = db.relationship("TranslationResearchToolCall", back_populates="events")


class TranslationMemoryEntry(db.Model):
    """Immutable translation-memory evidence with alignment witnesses."""

    __tablename__ = "translation_memory_entry"
    __table_args__ = (
        db.UniqueConstraint(
            "owner_user_id",
            "project_id",
            "derivation_key",
            name="uq_translation_memory_entry_derivation",
        ),
        db.CheckConstraint(
            "classification IN ('contextual_precedent', 'deliberate_variant')",
            name="ck_translation_memory_entry_classification",
        ),
        db.CheckConstraint(
            "granularity IN ('sentence', 'block')",
            name="ck_translation_memory_entry_granularity",
        ),
        db.CheckConstraint(
            "scope_kind = 'project'", name="ck_translation_memory_entry_project_scope"
        ),
        db.CheckConstraint(
            "scope_id IS NULL OR scope_id = project_id",
            name="ck_translation_memory_entry_scope_id",
        ),
        *[
            db.CheckConstraint(
                f"length({field}) = 64", name=f"ck_translation_memory_entry_{name}"
            )
            for field, name in (
                ("source_plan_hash", "plan_hash"),
                ("target_revision_hash", "target_revision_hash"),
                ("target_span_set_hash", "span_set_hash"),
                ("alignment_revision_hash", "alignment_hash"),
                ("structure_fingerprint", "structure_fingerprint"),
                ("source_witness_hash", "source_witness"),
                ("target_witness_hash", "target_witness"),
                ("source_evidence_hash", "source_evidence"),
                ("target_evidence_hash", "target_evidence"),
                ("group_ids_hash", "group_ids"),
                ("provenance_hash", "provenance"),
                ("record_hash", "record"),
                ("derivation_key", "derivation_key"),
            )
        ],
        db.CheckConstraint(
            "length(source_lookup_digest) BETWEEN 64 AND 160",
            name="ck_translation_memory_entry_source_lookup",
        ),
        db.CheckConstraint(
            "length(target_lookup_digest) BETWEEN 64 AND 160",
            name="ck_translation_memory_entry_target_lookup",
        ),
        db.CheckConstraint(
            "active_version >= 1", name="ck_translation_memory_entry_active_version"
        ),
        db.CheckConstraint(
            "length(active_pair_receipt) = 64",
            name="ck_translation_memory_entry_active_pair_receipt",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_memory_entry_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_memory_entry_owner",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_memory_entry_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_memory_entry_block_scope",
        ),
        db.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_memory_entry_target_revision",
        ),
        db.ForeignKeyConstraint(
            ["target_span_set_id", "source_plan_id", "block_id", "target_revision_id"],
            [
                "translation_target_span_set.id",
                "translation_target_span_set.source_plan_id",
                "translation_target_span_set.block_id",
                "translation_target_span_set.revision_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_memory_entry_span_set",
        ),
        db.ForeignKeyConstraint(
            [
                "alignment_revision_id",
                "source_plan_id",
                "block_id",
                "target_revision_id",
                "target_span_set_id",
            ],
            [
                "translation_alignment_revision.id",
                "translation_alignment_revision.source_plan_id",
                "translation_alignment_revision.block_id",
                "translation_alignment_revision.revision_id",
                "translation_alignment_revision.span_set_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_memory_entry_alignment",
        ),
        db.Index(
            "ix_translation_memory_entry_lookup",
            "owner_user_id",
            "project_id",
            "source_lookup_digest",
            "source_language",
            "target_language",
        ),
        db.Index(
            "ix_translation_memory_entry_parent",
            "source_plan_id",
            "block_id",
            "alignment_revision_id",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = db.Column(db.Integer, nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    block_id = db.Column(db.String(128), nullable=False)
    target_revision_id = db.Column(db.String(36), nullable=False)
    target_span_set_id = db.Column(db.String(36), nullable=False)
    alignment_revision_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    target_revision_hash = db.Column(db.String(64), nullable=False)
    target_span_set_hash = db.Column(db.String(64), nullable=False)
    alignment_revision_hash = db.Column(db.String(64), nullable=False)
    active_version = db.Column(db.Integer, nullable=False)
    active_pair_receipt = db.Column(db.String(64), nullable=False)
    target_activation_at = db.Column(db.DateTime, nullable=False)
    alignment_activation_at = db.Column(db.DateTime, nullable=False)
    source_language = db.Column(db.String(64), nullable=False)
    target_language = db.Column(db.String(64), nullable=False)
    semantic_role = db.Column(db.String(64), nullable=False)
    structure_fingerprint = db.Column(db.String(64), nullable=False)
    classification = db.Column(db.String(32), nullable=False)
    granularity = db.Column(db.String(16), nullable=False)
    scope_kind = db.Column(
        db.String(16), nullable=False, default="project", server_default="project"
    )
    scope_id = db.Column(db.String(36), nullable=True)
    source_lookup_digest = db.Column(db.String(160), nullable=False)
    target_lookup_digest = db.Column(db.String(160), nullable=False)
    derivation_key = db.Column(db.String(64), nullable=False)
    source_witness_hash = db.Column(db.String(64), nullable=False)
    target_witness_hash = db.Column(db.String(64), nullable=False)
    source_evidence_hash = db.Column(db.String(64), nullable=False)
    target_evidence_hash = db.Column(db.String(64), nullable=False)
    group_ids_hash = db.Column(db.String(64), nullable=False)
    provenance_hash = db.Column(db.String(64), nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    source_text = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    target_text = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    source_evidence_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    target_evidence_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    alignment_group_ids_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    provenance_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    project = db.relationship("TranslationProject", viewonly=True)
    owner = db.relationship("User", foreign_keys=[owner_user_id], viewonly=True)
    source_plan = db.relationship("TranslationSourcePlan", viewonly=True)


class TranslationEmbeddingGeneration(db.Model):
    """Immutable, replaceable dense-vector projection for one source plan."""

    __tablename__ = "translation_embedding_generation"
    __table_args__ = (
        db.UniqueConstraint(
            "namespace", name="uq_translation_embedding_generation_namespace"
        ),
        db.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            name="uq_translation_embedding_generation_id_scope",
        ),
        db.CheckConstraint(
            "projection_kind IN ('block', 'anchor', 'memory')",
            name="ck_translation_embedding_generation_kind",
        ),
        db.CheckConstraint(
            "status IN ('building', 'ready', 'active', 'failed', 'retired')",
            name="ck_translation_embedding_generation_status",
        ),
        db.CheckConstraint(
            "dimensions > 0", name="ck_translation_embedding_generation_dimensions"
        ),
        db.CheckConstraint(
            "entry_count >= 0", name="ck_translation_embedding_generation_entry_count"
        ),
        db.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_embedding_generation_plan_hash",
        ),
        db.CheckConstraint(
            "length(signature_hash) = 64",
            name="ck_translation_embedding_generation_signature_hash",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_embedding_generation_record_hash",
        ),
        db.CheckConstraint(
            "length(credential_witness) = 64",
            name="ck_translation_embedding_generation_credential_witness",
        ),
        db.CheckConstraint(
            "length(state_receipt) = 64",
            name="ck_translation_embedding_generation_state_receipt",
        ),
        db.CheckConstraint(
            "length(vector_witness) = 64",
            name="ck_translation_embedding_generation_vector_witness",
        ),
        db.CheckConstraint(
            "state_version >= 1",
            name="ck_translation_embedding_generation_state_version",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_generation_project",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_generation_owner",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_generation_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["model_record_id"],
            ["llm_models.id"],
            ondelete="RESTRICT",
            name="fk_translation_embedding_generation_model",
        ),
        db.ForeignKeyConstraint(
            ["provider_id"],
            ["llm_providers.id"],
            ondelete="RESTRICT",
            name="fk_translation_embedding_generation_provider",
        ),
        db.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            ondelete="RESTRICT",
            name="fk_translation_embedding_generation_api_key",
        ),
        db.ForeignKeyConstraint(
            ["previous_generation_id"],
            ["translation_embedding_generation.id"],
            ondelete="SET NULL",
            name="fk_translation_embedding_generation_previous",
        ),
        db.Index(
            "ix_translation_embedding_generation_scope",
            "owner_user_id",
            "project_id",
            "source_plan_id",
            "projection_kind",
            "status",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = db.Column(db.Integer, nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    projection_kind = db.Column(db.String(16), nullable=False)
    model_record_id = db.Column(db.Integer, nullable=False)
    model_owner_user_id = db.Column(db.Integer, nullable=True)
    provider_id = db.Column(db.Integer, nullable=False)
    provider_name = db.Column(db.String(100), nullable=False)
    provider_kind = db.Column(db.String(50), nullable=False)
    api_key_id = db.Column(db.Integer, nullable=False)
    api_key_owner_user_id = db.Column(db.Integer, nullable=True)
    api_key_created_at = db.Column(db.DateTime, nullable=False)
    credential_witness = db.Column(db.String(64), nullable=False)
    model_id = db.Column(db.String(255), nullable=False)
    model_revision = db.Column(db.String(255), nullable=False)
    model_updated_at = db.Column(db.DateTime, nullable=True)
    dimensions = db.Column(db.Integer, nullable=False)
    task_type = db.Column(db.String(64), nullable=False)
    normalization = db.Column(db.String(64), nullable=False)
    distance_metric = db.Column(db.String(64), nullable=False)
    prefix_style = db.Column(db.String(64), nullable=False)
    query_prefix = db.Column(db.String(4096), nullable=True)
    passage_prefix = db.Column(db.String(4096), nullable=True)
    extractor_version = db.Column(db.String(128), nullable=False)
    profile_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=False)
    signature_json = db.Column(
        ContentType(db.JSON, strategy="system"), nullable=False
    )
    signature_hash = db.Column(db.String(64), nullable=False)
    namespace = db.Column(db.String(128), nullable=False)
    status = db.Column(
        db.String(16), nullable=False, default="building", server_default="building"
    )
    entry_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    health_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=True)
    error_json = db.Column(ContentType(db.JSON, strategy="system"), nullable=True)
    previous_generation_id = db.Column(db.String(36), nullable=True)
    record_hash = db.Column(db.String(64), nullable=False)
    state_version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    state_receipt = db.Column(db.String(64), nullable=False)
    vector_witness = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    prepared_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    activated_at = db.Column(db.DateTime, nullable=True)
    retired_at = db.Column(db.DateTime, nullable=True)

    project = db.relationship("TranslationProject", viewonly=True)
    owner = db.relationship("User", foreign_keys=[owner_user_id], viewonly=True)
    model = db.relationship("LLMModel", foreign_keys=[model_record_id], viewonly=True)
    provider = db.relationship("LLMProvider", foreign_keys=[provider_id], viewonly=True)
    previous_generation = db.relationship(
        "TranslationEmbeddingGeneration",
        remote_side=[id],
        foreign_keys=[previous_generation_id],
        viewonly=True,
    )
    entries = db.relationship(
        "TranslationEmbeddingEntry",
        back_populates="generation",
        cascade="all, delete-orphan",
        order_by="TranslationEmbeddingEntry.ordinal",
    )


class TranslationEmbeddingEntry(db.Model):
    """One policy-encoded subject mapped to one row in a generation namespace."""

    __tablename__ = "translation_embedding_entry"
    __table_args__ = (
        db.UniqueConstraint(
            "generation_id",
            "ordinal",
            name="uq_translation_embedding_entry_ordinal",
        ),
        db.UniqueConstraint(
            "generation_id",
            "subject_type",
            "subject_id",
            name="uq_translation_embedding_entry_subject",
        ),
        db.UniqueConstraint(
            "generation_id",
            "vector_row_id",
            name="uq_translation_embedding_entry_vector_row",
        ),
        db.CheckConstraint(
            "subject_type IN ('block', 'anchor', 'memory')",
            name="ck_translation_embedding_entry_subject_type",
        ),
        db.CheckConstraint(
            "ordinal >= 0", name="ck_translation_embedding_entry_ordinal"
        ),
        db.CheckConstraint(
            "vector_row_id > 0",
            name="ck_translation_embedding_entry_vector_row_positive",
        ),
        db.CheckConstraint(
            "length(source_witness_hash) = 64",
            name="ck_translation_embedding_entry_source_witness",
        ),
        db.CheckConstraint(
            "length(subject_receipt) = 64",
            name="ck_translation_embedding_entry_subject_receipt",
        ),
        db.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_embedding_entry_record_hash",
        ),
        db.ForeignKeyConstraint(
            ["generation_id"],
            ["translation_embedding_generation.id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_entry_generation",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_entry_owner",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_entry_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_entry_plan_scope",
        ),
        db.Index(
            "ix_translation_embedding_entry_scope",
            "owner_user_id",
            "project_id",
            "source_plan_id",
            "subject_type",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    generation_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    subject_type = db.Column(db.String(16), nullable=False)
    subject_id = db.Column(db.String(128), nullable=False)
    ordinal = db.Column(db.Integer, nullable=False)
    vector_row_id = db.Column(db.Integer, nullable=False)
    source_text = db.Column(ContentType(db.Text, strategy="system"), nullable=False)
    source_witness_hash = db.Column(db.String(64), nullable=False)
    subject_receipt = db.Column(db.String(64), nullable=False)
    record_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    generation = db.relationship(
        "TranslationEmbeddingGeneration", back_populates="entries"
    )
    owner = db.relationship("User", foreign_keys=[owner_user_id], viewonly=True)
    project = db.relationship("TranslationProject", viewonly=True)
    source_plan = db.relationship("TranslationSourcePlan", viewonly=True)


class TranslationEmbeddingActive(db.Model):
    """CAS-switched active generation pointer for one Translator projection."""

    __tablename__ = "translation_embedding_active"
    __table_args__ = (
        db.PrimaryKeyConstraint(
            "owner_user_id",
            "project_id",
            "source_plan_id",
            "projection_kind",
            name="pk_translation_embedding_active",
        ),
        db.CheckConstraint(
            "projection_kind IN ('block', 'anchor', 'memory')",
            name="ck_translation_embedding_active_kind",
        ),
        db.CheckConstraint(
            "length(generation_record_hash) = 64",
            name="ck_translation_embedding_active_generation_hash",
        ),
        db.CheckConstraint(
            "length(generation_state_receipt) = 64",
            name="ck_translation_embedding_active_generation_state_receipt",
        ),
        db.CheckConstraint(
            "length(pointer_receipt) = 64",
            name="ck_translation_embedding_active_pointer_receipt",
        ),
        db.CheckConstraint(
            "version >= 1", name="ck_translation_embedding_active_version"
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_active_owner",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_active_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_embedding_active_plan_scope",
        ),
        db.ForeignKeyConstraint(
            ["generation_id", "project_id", "source_plan_id"],
            [
                "translation_embedding_generation.id",
                "translation_embedding_generation.project_id",
                "translation_embedding_generation.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_embedding_active_generation_scope",
        ),
    )

    owner_user_id = db.Column(db.Integer, nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    projection_kind = db.Column(db.String(16), nullable=False)
    generation_id = db.Column(db.String(36), nullable=False)
    generation_record_hash = db.Column(db.String(64), nullable=False)
    generation_state_receipt = db.Column(db.String(64), nullable=False)
    pointer_receipt = db.Column(db.String(64), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    updated_at = db.Column(
        db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False
    )

    generation = db.relationship("TranslationEmbeddingGeneration", viewonly=True)


class TranslationBlockWorkflowConfig(db.Model):
    """Immutable, operation-bound settings for the document-block engine.

    The encrypted JSON is the canonical workflow input.  The public hash is a
    deterministic identity for idempotency; ``config_receipt`` is the retained
    installation-key witness that authenticates the decrypted JSON and its
    owner/source scope.
    """

    __tablename__ = "translation_block_workflow_config"
    __table_args__ = (
        db.UniqueConstraint(
            "operation_id", name="uq_translation_block_workflow_config_operation"
        ),
        db.UniqueConstraint(
            "id",
            "project_id",
            name="uq_translation_block_workflow_config_id_project",
        ),
        db.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_block_workflow_config_plan_hash",
        ),
        db.CheckConstraint(
            "length(source_content_hash) = 64",
            name="ck_translation_block_workflow_config_content_hash",
        ),
        db.CheckConstraint(
            "length(config_hash) = 64",
            name="ck_translation_block_workflow_config_hash",
        ),
        db.CheckConstraint(
            "length(config_receipt) = 64",
            name="ck_translation_block_workflow_config_receipt",
        ),
        db.ForeignKeyConstraint(
            ["operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="CASCADE",
            name="fk_translation_block_workflow_config_operation_scope",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_block_workflow_config_owner",
        ),
        db.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_block_workflow_config_project",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_block_workflow_config_plan_scope",
        ),
        db.Index(
            "ix_translation_block_workflow_config_scope",
            "owner_user_id",
            "project_id",
            "source_plan_id",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    source_content_hash = db.Column(db.String(64), nullable=False)
    config_hash = db.Column(db.String(64), nullable=False)
    config_json = db.Column(EncryptedType(db.JSON, strategy="system"), nullable=False)
    config_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    operation = db.relationship("TranslationOperation", viewonly=True)
    owner = db.relationship("User", foreign_keys=[owner_user_id], viewonly=True)
    project = db.relationship("TranslationProject", viewonly=True)
    source_plan = db.relationship("TranslationSourcePlan", viewonly=True)


class TranslationOperationEvidenceReuse(db.Model):
    """Authenticated cross-operation selection of immutable phase evidence."""

    __tablename__ = "translation_operation_evidence_reuse"
    __table_args__ = (
        db.UniqueConstraint(
            "operation_id", name="uq_translation_operation_evidence_reuse_operation"
        ),
        db.CheckConstraint(
            "contract_version = 'translator-operation-evidence-reuse-v1'",
            name="ck_translation_operation_evidence_reuse_contract",
        ),
        db.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_operation_evidence_reuse_plan_hash",
        ),
        db.CheckConstraint(
            "length(source_content_hash) = 64",
            name="ck_translation_operation_evidence_reuse_content_hash",
        ),
        db.CheckConstraint(
            "length(workflow_config_hash) = 64",
            name="ck_translation_operation_evidence_reuse_workflow_hash",
        ),
        db.CheckConstraint(
            "length(origin_workflow_config_hash) = 64",
            name="ck_translation_operation_evidence_reuse_origin_workflow_hash",
        ),
        db.CheckConstraint(
            "length(document_brief_revision_hash) = 64",
            name="ck_translation_operation_evidence_reuse_brief_hash",
        ),
        db.CheckConstraint(
            "length(style_guide_revision_hash) = 64",
            name="ck_translation_operation_evidence_reuse_style_hash",
        ),
        db.CheckConstraint(
            "length(entity_register_revision_hash) = 64",
            name="ck_translation_operation_evidence_reuse_entity_hash",
        ),
        db.CheckConstraint(
            "(research_run_id IS NULL AND research_run_record_hash IS NULL) OR "
            "(research_run_id IS NOT NULL AND length(research_run_record_hash) = 64)",
            name="ck_translation_operation_evidence_reuse_research",
        ),
        db.CheckConstraint(
            "length(selection_hash) = 64",
            name="ck_translation_operation_evidence_reuse_selection_hash",
        ),
        db.CheckConstraint(
            "length(selection_receipt) = 64",
            name="ck_translation_operation_evidence_reuse_selection_receipt",
        ),
        db.ForeignKeyConstraint(
            ["operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="CASCADE",
            name="fk_translation_operation_evidence_reuse_operation",
        ),
        db.ForeignKeyConstraint(
            ["origin_operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_origin_operation",
        ),
        db.ForeignKeyConstraint(
            ["workflow_config_id", "project_id"],
            [
                "translation_block_workflow_config.id",
                "translation_block_workflow_config.project_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_workflow",
        ),
        db.ForeignKeyConstraint(
            ["origin_workflow_config_id", "project_id"],
            [
                "translation_block_workflow_config.id",
                "translation_block_workflow_config.project_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_origin_workflow",
        ),
        db.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_operation_evidence_reuse_owner",
        ),
        db.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_plan",
        ),
        *[
            db.ForeignKeyConstraint(
                [column, "project_id", "source_plan_id"],
                [
                    "translation_context_artifact_revision.id",
                    "translation_context_artifact_revision.project_id",
                    "translation_context_artifact_revision.source_plan_id",
                ],
                ondelete="RESTRICT",
                name=f"fk_translation_operation_evidence_reuse_{name}",
            )
            for column, name in (
                ("document_brief_revision_id", "brief"),
                ("style_guide_revision_id", "style"),
                ("entity_register_revision_id", "entity"),
            )
        ],
        db.ForeignKeyConstraint(
            ["research_run_id", "source_plan_id"],
            ["translation_research_run.id", "translation_research_run.source_plan_id"],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_research",
        ),
        db.Index(
            "ix_translation_operation_evidence_reuse_scope",
            "owner_user_id",
            "project_id",
            "source_plan_id",
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    contract_version = db.Column(
        db.String(64),
        nullable=False,
        default="translator-operation-evidence-reuse-v1",
        server_default="translator-operation-evidence-reuse-v1",
    )
    operation_id = db.Column(db.String(36), nullable=False)
    origin_operation_id = db.Column(db.String(36), nullable=False)
    owner_user_id = db.Column(db.Integer, nullable=False)
    project_id = db.Column(db.String(36), nullable=False)
    source_plan_id = db.Column(db.String(36), nullable=False)
    source_plan_hash = db.Column(db.String(64), nullable=False)
    source_content_hash = db.Column(db.String(64), nullable=False)
    workflow_config_id = db.Column(db.String(36), nullable=False)
    workflow_config_hash = db.Column(db.String(64), nullable=False)
    origin_workflow_config_id = db.Column(db.String(36), nullable=False)
    origin_workflow_config_hash = db.Column(db.String(64), nullable=False)
    document_brief_revision_id = db.Column(db.String(36), nullable=False)
    document_brief_revision_hash = db.Column(db.String(64), nullable=False)
    style_guide_revision_id = db.Column(db.String(36), nullable=False)
    style_guide_revision_hash = db.Column(db.String(64), nullable=False)
    entity_register_revision_id = db.Column(db.String(36), nullable=False)
    entity_register_revision_hash = db.Column(db.String(64), nullable=False)
    research_run_id = db.Column(db.String(36), nullable=True)
    research_run_record_hash = db.Column(db.String(64), nullable=True)
    selection_json = db.Column(
        EncryptedType(db.JSON, strategy="system"), nullable=False
    )
    selection_hash = db.Column(db.String(64), nullable=False)
    selection_receipt = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    operation = db.relationship(
        "TranslationOperation", foreign_keys=[operation_id], viewonly=True
    )
    origin_operation = db.relationship(
        "TranslationOperation", foreign_keys=[origin_operation_id], viewonly=True
    )
    workflow_config = db.relationship(
        "TranslationBlockWorkflowConfig",
        foreign_keys=[workflow_config_id],
        viewonly=True,
    )
    origin_workflow_config = db.relationship(
        "TranslationBlockWorkflowConfig",
        foreign_keys=[origin_workflow_config_id],
        viewonly=True,
    )
    research_run = db.relationship("TranslationResearchRun", viewonly=True)


class TranslationEpubEdition(db.Model):
    """Owner-scoped EPUB recipe bound to one active translation plan."""

    __tablename__ = "translation_epub_edition"
    __table_args__ = (
        db.CheckConstraint("version >= 1", name="ck_translation_epub_edition_version"),
        db.CheckConstraint(
            "source_revision IS NULL OR length(source_revision) = 64",
            name="ck_translation_epub_edition_source_revision",
        ),
    )

    project_id = db.Column(
        db.String(36),
        db.ForeignKey("translation_project.id", ondelete="CASCADE"),
        primary_key=True,
    )
    owner_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = db.Column(db.Integer, nullable=False)
    source_revision = db.Column(db.String(64), nullable=True)
    recipe = db.Column(ContentType(db.JSON, strategy="system"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now_db)

    project = db.relationship("TranslationProject", back_populates="epub_edition")
