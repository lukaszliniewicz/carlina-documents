import uuid
from datetime import UTC, datetime
from typing import Any

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from carlina.core.utc import utc_now_db
from iarf_assistant.database.types import ContentType, EncryptedType
from iarf_assistant.extensions import db


class Setting(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    # Historical rows may be plaintext. EncryptedType reads those rows for a
    # non-destructive cutover, while every new or changed setting is written as
    # an authenticated installation-key envelope. This protects both add-on
    # client secrets and core settings that may contain private organization
    # context without changing the physical TEXT schema.
    value = db.Column(EncryptedType(db.Text, strategy="system"), nullable=True)

class Addon(db.Model):
    """Represents the state of an addon discovered on the filesystem."""
    __tablename__ = 'addons'
    id = db.Column(db.String(100), primary_key=True) # Corresponds to manifest id
    version = db.Column(db.String(50), nullable=False)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    alembic_revision = db.Column(db.String(32), nullable=True)
    source_kind = db.Column(
        db.String(32),
        nullable=False,
        default="built_in",
        server_default="built_in",
        index=True,
    )
    trust_tier = db.Column(
        db.String(40),
        nullable=False,
        default="built_in_trusted",
        server_default="built_in_trusted",
        index=True,
    )
    lifecycle_state = db.Column(
        db.String(32),
        nullable=False,
        default="discovered",
        server_default="discovered",
        index=True,
    )
    lifecycle_generation = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    lifecycle_error = db.Column(db.Text, nullable=True)
    manifest_sha256 = db.Column(db.String(64), nullable=True)
    contract_sha256 = db.Column(db.String(64), nullable=True)
    resolved_contract_json = db.Column(db.JSON, nullable=True)
    provenance_json = db.Column(db.JSON, nullable=True)
    code_path = db.Column(db.Text, nullable=True)
    installed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    lifecycle_updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "version": self.version,
            "is_active": self.is_active,
            "alembic_revision": self.alembic_revision,
            "source_kind": self.source_kind,
            "trust_tier": self.trust_tier,
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_generation": self.lifecycle_generation,
            "lifecycle_error": self.lifecycle_error,
            "manifest_sha256": self.manifest_sha256,
            "contract_sha256": self.contract_sha256,
            "provenance": self.provenance_json or {},
            "code_path": self.code_path,
            "installed_at": self.installed_at.isoformat() if self.installed_at else None,
            "lifecycle_updated_at": (
                self.lifecycle_updated_at.isoformat()
                if self.lifecycle_updated_at
                else None
            ),
        }


class AddonLifecycleEvent(db.Model):
    """Append-only journal of add-on lifecycle decisions and failures."""

    __tablename__ = "addon_lifecycle_events"
    id = db.Column(db.Integer, primary_key=True)
    addon_id = db.Column(
        db.String(100),
        db.ForeignKey("addons.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_state = db.Column(db.String(32), nullable=False)
    to_state = db.Column(db.String(32), nullable=False)
    event = db.Column(db.String(80), nullable=False)
    success = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    detail = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.JSON, nullable=True)
    actor_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    generation = db.Column(db.Integer, nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    __table_args__ = (
        db.Index("ix_addon_lifecycle_events_addon_id_id", "addon_id", "id"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "addon_id": self.addon_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "event": self.event,
            "success": self.success,
            "detail": self.detail,
            "metadata": self.metadata_json or {},
            "actor_user_id": self.actor_user_id,
            "generation": self.generation,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False, server_default='0')
    addon_settings = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    assistant_instructions = db.Column(db.Text, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    __table_args__ = (db.UniqueConstraint('email', name='uq_user_email'),)

    def to_dict(self):
        """Returns a dictionary representation of the user, excluding sensitive info."""
        return {
            'id': self.id,
            'email': self.email,
            'is_admin': self.is_admin,
            'assistant_instructions': self.assistant_instructions,
            'addon_settings': self.addon_settings or {}
        }

class CompassClientDevice(db.Model):
    """An owner-approved native Compass client device.

    This core-owned record lets the bearer gateway reject a revoked device
    before dispatch reaches the Compass add-on. Its display name is encrypted
    personal metadata; the compact lifecycle fields stay operational.
    """

    __tablename__ = "compass_client_device"

    device_id = db.Column(db.String(36), primary_key=True)
    owner_user_id = db.Column(db.Integer, nullable=False)
    display_name = db.Column(
        EncryptedType(db.String(96), strategy="user"), nullable=False
    )
    platform = db.Column(db.String(32), nullable=False)
    scope = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="active")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["owner_user_id"], ["user.id"], ondelete="CASCADE",
            name="fk_compass_client_device_owner",
        ),
        db.CheckConstraint("scope = 'compass.capture'", name="ck_compass_client_device_scope"),
        db.CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_compass_client_device_status"
        ),
        db.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) "
            "OR (status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_compass_client_device_revocation",
        ),
        db.Index("ix_compass_client_device_owner_created", "owner_user_id", "created_at"),
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class CompassClientAuthorizationCode(db.Model):
    """One-time PKCE code; only a keyed digest is retained."""

    __tablename__ = "compass_client_authorization_code"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code_hash = db.Column(db.String(64), nullable=False, unique=True)
    device_id = db.Column(db.String(36), nullable=False)
    redirect_uri = db.Column(db.String(512), nullable=False)
    code_challenge = db.Column(db.String(128), nullable=False)
    scope = db.Column(db.String(32), nullable=False)
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    consumed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["device_id"], ["compass_client_device.device_id"], ondelete="CASCADE",
            name="fk_compass_client_authorization_code_device",
        ),
        db.CheckConstraint(
            "scope = 'compass.capture'", name="ck_compass_client_authorization_code_scope"
        ),
        db.CheckConstraint(
            "expires_at > issued_at", name="ck_compass_client_authorization_code_expiry"
        ),
        db.Index("ix_compass_client_authorization_code_expiry", "expires_at"),
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class CompassClientAccessToken(db.Model):
    """Short-lived opaque bearer token represented exclusively by a digest."""

    __tablename__ = "compass_client_access_token"

    token_hash = db.Column(db.String(64), primary_key=True)
    device_id = db.Column(db.String(36), nullable=False)
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["device_id"], ["compass_client_device.device_id"], ondelete="CASCADE",
            name="fk_compass_client_access_token_device",
        ),
        db.CheckConstraint(
            "expires_at > issued_at", name="ck_compass_client_access_token_expiry"
        ),
        db.Index("ix_compass_client_access_token_expiry", "expires_at"),
        db.Index("ix_compass_client_access_token_device", "device_id"),
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class CompassClientRefreshToken(db.Model):
    """Rotating opaque refresh token represented exclusively by a digest."""

    __tablename__ = "compass_client_refresh_token"

    token_hash = db.Column(db.String(64), primary_key=True)
    device_id = db.Column(db.String(36), nullable=False)
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    replaced_by_hash = db.Column(db.String(64), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["device_id"], ["compass_client_device.device_id"], ondelete="CASCADE",
            name="fk_compass_client_refresh_token_device",
        ),
        db.CheckConstraint(
            "expires_at > issued_at", name="ck_compass_client_refresh_token_expiry"
        ),
        db.CheckConstraint(
            "(used_at IS NULL AND replaced_by_hash IS NULL) "
            "OR (used_at IS NOT NULL AND replaced_by_hash IS NOT NULL)",
            name="ck_compass_client_refresh_token_rotation",
        ),
        db.Index("ix_compass_client_refresh_token_device", "device_id"),
        db.Index("ix_compass_client_refresh_token_expiry", "expires_at"),
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class CompassClientAuthEvent(db.Model):
    """Durable authorization audit context without raw credential material."""

    __tablename__ = "compass_client_auth_event"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    owner_user_id = db.Column(db.Integer, nullable=False)
    device_id = db.Column(db.String(36), nullable=False)
    event = db.Column(db.String(32), nullable=False)
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["owner_user_id"], ["user.id"], ondelete="CASCADE",
            name="fk_compass_client_auth_event_owner",
        ),
        db.ForeignKeyConstraint(
            ["device_id"], ["compass_client_device.device_id"], ondelete="CASCADE",
            name="fk_compass_client_auth_event_device",
        ),
        db.CheckConstraint(
            "event IN ('approved', 'code_exchanged', 'refresh_rotated', "
            "'refresh_reuse_revoked', 'revoked')",
            name="ck_compass_client_auth_event_event",
        ),
        db.Index("ix_compass_client_auth_event_owner_occurred", "owner_user_id", "occurred_at"),
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class LLMProvider(db.Model):
    """Represents an LLM provider configuration."""
    __tablename__ = 'llm_providers'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    name = db.Column(db.String(100), nullable=False)
    display_name = db.Column(db.String(100), nullable=True)
    provider_kind = db.Column(db.String(50), default='native', nullable=False, server_default='native')
    discovery_type = db.Column(db.String(50), default='automatic', nullable=False) # 'automatic' or 'manual'
    supports_custom_models = db.Column(db.Boolean, default=False, nullable=False)
    requires_api_base = db.Column(db.Boolean, default=False, nullable=False)
    models = db.relationship('LLMModel', back_populates='provider', cascade="all, delete-orphan")

    user = db.relationship('User', backref=db.backref('llm_providers', lazy=True, cascade="all, delete-orphan"))

    __table_args__ = (
        db.UniqueConstraint('name', name='uq_llm_providers_name'),
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_llm_provider_user_id_user'),
    )

    def to_dict(self):
        display_name = self.display_name or self.name.replace('_', ' ').title()
        return {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'display_name': display_name,
            'provider_kind': self.provider_kind,
            'discovery_type': self.discovery_type,
            'supports_custom_models': self.supports_custom_models,
            'requires_api_base': self.requires_api_base
        }

class ApiKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True) # Null for global (admin-set) keys
    provider_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    api_key = db.Column(EncryptedType(db.String(512), strategy='system'), nullable=False)
    api_base = db.Column(EncryptedType(db.String(512), strategy='system'), nullable=True) # For providers like LM Studio
    api_version = db.Column(db.String(100), nullable=True) # For Azure OpenAI
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db)

    user = db.relationship('User', backref=db.backref('api_keys', lazy=True, cascade="all, delete-orphan"))
    provider = db.relationship('LLMProvider', backref=db.backref('api_keys', lazy=True))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'provider_id', 'name', name='uq_api_key_user_id_provider_id_name'),
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_api_key_user_id_user'),
        db.ForeignKeyConstraint(['provider_id'], ['llm_providers.id'], name='fk_api_key_provider_id_llm_providers'),
    )

    def to_dict(self):
        """Returns a dictionary representation of the API key, excluding the key itself."""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'provider_id': self.provider_id,
            'provider_name': self.provider.name if self.provider else None,
            'name': self.name,
            'api_base': self.api_base,
            'api_version': self.api_version,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class LLMModel(db.Model):
    """Represents a Large Language Model available in the system."""
    __tablename__ = 'llm_models'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True) # Null for global (admin-set) models
    provider_id = db.Column(db.Integer, nullable=False)
    model_id = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    manually_added = db.Column(db.Boolean, default=False, nullable=False)
    
    # Metadata from LiteLLM
    context_window = db.Column(db.Integer, nullable=True)
    max_output_tokens = db.Column(db.Integer, nullable=True)
    capability_overrides = db.Column(db.JSON, nullable=True)
    supports_vision = db.Column(db.Boolean, default=False, nullable=False)
    supports_pdf = db.Column(db.Boolean, default=False, nullable=False, server_default='0')
    supports_reasoning = db.Column(db.Boolean, default=False, nullable=False, server_default='0')
    supports_audio_in = db.Column(db.Boolean, default=False, nullable=False, server_default='0')
    supported_reasoning_levels = db.Column(db.String(255), nullable=True)
    input_cost_per_million_tokens = db.Column(db.Float, nullable=True)
    input_cost_per_million_cached_tokens = db.Column(db.Float, nullable=True)
    output_cost_per_million_tokens = db.Column(db.Float, nullable=True)

    created_at = db.Column(db.DateTime, default=utc_now_db)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db)
    
    # Custom Embedding configuration fields
    model_type = db.Column(db.String(50), default='chat', nullable=False, server_default='chat')
    default_dimensions = db.Column(db.Integer, nullable=True)
    supports_mrl = db.Column(db.Boolean, default=False, nullable=False, server_default='0')
    prefix_style = db.Column(db.String(50), default='none', nullable=False, server_default='none')
    custom_query_prefix = db.Column(db.String(255), nullable=True)
    custom_passage_prefix = db.Column(db.String(255), nullable=True)

    provider = db.relationship('LLMProvider', back_populates='models')
    user = db.relationship('User', backref=db.backref('llm_models', lazy=True, cascade="all, delete-orphan"))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'model_id', name='uq_llm_models_user_id_model_id'),
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_llm_models_user_id_user'),
        db.ForeignKeyConstraint(['provider_id'], ['llm_providers.id'], name='fk_llm_models_provider_id_llm_providers'),
    )

    def to_dict(self):
        return {
            'db_id': self.id,
            'id': self.model_id,
            'name': self.display_name,
            'provider': self.provider.name,
            'is_active': self.is_active,
            'context_window': self.context_window,
            'max_output_tokens': self.max_output_tokens,
            'capability_overrides': self.capability_overrides or {},
            'supports_vision': self.supports_vision,
            'supports_pdf': self.supports_pdf,
            'supports_reasoning': self.supports_reasoning,
            'supports_audio_in': self.supports_audio_in,
            'supported_reasoning_levels': self.supported_reasoning_levels,
            'input_cost': self.input_cost_per_million_tokens,
            'input_cached_cost': self.input_cost_per_million_cached_tokens,
            'output_cost': self.output_cost_per_million_tokens,
            'manually_added': self.manually_added,
            'user_id': self.user_id,
            'model_type': self.model_type,
            'default_dimensions': self.default_dimensions,
            'supports_mrl': self.supports_mrl,
            'prefix_style': self.prefix_style,
            'custom_query_prefix': self.custom_query_prefix,
            'custom_passage_prefix': self.custom_passage_prefix
        }

# Association table for the many-to-many relationship
data_context_collection_association = db.Table('data_context_collection_association',
    db.Column('collection_id', db.Integer, db.ForeignKey('data_context_collection.id'), primary_key=True),
    db.Column('context_id', db.Integer, db.ForeignKey('data_context.id'), primary_key=True)
)

class DataContextCollection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db)
    source_type = db.Column(db.String(50), nullable=True)  # e.g., 'gmail', 'document_upload'
    collection_mode = db.Column(db.String(50), nullable=True) # e.g., 'vector', 'regular'
    source_metadata = db.Column(db.JSON, nullable=True)  # e.g., {'query': '...'}
    last_synced_at = db.Column(db.DateTime, nullable=True)
    storage_path = db.Column(db.String(255), nullable=True) # Relative to uploads folder

    user = db.relationship('User', backref=db.backref('data_context_collections', lazy=True, cascade="all, delete-orphan"))
    items = db.relationship('DataContext', secondary=data_context_collection_association, lazy='subquery',
                            backref=db.backref('collections', lazy=True))

    __table_args__ = (db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_data_context_collection_user_id_user'),)

    def to_dict(self):
        canonical_item_count = len(getattr(self, 'knowledge_memberships', []) or [])
        legacy_item_count = len(self.items)
        saved_view = getattr(self, 'knowledge_saved_view', None)
        logical_item_count = (
            int(saved_view.result_count or 0)
            if saved_view is not None and saved_view.refresh_status == 'ready'
            else canonical_item_count
        )
        return {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'description': self.description,
            # Once canonical memberships exist they are authoritative. The
            # separate count keeps legacy migration progress inspectable
            # without double-counting the same logical source.
            'item_count': logical_item_count or legacy_item_count,
            'canonical_item_count': canonical_item_count,
            'logical_item_count': logical_item_count,
            'legacy_item_count': legacy_item_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'source_type': self.source_type,
            'collection_mode': self.collection_mode,
            'source_metadata': self.source_metadata,
            'last_synced_at': self.last_synced_at.isoformat() if self.last_synced_at else None,
            'storage_path': self.storage_path
        }

class DataContext(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    source_type = db.Column(db.String(50), nullable=False)  # e.g., 'gmail', 'file'
    source_id = db.Column(db.String(255), nullable=True)  # e.g., gmail thread_id, file path
    # Legacy rows may still be plaintext. EncryptedType reads those rows through
    # its compatibility fallback while ensuring all new/updated content is
    # encrypted at rest.
    content = db.Column(EncryptedType(db.Text, strategy='user'), nullable=False)
    json_metadata = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)  # e.g., {'sender': '...', 'subject': '...'}
    embedding = db.Column(db.LargeBinary, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db)

    user = db.relationship('User', backref=db.backref('data_contexts', lazy=True))
    chunks = db.relationship('DataContextChunk', backref='data_context', lazy=True, cascade="all, delete-orphan")

    __table_args__ = (db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_data_context_user_id_user'),)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'source_type': self.source_type,
            'source_id': self.source_id,
            'content_snippet': self.content[:100] + '...' if self.content and len(self.content) > 100 else self.content,
            'metadata': self.json_metadata,
            'has_embedding': self.embedding is not None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

class OrgMemory(db.Model):
    __tablename__ = 'org_memory'
    id = db.Column(db.Integer, primary_key=True)
    # Org-scoped memory must be readable in worker/background contexts and across admins.
    fact = db.Column(EncryptedType(db.Text, strategy='system'), nullable=False)
    embedding = db.Column(db.LargeBinary, nullable=True)
    json_metadata = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)

class UserMemory(db.Model):
    __tablename__ = 'user_memory'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    fact = db.Column(EncryptedType(db.Text, strategy='user'), nullable=False)
    embedding = db.Column(db.LargeBinary, nullable=True)
    json_metadata = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)

    user = db.relationship('User', backref=db.backref('user_memories', lazy=True, cascade="all, delete-orphan"))

    __table_args__ = (
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_user_memory_user_id_user'),
    )

class UserAlert(db.Model):
    __tablename__ = 'user_alert'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    action_url = db.Column(db.String(1024), nullable=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    user = db.relationship('User', backref=db.backref('user_alerts', lazy=True, cascade="all, delete-orphan"))

    __table_args__ = (
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_user_alert_user_id_user'),
    )

class DataContextChunk(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    data_context_id = db.Column(db.Integer, nullable=False)
    chunk_text = db.Column(EncryptedType(db.Text, strategy='user'), nullable=False)
    json_metadata = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)

    __table_args__ = (db.ForeignKeyConstraint(['data_context_id'], ['data_context.id'], name='fk_data_context_chunk_data_context_id_data_context'),)


class KnowledgeSource(db.Model):
    """Stable logical identity for a document, thread, message, or other source."""

    __tablename__ = 'knowledge_source'

    id = db.Column(
        db.String(40),
        primary_key=True,
        default=lambda: f"src_{uuid.uuid4().hex}",
    )
    user_id = db.Column(db.Integer, nullable=False, index=True)
    source_type = db.Column(db.String(64), nullable=False, index=True)
    external_id = db.Column(db.String(512), nullable=False)
    title = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    mime_type = db.Column(db.String(255), nullable=True)
    current_version_number = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    source_created_at = db.Column(db.DateTime, nullable=True, index=True)
    source_modified_at = db.Column(db.DateTime, nullable=True, index=True)
    access_policy_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    legacy_data_context_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=utc_now_db,
        onupdate=utc_now_db,
        nullable=False,
    )

    user = db.relationship(
        'User',
        backref=db.backref(
            'knowledge_sources',
            lazy='dynamic',
            cascade='all, delete-orphan',
        ),
    )
    versions = db.relationship(
        'KnowledgeSourceVersion',
        back_populates='source',
        cascade='all, delete-orphan',
        order_by='KnowledgeSourceVersion.version_number',
    )
    memberships = db.relationship(
        'KnowledgeCollectionMembership',
        back_populates='source',
        cascade='all, delete-orphan',
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['user_id'],
            ['user.id'],
            name='fk_knowledge_source_user_id_user',
        ),
        db.ForeignKeyConstraint(
            ['legacy_data_context_id'],
            ['data_context.id'],
            name='fk_knowledge_source_legacy_context_id_data_context',
        ),
        db.UniqueConstraint(
            'user_id',
            'source_type',
            'external_id',
            name='uq_knowledge_source_owner_type_external',
        ),
        db.CheckConstraint(
            'current_version_number >= 0',
            name='ck_knowledge_source_current_version_nonnegative',
        ),
    )

    @property
    def current_version(self):
        if not self.current_version_number:
            return None
        return next(
            (
                version
                for version in self.versions
                if version.version_number == self.current_version_number
            ),
            None,
        )

    def to_dict(self, *, include_metadata=True):
        payload = {
            'id': self.id,
            'user_id': self.user_id,
            'source_type': self.source_type,
            'external_id': self.external_id,
            'title': self.title,
            'mime_type': self.mime_type,
            'current_version_number': self.current_version_number,
            'source_created_at': self.source_created_at.isoformat() if self.source_created_at else None,
            'source_modified_at': self.source_modified_at.isoformat() if self.source_modified_at else None,
            'legacy_data_context_id': self.legacy_data_context_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_metadata:
            payload['metadata'] = self.metadata_json or {}
            payload['access_policy'] = self.access_policy_json or {}
        return payload


class KnowledgeSourceVersion(db.Model):
    """Immutable extracted representation of one source revision."""

    __tablename__ = 'knowledge_source_version'

    id = db.Column(
        db.String(41),
        primary_key=True,
        default=lambda: f"srcv_{uuid.uuid4().hex}",
    )
    source_id = db.Column(db.String(40), nullable=False, index=True)
    version_number = db.Column(db.Integer, nullable=False)
    content_hash = db.Column(db.String(64), nullable=False, index=True)
    content = db.Column(EncryptedType(db.Text, strategy='user'), nullable=False)
    extraction_status = db.Column(
        db.String(24),
        nullable=False,
        default='ready',
        server_default='ready',
        index=True,
    )
    extractor_name = db.Column(db.String(128), nullable=True)
    extractor_version = db.Column(db.String(64), nullable=True)
    extractor_config_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    provenance_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    char_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    byte_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    token_estimate = db.Column(db.Integer, nullable=True)
    source_modified_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    source = db.relationship('KnowledgeSource', back_populates='versions')
    units = db.relationship(
        'KnowledgeSourceUnit',
        back_populates='source_version',
        cascade='all, delete-orphan',
        order_by='KnowledgeSourceUnit.ordinal',
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['source_id'],
            ['knowledge_source.id'],
            name='fk_knowledge_source_version_source_id_source',
        ),
        db.UniqueConstraint(
            'source_id',
            'version_number',
            name='uq_knowledge_source_version_number',
        ),
        db.UniqueConstraint(
            'source_id',
            'content_hash',
            name='uq_knowledge_source_version_hash',
        ),
        db.CheckConstraint(
            'version_number >= 1',
            name='ck_knowledge_source_version_positive',
        ),
        db.CheckConstraint(
            "extraction_status in ('pending','ready','failed','superseded')",
            name='ck_knowledge_source_version_status',
        ),
    )

    def to_dict(self, *, include_content=False):
        payload = {
            'id': self.id,
            'source_id': self.source_id,
            'version_number': self.version_number,
            'content_hash': self.content_hash,
            'extraction_status': self.extraction_status,
            'extractor_name': self.extractor_name,
            'extractor_version': self.extractor_version,
            'char_count': self.char_count,
            'byte_count': self.byte_count,
            'token_estimate': self.token_estimate,
            'source_modified_at': self.source_modified_at.isoformat() if self.source_modified_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'unit_count': len(self.units),
            'provenance': self.provenance_json or {},
        }
        if include_content:
            payload['content'] = self.content
        return payload


class KnowledgeSourceUnit(db.Model):
    """Ordered, addressable evidence unit within a frozen source version."""

    __tablename__ = 'knowledge_source_unit'

    id = db.Column(
        db.String(41),
        primary_key=True,
        default=lambda: f"unit_{uuid.uuid4().hex}",
    )
    source_version_id = db.Column(db.String(41), nullable=False, index=True)
    ordinal = db.Column(db.Integer, nullable=False)
    unit_type = db.Column(db.String(48), nullable=False, default='segment', server_default='segment', index=True)
    external_id = db.Column(db.String(512), nullable=True, index=True)
    anchor = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    content = db.Column(EncryptedType(db.Text, strategy='user'), nullable=False)
    content_hash = db.Column(db.String(64), nullable=False, index=True)
    start_offset = db.Column(db.Integer, nullable=True)
    end_offset = db.Column(db.Integer, nullable=True)
    occurred_at = db.Column(db.DateTime, nullable=True, index=True)
    char_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    token_estimate = db.Column(db.Integer, nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    source_version = db.relationship(
        'KnowledgeSourceVersion',
        back_populates='units',
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['source_version_id'],
            ['knowledge_source_version.id'],
            name='fk_knowledge_source_unit_version_id_version',
        ),
        db.UniqueConstraint(
            'source_version_id',
            'ordinal',
            name='uq_knowledge_source_unit_version_ordinal',
        ),
        db.CheckConstraint(
            'ordinal >= 0',
            name='ck_knowledge_source_unit_ordinal_nonnegative',
        ),
        db.CheckConstraint(
            'start_offset is null or start_offset >= 0',
            name='ck_knowledge_source_unit_start_nonnegative',
        ),
        db.CheckConstraint(
            'end_offset is null or start_offset is null or end_offset >= start_offset',
            name='ck_knowledge_source_unit_offsets',
        ),
    )

    def to_dict(self, *, include_content=False):
        payload = {
            'id': self.id,
            'source_version_id': self.source_version_id,
            'ordinal': self.ordinal,
            'unit_type': self.unit_type,
            'external_id': self.external_id,
            'anchor': self.anchor,
            'content_hash': self.content_hash,
            'start_offset': self.start_offset,
            'end_offset': self.end_offset,
            'occurred_at': self.occurred_at.isoformat() if self.occurred_at else None,
            'char_count': self.char_count,
            'token_estimate': self.token_estimate,
            'metadata': self.metadata_json or {},
        }
        if include_content:
            payload['content'] = self.content
        return payload


class KnowledgeCollectionMembership(db.Model):
    """Collection projection membership; never owns canonical content."""

    __tablename__ = 'knowledge_collection_membership'

    id = db.Column(db.Integer, primary_key=True)
    collection_id = db.Column(db.Integer, nullable=False, index=True)
    source_id = db.Column(db.String(40), nullable=False, index=True)
    frozen_version_id = db.Column(db.String(41), nullable=True, index=True)
    position = db.Column(db.Integer, nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=utc_now_db,
        onupdate=utc_now_db,
        nullable=False,
    )

    collection = db.relationship(
        'DataContextCollection',
        backref=db.backref(
            'knowledge_memberships',
            lazy=True,
            cascade='all, delete-orphan',
        ),
    )
    source = db.relationship('KnowledgeSource', back_populates='memberships')
    frozen_version = db.relationship('KnowledgeSourceVersion')

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['collection_id'],
            ['data_context_collection.id'],
            name='fk_knowledge_membership_collection_id_collection',
        ),
        db.ForeignKeyConstraint(
            ['source_id'],
            ['knowledge_source.id'],
            name='fk_knowledge_membership_source_id_source',
        ),
        db.ForeignKeyConstraint(
            ['frozen_version_id'],
            ['knowledge_source_version.id'],
            name='fk_knowledge_membership_version_id_version',
        ),
        db.UniqueConstraint(
            'collection_id',
            'source_id',
            name='uq_knowledge_membership_collection_source',
        ),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'collection_id': self.collection_id,
            'source_id': self.source_id,
            'frozen_version_id': self.frozen_version_id,
            'position': self.position,
            'metadata': self.metadata_json or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class KnowledgeSavedView(db.Model):
    """A refreshable structured query, notably for email collections."""

    __tablename__ = 'knowledge_saved_view'

    id = db.Column(
        db.String(40),
        primary_key=True,
        default=lambda: f"view_{uuid.uuid4().hex}",
    )
    collection_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    source_type = db.Column(db.String(64), nullable=False, index=True)
    query_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=False)
    query_version = db.Column(db.Integer, nullable=False, default=1, server_default='1')
    refresh_status = db.Column(db.String(24), nullable=False, default='ready', server_default='ready')
    result_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    last_refreshed_at = db.Column(db.DateTime, nullable=True)
    error_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=utc_now_db,
        onupdate=utc_now_db,
        nullable=False,
    )

    collection = db.relationship(
        'DataContextCollection',
        backref=db.backref(
            'knowledge_saved_view',
            uselist=False,
            cascade='all, delete-orphan',
        ),
    )
    user = db.relationship('User')

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['collection_id'],
            ['data_context_collection.id'],
            name='fk_knowledge_saved_view_collection_id_collection',
        ),
        db.ForeignKeyConstraint(
            ['user_id'],
            ['user.id'],
            name='fk_knowledge_saved_view_user_id_user',
        ),
        db.CheckConstraint(
            'query_version >= 1',
            name='ck_knowledge_saved_view_query_version_positive',
        ),
        db.CheckConstraint(
            "refresh_status in ('pending','refreshing','ready','failed')",
            name='ck_knowledge_saved_view_refresh_status',
        ),
    )


class KnowledgeIndexGeneration(db.Model):
    """Immutable search projection generation with blue-green activation."""

    __tablename__ = 'knowledge_index_generation'

    id = db.Column(
        db.String(39),
        primary_key=True,
        default=lambda: f"idx_{uuid.uuid4().hex}",
    )
    user_id = db.Column(db.Integer, nullable=False, index=True)
    collection_id = db.Column(db.Integer, nullable=True, index=True)
    projection_type = db.Column(db.String(32), nullable=False, default='hybrid', server_default='hybrid')
    status = db.Column(db.String(24), nullable=False, default='building', server_default='building', index=True)
    signature_hash = db.Column(db.String(64), nullable=False, index=True)
    signature_json = db.Column(db.JSON, nullable=False)
    previous_generation_id = db.Column(db.String(39), nullable=True)
    entry_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    health_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    error_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    activated_at = db.Column(db.DateTime, nullable=True)
    retired_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User')
    collection = db.relationship('DataContextCollection')
    entries = db.relationship(
        'KnowledgeIndexEntry',
        back_populates='generation',
        cascade='all, delete-orphan',
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['user_id'],
            ['user.id'],
            name='fk_knowledge_index_generation_user_id_user',
        ),
        db.ForeignKeyConstraint(
            ['collection_id'],
            ['data_context_collection.id'],
            name='fk_knowledge_index_generation_collection_id_collection',
        ),
        db.ForeignKeyConstraint(
            ['previous_generation_id'],
            ['knowledge_index_generation.id'],
            name='fk_knowledge_index_generation_previous_id_generation',
        ),
        db.CheckConstraint(
            "status in ('building','ready','active','failed','retired')",
            name='ck_knowledge_index_generation_status',
        ),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'collection_id': self.collection_id,
            'projection_type': self.projection_type,
            'status': self.status,
            'signature_hash': self.signature_hash,
            'signature': self.signature_json or {},
            'previous_generation_id': self.previous_generation_id,
            'entry_count': self.entry_count,
            'health': self.health_json or {},
            'error': self.error_json,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'activated_at': self.activated_at.isoformat() if self.activated_at else None,
            'retired_at': self.retired_at.isoformat() if self.retired_at else None,
        }


class KnowledgeIndexEntry(db.Model):
    """A generation-specific projection pointer to one canonical source unit."""

    __tablename__ = 'knowledge_index_entry'

    id = db.Column(db.Integer, primary_key=True)
    generation_id = db.Column(db.String(39), nullable=False, index=True)
    source_unit_id = db.Column(db.String(41), nullable=False, index=True)
    ordinal = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    vector_key = db.Column(db.String(128), nullable=True, index=True)
    lexical_fingerprints = db.Column(db.JSON, nullable=True)
    projection_metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    generation = db.relationship(
        'KnowledgeIndexGeneration',
        back_populates='entries',
    )
    source_unit = db.relationship('KnowledgeSourceUnit')

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['generation_id'],
            ['knowledge_index_generation.id'],
            name='fk_knowledge_index_entry_generation_id_generation',
        ),
        db.ForeignKeyConstraint(
            ['source_unit_id'],
            ['knowledge_source_unit.id'],
            name='fk_knowledge_index_entry_source_unit_id_unit',
        ),
        db.UniqueConstraint(
            'generation_id',
            'source_unit_id',
            name='uq_knowledge_index_entry_generation_unit',
        ),
    )


class KnowledgeCoverageRun(db.Model):
    """Exhaustive, resumable processing of every unit in a frozen version."""

    __tablename__ = 'knowledge_coverage_run'

    id = db.Column(
        db.String(39),
        primary_key=True,
        default=lambda: f"cov_{uuid.uuid4().hex}",
    )
    user_id = db.Column(db.Integer, nullable=False, index=True)
    source_id = db.Column(db.String(40), nullable=False, index=True)
    source_version_id = db.Column(db.String(41), nullable=False, index=True)
    session_id = db.Column(db.String(36), nullable=True, index=True)
    turn_id = db.Column(db.String(36), nullable=True, index=True)
    model_id = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(24), nullable=False, default='pending', server_default='pending', index=True)
    total_units = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    covered_units = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    failed_units = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    manifest_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    synthesis_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=utc_now_db,
        onupdate=utc_now_db,
        nullable=False,
    )
    completed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User')
    source = db.relationship('KnowledgeSource')
    source_version = db.relationship('KnowledgeSourceVersion')
    slices = db.relationship(
        'KnowledgeCoverageSlice',
        back_populates='coverage_run',
        cascade='all, delete-orphan',
        order_by='KnowledgeCoverageSlice.slice_index',
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['user_id'],
            ['user.id'],
            name='fk_knowledge_coverage_run_user_id_user',
        ),
        db.ForeignKeyConstraint(
            ['source_id'],
            ['knowledge_source.id'],
            name='fk_knowledge_coverage_run_source_id_source',
        ),
        db.ForeignKeyConstraint(
            ['source_version_id'],
            ['knowledge_source_version.id'],
            name='fk_knowledge_coverage_run_version_id_version',
        ),
        db.ForeignKeyConstraint(
            ['session_id'],
            ['chat_session.id'],
            name='fk_knowledge_coverage_run_session_id_session',
        ),
        db.ForeignKeyConstraint(
            ['turn_id'],
            ['chat_turn.id'],
            name='fk_knowledge_coverage_run_turn_id_turn',
        ),
        db.CheckConstraint(
            "status in ('pending','running','completed','partial','failed')",
            name='ck_knowledge_coverage_run_status',
        ),
    )


class KnowledgeCoverageSlice(db.Model):
    """One non-overlapping primary range in an exhaustive coverage run."""

    __tablename__ = 'knowledge_coverage_slice'

    id = db.Column(
        db.String(42),
        primary_key=True,
        default=lambda: f"covsl_{uuid.uuid4().hex}",
    )
    coverage_run_id = db.Column(db.String(39), nullable=False, index=True)
    slice_index = db.Column(db.Integer, nullable=False)
    start_ordinal = db.Column(db.Integer, nullable=False)
    end_ordinal = db.Column(db.Integer, nullable=False)
    overlap_before = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    overlap_after = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    status = db.Column(db.String(24), nullable=False, default='pending', server_default='pending', index=True)
    output_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    evidence_refs = db.Column(db.JSON, nullable=True)
    error_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    coverage_run = db.relationship(
        'KnowledgeCoverageRun',
        back_populates='slices',
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['coverage_run_id'],
            ['knowledge_coverage_run.id'],
            name='fk_knowledge_coverage_slice_run_id_run',
        ),
        db.UniqueConstraint(
            'coverage_run_id',
            'slice_index',
            name='uq_knowledge_coverage_slice_run_index',
        ),
        db.CheckConstraint(
            'slice_index >= 0',
            name='ck_knowledge_coverage_slice_index_nonnegative',
        ),
        db.CheckConstraint(
            'start_ordinal >= 0 and end_ordinal >= start_ordinal',
            name='ck_knowledge_coverage_slice_ordinals',
        ),
        db.CheckConstraint(
            "status in ('pending','running','completed','failed')",
            name='ck_knowledge_coverage_slice_status',
        ),
    )


class LLMCost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True) # Null for system tasks
    session_id = db.Column(db.String(36), nullable=True, index=True)
    workflow_run_id = db.Column(db.String(36), nullable=True, index=True)
    workflow_step_id = db.Column(db.String(36), nullable=True, index=True)
    runtime_job_id = db.Column(db.String(64), nullable=True, index=True)
    turn_id = db.Column(db.String(36), nullable=True, index=True)
    action_id = db.Column(db.String(64), nullable=True, index=True)
    timestamp = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    cost = db.Column(db.Float, nullable=False)
    prompt_tokens = db.Column(db.Integer, nullable=False)
    completion_tokens = db.Column(db.Integer, nullable=False)
    total_tokens = db.Column(db.Integer, nullable=False)
    model = db.Column(db.String(255), nullable=False)
    source = db.Column(db.String(100), nullable=False) # e.g., 'chat', 'email_cleaning'

    user = db.relationship('User', backref=db.backref('llm_costs', lazy=True, cascade="all, delete-orphan"))
    session = db.relationship('ChatSession', backref=db.backref('llm_costs', lazy=True, cascade="all, delete-orphan"))
    workflow_run = db.relationship('WorkflowRun', backref=db.backref('llm_costs', lazy=True))
    workflow_step = db.relationship('WorkflowStep', backref=db.backref('llm_costs', lazy=True))
    runtime_job = db.relationship('RuntimeJob', backref=db.backref('llm_costs', lazy=True))

    __table_args__ = (
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_llm_cost_user_id_user'),
        db.ForeignKeyConstraint(['session_id'], ['chat_session.id'], name='fk_llm_cost_session_id_chat_session'),
        db.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_llm_cost_workflow_run_id_workflow_run'),
        db.ForeignKeyConstraint(['workflow_step_id'], ['workflow_step.id'], name='fk_llm_cost_workflow_step_id_workflow_step'),
        db.ForeignKeyConstraint(['runtime_job_id'], ['runtime_job.id'], name='fk_llm_cost_runtime_job_id_runtime_job'),
    )

class EdlProject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    transcript_filename = db.Column(db.String(255), nullable=False)
    transcript_path = db.Column(db.String(512), nullable=True) # Relative to uploads folder
    edl_content = db.Column(db.Text, nullable=True)
    custom_prompt = db.Column(db.Text, nullable=True)
    settings_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db)

    user = db.relationship('User', backref=db.backref('edl_projects', lazy=True, cascade="all, delete-orphan"))

    __table_args__ = (db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_edl_project_user_id_user'),)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'transcript_filename': self.transcript_filename,
            'has_edl': bool(self.edl_content),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

class PluginCredential(db.Model):
    __tablename__ = 'plugin_credential'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    provider_name = db.Column(db.String(50), nullable=False)
    credential_name = db.Column(db.String(100), nullable=False)
    credential_data = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=False)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db)

    user = db.relationship('User', backref=db.backref('plugin_credentials', lazy=True, cascade="all, delete-orphan"))

    __table_args__ = (db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_plugin_credential_user_id_user'),)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'provider_name': self.provider_name,
            'credential_name': self.credential_name,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class CredentialDelegation(db.Model):
    __tablename__ = 'credential_delegation'
    delegatee_id = db.Column(db.Integer, primary_key=True)
    provider_name = db.Column(db.String(50), primary_key=True)
    delegator_id = db.Column(db.Integer, nullable=False)
    credential_id = db.Column(db.Integer, nullable=True)

    delegator = db.relationship('User', foreign_keys=[delegator_id], backref=db.backref('delegations_given', lazy='dynamic', cascade="all, delete-orphan"))
    delegatee = db.relationship('User', foreign_keys=[delegatee_id], backref=db.backref('delegations_received', lazy='dynamic', cascade="all, delete-orphan"))
    credential = db.relationship('PluginCredential', foreign_keys=[credential_id], backref=db.backref('delegations', lazy='dynamic', cascade="all, delete-orphan"))

    __table_args__ = (
        db.ForeignKeyConstraint(['delegatee_id'], ['user.id'], name='fk_credential_delegation_delegatee_id_user'),
        db.ForeignKeyConstraint(['delegator_id'], ['user.id'], name='fk_credential_delegation_delegator_id_user'),
        db.ForeignKeyConstraint(['credential_id'], ['plugin_credential.id'], name='fk_credential_delegation_credential_id_plugin_credential'),
    )

    def to_dict(self):
        return {
            'delegator_id': self.delegator_id,
            'delegatee_id': self.delegatee_id,
            'delegatee_email': self.delegatee.email if self.delegatee else None,
            'provider_name': self.provider_name,
            'credential_id': self.credential_id,
            'credential_name': self.credential.credential_name if self.credential else self.provider_name.replace('_', ' ').title()
        }


class AddonConnection(db.Model):
    """One configured external account or service connection.

    Connector definitions live in immutable add-on contracts. This record owns
    user-specific configuration and references encrypted secrets without
    exposing their payloads through serialization.
    """

    __tablename__ = "addon_connection"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connector_addon_id = db.Column(db.String(100), nullable=False, index=True)
    connector_definition_id = db.Column(db.String(128), nullable=False)
    label = db.Column(EncryptedType(db.String(255), strategy="user"), nullable=False)
    state = db.Column(
        db.String(32),
        nullable=False,
        default="ready",
        server_default="ready",
        index=True,
    )
    is_default = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    config_json = db.Column(
        EncryptedType(db.JSON, strategy="user"),
        nullable=True,
    )
    metadata_json = db.Column(
        EncryptedType(db.JSON, strategy="user"),
        nullable=True,
    )
    health_status = db.Column(db.String(32), nullable=True, index=True)
    health_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    legacy_credential_id = db.Column(
        db.Integer,
        db.ForeignKey("plugin_credential.id", ondelete="SET NULL"),
        nullable=True,
    )
    legacy_provider_name = db.Column(db.String(50), nullable=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    owner = db.relationship(
        "User",
        backref=db.backref(
            "addon_connections",
            lazy="dynamic",
            cascade="all, delete-orphan",
        ),
    )
    legacy_credential = db.relationship(
        "PluginCredential",
        foreign_keys=[legacy_credential_id],
        backref=db.backref("migrated_connection", uselist=False),
    )
    secrets = db.relationship(
        "ConnectionSecret",
        back_populates="connection",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "legacy_credential_id",
            name="uq_addon_connection_legacy_credential_id",
        ),
        db.Index(
            "ix_addon_connection_owner_connector_state",
            "owner_user_id",
            "connector_addon_id",
            "state",
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "owner_user_id": self.owner_user_id,
            "connector_addon_id": self.connector_addon_id,
            "connector_definition_id": self.connector_definition_id,
            "label": self.label,
            "state": self.state,
            "is_default": self.is_default,
            "config": self.config_json or {},
            "health_status": self.health_status,
            "health_checked_at": (
                self.health_checked_at.isoformat() if self.health_checked_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConnectionSecret(db.Model):
    """Encrypted secret material attached to a Connection."""

    __tablename__ = "connection_secret"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id = db.Column(
        db.String(36),
        db.ForeignKey("addon_connection.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(128), nullable=False)
    secret_data = db.Column(
        EncryptedType(db.JSON, strategy="system"),
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    connection = db.relationship("AddonConnection", back_populates="secrets")

    __table_args__ = (
        db.UniqueConstraint(
            "connection_id",
            "name",
            name="uq_connection_secret_connection_name",
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "connection_id": self.connection_id,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConnectionGrant(db.Model):
    """Delegated use of a Connection without exposing its secret payload."""

    __tablename__ = "connection_grant"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id = db.Column(
        db.String(36),
        db.ForeignKey("addon_connection.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    grantee_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    granted_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    operations_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    connection = db.relationship(
        "AddonConnection",
        backref=db.backref("grants", cascade="all, delete-orphan"),
    )
    grantee = db.relationship("User", foreign_keys=[grantee_user_id])
    granted_by = db.relationship("User", foreign_keys=[granted_by_user_id])

    __table_args__ = (
        db.UniqueConstraint(
            "connection_id",
            "grantee_user_id",
            name="uq_connection_grant_connection_grantee",
        ),
    )


class CapabilityBinding(db.Model):
    """A scoped selection of one provider for a capability requirement."""

    __tablename__ = "capability_binding"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    binding_key = db.Column(db.String(64), nullable=False, unique=True)
    consumer_addon_id = db.Column(db.String(100), nullable=False, index=True)
    requirement_id = db.Column(db.String(128), nullable=False)
    provider_addon_id = db.Column(db.String(100), nullable=False, index=True)
    provider_capability_id = db.Column(db.String(128), nullable=False)
    connection_id = db.Column(
        db.String(36),
        db.ForeignKey("addon_connection.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scope = db.Column(
        db.String(32),
        nullable=False,
        default="installation",
        server_default="installation",
    )
    scope_id = db.Column(db.String(128), nullable=False, default="", server_default="")
    priority = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    status = db.Column(
        db.String(32),
        nullable=False,
        default="enabled",
        server_default="enabled",
        index=True,
    )
    contract_version = db.Column(db.String(50), nullable=False)
    metadata_json = db.Column(db.JSON, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    created_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    connection = db.relationship(
        "AddonConnection",
        backref=db.backref("capability_bindings", lazy="dynamic"),
    )
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])

    __table_args__ = (
        db.Index(
            "ix_capability_binding_consumer_requirement_scope",
            "consumer_addon_id",
            "requirement_id",
            "scope",
            "scope_id",
            "status",
        ),
        db.Index(
            "ix_capability_binding_provider_capability",
            "provider_addon_id",
            "provider_capability_id",
        ),
    )


class AddonPreference(db.Model):
    """Normalized, encrypted non-secret preferences for an add-on and user."""

    __tablename__ = "addon_preference"

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    addon_id = db.Column(db.String(100), primary_key=True)
    preferences_json = db.Column(
        EncryptedType(db.JSON, strategy="user"),
        nullable=False,
        default=dict,
    )
    migrated_from_user_settings = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user = db.relationship(
        "User",
        backref=db.backref("normalized_addon_preferences", lazy="dynamic"),
    )


class AddonDataMigrationRun(db.Model):
    """Durable run record for resumable one-time add-on data migrations."""

    __tablename__ = "addon_data_migration_run"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    migration_id = db.Column(db.String(100), nullable=False, index=True)
    status = db.Column(db.String(32), nullable=False, index=True)
    checkpoint = db.Column(db.String(100), nullable=True)
    resumed_from_run_id = db.Column(
        db.String(36),
        db.ForeignKey("addon_data_migration_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    report_json = db.Column(db.JSON, nullable=True)
    error_type = db.Column(db.String(200), nullable=True)
    started_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    events = db.relationship(
        "AddonDataMigrationEvent",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AddonDataMigrationEvent.sequence",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "migration_id": self.migration_id,
            "status": self.status,
            "checkpoint": self.checkpoint,
            "resumed_from_run_id": self.resumed_from_run_id,
            "report": self.report_json or {},
            "error_type": self.error_type,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }


class AddonDataMigrationEvent(db.Model):
    """Append-only checkpoint event emitted by an add-on data migration."""

    __tablename__ = "addon_data_migration_event"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(
        db.String(36),
        db.ForeignKey("addon_data_migration_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence = db.Column(db.Integer, nullable=False)
    stage = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(32), nullable=False)
    detail_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    run = db.relationship("AddonDataMigrationRun", back_populates="events")

    __table_args__ = (
        db.UniqueConstraint(
            "run_id",
            "sequence",
            name="uq_addon_data_migration_event_run_sequence",
        ),
    )

class ChatSession(db.Model):
    __tablename__ = 'chat_session'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, nullable=False, index=True)
    title = db.Column(EncryptedType(db.String(255), strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)
    active_plugins = db.Column(db.JSON, nullable=True)
    active_capability_ids = db.Column(db.JSON, nullable=True)
    working_memory = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True) # Session-scoped AI scratchpad.
    context_stewardship = db.Column(
        db.String(24),
        default='assisted',
        nullable=False,
        server_default='assisted',
    )

    user = db.relationship('User', backref=db.backref('chat_sessions', lazy='dynamic', cascade="all, delete-orphan"))
    messages = db.relationship('ChatMessage', back_populates='session', cascade="all, delete-orphan", order_by='ChatMessage.created_at', lazy='dynamic')
    interactions = db.relationship(
        'RuntimeInteraction',
        back_populates='chat_session',
        cascade="all, delete-orphan",
        order_by='RuntimeInteraction.created_at',
        lazy='dynamic',
    )

    __table_args__ = (
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_chat_session_user_id_user'),
        db.Index(
            'ix_chat_session_user_updated_id',
            'user_id',
            'updated_at',
            'id',
        ),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'title': self.title,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'active_plugins': self.active_plugins or [],
            'active_capability_ids': self.active_capability_ids if self.active_capability_ids is not None else (self.active_plugins or []),
            'working_memory': self.working_memory,
            'context_stewardship': self.context_stewardship,
        }

class ChatMessage(db.Model):
    __tablename__ = 'chat_message'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(36), nullable=False, index=True)
    
    # This column stores the entire message dictionary (role, content, tool_calls, etc.)
    # It's flexible and perfectly matches the litellm/OpenAI format.
    message = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=False) 
    tts_audio_path = db.Column(db.String(512), nullable=True)
    
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    
    session = db.relationship('ChatSession', back_populates='messages')

    __table_args__ = (
        db.ForeignKeyConstraint(['session_id'], ['chat_session.id'], name='fk_chat_message_session_id_chat_session'),
        db.Index(
            'ix_chat_message_session_id_id',
            'session_id',
            'id',
        ),
        db.Index(
            'ix_chat_message_session_created_id',
            'session_id',
            'created_at',
            'id',
        ),
    )


class ChatTurn(db.Model):
    """One durable chat turn and its model/admission/finalization state."""
    __tablename__ = 'chat_turn'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = db.Column(db.String(36), nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    status = db.Column(db.String(24), nullable=False, default='pending', server_default='pending', index=True)
    context_stewardship = db.Column(db.String(24), nullable=False, default='assisted', server_default='assisted')
    model_id = db.Column(db.String(255), nullable=True)
    assistant_message_id = db.Column(db.Integer, nullable=True)
    base_memory_revision = db.Column(db.Integer, nullable=True)
    admission_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    finalization_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    error_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)
    finalized_at = db.Column(db.DateTime, nullable=True)

    session = db.relationship('ChatSession', backref=db.backref('turns', lazy='dynamic', cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('chat_turns', lazy='dynamic', cascade='all, delete-orphan'))
    assistant_message = db.relationship('ChatMessage', foreign_keys=[assistant_message_id])

    __table_args__ = (
        db.ForeignKeyConstraint(['session_id'], ['chat_session.id'], name='fk_chat_turn_session_id_chat_session'),
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_chat_turn_user_id_user'),
        db.ForeignKeyConstraint(['assistant_message_id'], ['chat_message.id'], name='fk_chat_turn_assistant_message_id_chat_message'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'status': self.status,
            'context_stewardship': self.context_stewardship,
            'model_id': self.model_id,
            'assistant_message_id': self.assistant_message_id,
            'base_memory_revision': self.base_memory_revision,
            'admission': self.admission_json,
            'finalization': self.finalization_json,
            'error': self.error_json,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'finalized_at': self.finalized_at.isoformat() if self.finalized_at else None,
        }


class ChatEvidence(db.Model):
    """Immutable raw evidence retained independently from prompt lifecycle."""
    __tablename__ = 'chat_evidence'

    id = db.Column(db.String(36), primary_key=True, default=lambda: f"ev_{uuid.uuid4().hex}")
    session_id = db.Column(db.String(36), nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    turn_id = db.Column(db.String(36), nullable=True, index=True)
    kind = db.Column(db.String(64), nullable=False, index=True)
    role = db.Column(db.String(32), nullable=True)
    mime_type = db.Column(db.String(255), nullable=True)
    source_type = db.Column(db.String(64), nullable=True, index=True)
    source_id = db.Column(db.String(512), nullable=True, index=True)
    source_version = db.Column(db.String(128), nullable=True)
    content_hash = db.Column(db.String(64), nullable=False, index=True)
    content = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    content_encoding = db.Column(db.String(32), nullable=False, default='utf-8', server_default='utf-8')
    char_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    byte_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    token_estimate = db.Column(db.Integer, nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    retention_state = db.Column(db.String(24), nullable=False, default='retained', server_default='retained', index=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)

    session = db.relationship('ChatSession', backref=db.backref('evidence_records', lazy='dynamic', cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('chat_evidence_records', lazy='dynamic', cascade='all, delete-orphan'))
    turn = db.relationship('ChatTurn', backref=db.backref('evidence_records', lazy='dynamic'))

    __table_args__ = (
        db.ForeignKeyConstraint(['session_id'], ['chat_session.id'], name='fk_chat_evidence_session_id_chat_session'),
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_chat_evidence_user_id_user'),
        db.ForeignKeyConstraint(['turn_id'], ['chat_turn.id'], name='fk_chat_evidence_turn_id_chat_turn'),
    )

    def to_dict(self, include_content=False):
        payload = {
            'id': self.id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'turn_id': self.turn_id,
            'kind': self.kind,
            'role': self.role,
            'mime_type': self.mime_type,
            'source_type': self.source_type,
            'source_id': self.source_id,
            'source_version': self.source_version,
            'content_hash': self.content_hash,
            'char_count': self.char_count,
            'byte_count': self.byte_count,
            'token_estimate': self.token_estimate,
            'metadata': self.metadata_json or {},
            'retention_state': self.retention_state,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'deleted_at': self.deleted_at.isoformat() if self.deleted_at else None,
        }
        if include_content:
            payload['content'] = self.content
        return payload


class ChatEvidenceChunk(db.Model):
    """Encrypted ordered chunk for evidence too large for one materialization."""
    __tablename__ = 'chat_evidence_chunk'

    id = db.Column(db.Integer, primary_key=True)
    evidence_id = db.Column(db.String(36), nullable=False, index=True)
    ordinal = db.Column(db.Integer, nullable=False)
    start_offset = db.Column(db.Integer, nullable=False)
    end_offset = db.Column(db.Integer, nullable=False)
    content_hash = db.Column(db.String(64), nullable=False)
    content = db.Column(EncryptedType(db.Text, strategy='user'), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    evidence = db.relationship(
        'ChatEvidence',
        backref=db.backref('chunks', lazy=True, cascade='all, delete-orphan', order_by='ChatEvidenceChunk.ordinal'),
    )

    __table_args__ = (
        db.UniqueConstraint('evidence_id', 'ordinal', name='uq_chat_evidence_chunk_evidence_ordinal'),
        db.ForeignKeyConstraint(['evidence_id'], ['chat_evidence.id'], name='fk_chat_evidence_chunk_evidence_id_chat_evidence'),
        db.CheckConstraint('start_offset >= 0', name='ck_chat_evidence_chunk_start_nonnegative'),
        db.CheckConstraint('end_offset >= start_offset', name='ck_chat_evidence_chunk_offsets'),
    )


class ChatContextItem(db.Model):
    """An addressable prompt view over durable evidence."""
    __tablename__ = 'chat_context_item'

    id = db.Column(db.String(36), primary_key=True, default=lambda: f"ctx_{uuid.uuid4().hex}")
    session_id = db.Column(db.String(36), nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    evidence_id = db.Column(db.String(36), nullable=False, index=True)
    first_turn_id = db.Column(db.String(36), nullable=True)
    last_turn_id = db.Column(db.String(36), nullable=True)
    kind = db.Column(db.String(64), nullable=False, index=True)
    title = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    lifecycle_state = db.Column(db.String(24), nullable=False, default='active', server_default='active', index=True)
    materialization = db.Column(db.String(24), nullable=False, default='full', server_default='full')
    materialized_content = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    priority = db.Column(db.Integer, nullable=False, default=50, server_default='50')
    is_pinned = db.Column(db.Boolean, nullable=False, default=False, server_default='0', index=True)
    token_estimate = db.Column(db.Integer, nullable=True)
    content_hash = db.Column(db.String(64), nullable=False)
    source_ref_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    version = db.Column(db.Integer, nullable=False, default=1, server_default='1')
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)

    session = db.relationship('ChatSession', backref=db.backref('context_items', lazy='dynamic', cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('chat_context_items', lazy='dynamic', cascade='all, delete-orphan'))
    evidence = db.relationship('ChatEvidence', backref=db.backref('context_items', lazy='dynamic'))
    first_turn = db.relationship('ChatTurn', foreign_keys=[first_turn_id])
    last_turn = db.relationship('ChatTurn', foreign_keys=[last_turn_id])

    __table_args__ = (
        db.ForeignKeyConstraint(['session_id'], ['chat_session.id'], name='fk_chat_context_item_session_id_chat_session'),
        db.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_chat_context_item_user_id_user'),
        db.ForeignKeyConstraint(['evidence_id'], ['chat_evidence.id'], name='fk_chat_context_item_evidence_id_chat_evidence'),
        db.ForeignKeyConstraint(['first_turn_id'], ['chat_turn.id'], name='fk_chat_context_item_first_turn_id_chat_turn'),
        db.ForeignKeyConstraint(['last_turn_id'], ['chat_turn.id'], name='fk_chat_context_item_last_turn_id_chat_turn'),
        db.CheckConstraint("lifecycle_state in ('active','archived','excluded','deleted')", name='ck_chat_context_item_lifecycle_state'),
        db.CheckConstraint("materialization in ('full','excerpt','summary','reference','overflow')", name='ck_chat_context_item_materialization'),
        db.CheckConstraint('priority >= 0 and priority <= 100', name='ck_chat_context_item_priority'),
        db.CheckConstraint('version >= 1', name='ck_chat_context_item_version'),
    )

    def to_dict(self, include_materialized_content=False):
        payload = {
            'id': self.id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'evidence_id': self.evidence_id,
            'first_turn_id': self.first_turn_id,
            'last_turn_id': self.last_turn_id,
            'kind': self.kind,
            'title': self.title,
            'lifecycle_state': self.lifecycle_state,
            'materialization': self.materialization,
            'priority': self.priority,
            'is_pinned': self.is_pinned,
            'token_estimate': self.token_estimate,
            'content_hash': self.content_hash,
            'source_ref': self.source_ref_json or {},
            'metadata': self.metadata_json or {},
            'version': self.version,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_materialized_content:
            payload['materialized_content'] = self.materialized_content
        return payload


class ChatContextEvent(db.Model):
    """Append-only audit event for context lifecycle and materialization changes."""
    __tablename__ = 'chat_context_event'

    id = db.Column(db.String(36), primary_key=True, default=lambda: f"ce_{uuid.uuid4().hex}")
    context_item_id = db.Column(db.String(36), nullable=False, index=True)
    session_id = db.Column(db.String(36), nullable=False, index=True)
    turn_id = db.Column(db.String(36), nullable=True, index=True)
    actor_type = db.Column(db.String(24), nullable=False)
    actor_id = db.Column(db.String(255), nullable=True)
    event_type = db.Column(db.String(32), nullable=False, index=True)
    from_state = db.Column(db.String(24), nullable=True)
    to_state = db.Column(db.String(24), nullable=True)
    from_materialization = db.Column(db.String(24), nullable=True)
    to_materialization = db.Column(db.String(24), nullable=True)
    expected_version = db.Column(db.Integer, nullable=True)
    result_version = db.Column(db.Integer, nullable=False)
    reason = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    context_item = db.relationship('ChatContextItem', backref=db.backref('events', lazy=True, cascade='all, delete-orphan', order_by='ChatContextEvent.created_at'))
    session = db.relationship('ChatSession')
    turn = db.relationship('ChatTurn')

    __table_args__ = (
        db.ForeignKeyConstraint(['context_item_id'], ['chat_context_item.id'], name='fk_chat_context_event_item_id_chat_context_item'),
        db.ForeignKeyConstraint(['session_id'], ['chat_session.id'], name='fk_chat_context_event_session_id_chat_session'),
        db.ForeignKeyConstraint(['turn_id'], ['chat_turn.id'], name='fk_chat_context_event_turn_id_chat_turn'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'context_item_id': self.context_item_id,
            'session_id': self.session_id,
            'turn_id': self.turn_id,
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'event_type': self.event_type,
            'from_state': self.from_state,
            'to_state': self.to_state,
            'from_materialization': self.from_materialization,
            'to_materialization': self.to_materialization,
            'expected_version': self.expected_version,
            'result_version': self.result_version,
            'reason': self.reason,
            'metadata': self.metadata_json or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ChatMemoryRevision(db.Model):
    """Versioned structured session memory with evidence-linked patches."""
    __tablename__ = 'chat_memory_revision'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(36), nullable=False, index=True)
    turn_id = db.Column(db.String(36), nullable=True, index=True)
    revision = db.Column(db.Integer, nullable=False)
    parent_revision = db.Column(db.Integer, nullable=True)
    model_id = db.Column(db.String(255), nullable=True)
    patch_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=False)
    snapshot_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=False)
    evidence_refs = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    session = db.relationship('ChatSession', backref=db.backref('memory_revisions', lazy='dynamic', cascade='all, delete-orphan'))
    turn = db.relationship('ChatTurn', backref=db.backref('memory_revision', uselist=False))

    __table_args__ = (
        db.UniqueConstraint('session_id', 'revision', name='uq_chat_memory_revision_session_revision'),
        db.UniqueConstraint('turn_id', name='uq_chat_memory_revision_turn_id'),
        db.ForeignKeyConstraint(['session_id'], ['chat_session.id'], name='fk_chat_memory_revision_session_id_chat_session'),
        db.ForeignKeyConstraint(['turn_id'], ['chat_turn.id'], name='fk_chat_memory_revision_turn_id_chat_turn'),
        db.CheckConstraint('revision >= 1', name='ck_chat_memory_revision_positive'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'turn_id': self.turn_id,
            'revision': self.revision,
            'parent_revision': self.parent_revision,
            'model_id': self.model_id,
            'patch': self.patch_json,
            'snapshot': self.snapshot_json,
            'evidence_refs': self.evidence_refs or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class WorkflowTemplate(db.Model):
    __tablename__ = 'workflow_template'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    definition_json = db.Column(db.JSON, nullable=False)
    owner_id = db.Column(db.Integer, nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)
    
    __table_args__ = (
        db.ForeignKeyConstraint(['owner_id'], ['user.id'], name='fk_workflow_template_owner_id_user'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'definition': self.definition_json,
            'owner_id': self.owner_id,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class WorkflowRun(db.Model):
    __tablename__ = 'workflow_run'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(EncryptedType(db.String(255), strategy='system'), nullable=True)
    kind = db.Column(db.String(32), nullable=False, default='template', index=True)
    workflow_id = db.Column(db.String(255), nullable=True, index=True)
    source_addon_id = db.Column(db.String(100), nullable=True, index=True)
    status = db.Column(db.String(32), nullable=False, default='draft', index=True)
    input_payload = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    understanding_json = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    plan_json = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    state_json = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    current_step = db.Column(db.String(255), nullable=True)
    created_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    chat_session_id = db.Column(db.String(36), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    approval_ids = db.Column(db.JSON, nullable=True)
    resource_reference_ids = db.Column(db.JSON, nullable=True)
    job_ids = db.Column(db.JSON, nullable=True)
    step_ids = db.Column(db.JSON, nullable=True)
    source_ids = db.Column(db.JSON, nullable=True)
    evidence_ids = db.Column(db.JSON, nullable=True)
    artifact_ids = db.Column(db.JSON, nullable=True)
    log_source_ids = db.Column(db.JSON, nullable=True)
    resume_url = db.Column(db.String(512), nullable=True)
    cancel_url = db.Column(db.String(512), nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_workflow_run_created_by_user_id_user'),
        db.ForeignKeyConstraint(['chat_session_id'], ['chat_session.id'], name='fk_workflow_run_chat_session_id_chat_session'),
        db.Index('ix_workflow_run_owner_updated_id', 'created_by_user_id', 'updated_at', 'id'),
    )

    def to_dict(self, *, projection=None):
        """Compatibility facade over the explicit run projection serializer."""
        from iarf_assistant.core.runs.projections import (
            load_run_projection,
            serialize_workflow_run,
        )

        return serialize_workflow_run(self, projection or load_run_projection(self))


class WorkflowStep(db.Model):
    __tablename__ = 'workflow_step'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_run_id = db.Column(db.String(36), nullable=False, index=True)
    parent_step_id = db.Column(db.String(36), nullable=True, index=True)
    depends_on_step_ids = db.Column(db.JSON, nullable=True)
    step_key = db.Column(db.String(255), nullable=True, index=True)
    step_type = db.Column(db.String(50), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(32), nullable=False, default='planned', index=True)
    sequence = db.Column(db.Integer, nullable=True)
    assigned_model_role = db.Column(db.String(100), nullable=True)
    created_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    instructions = db.Column(EncryptedType(db.Text, strategy='system'), nullable=True)
    capability_ids = db.Column(db.JSON, nullable=True)
    skill_ids = db.Column(db.JSON, nullable=True)
    tool_ids = db.Column(db.JSON, nullable=True)
    provider_requirements = db.Column(db.JSON, nullable=True)
    resource_reference_ids = db.Column(db.JSON, nullable=True)
    approval_ids = db.Column(db.JSON, nullable=True)
    job_ids = db.Column(db.JSON, nullable=True)
    artifact_ids = db.Column(db.JSON, nullable=True)
    source_ids = db.Column(db.JSON, nullable=True)
    evidence_ids = db.Column(db.JSON, nullable=True)
    input_schema = db.Column(db.JSON, nullable=True)
    output_schema = db.Column(db.JSON, nullable=True)
    input_payload = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    output_payload = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    error = db.Column(EncryptedType(db.Text, strategy='system'), nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)
    log_source_ids = db.Column(db.JSON, nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_workflow_step_workflow_run_id_workflow_run'),
        db.ForeignKeyConstraint(['parent_step_id'], ['workflow_step.id'], name='fk_workflow_step_parent_step_id_workflow_step'),
        db.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_workflow_step_created_by_user_id_user'),
        db.Index('ix_workflow_step_run_created_id', 'workflow_run_id', 'created_at', 'id'),
        db.Index('ix_workflow_step_status_sequence_created_id', 'status', 'sequence', 'created_at', 'id'),
    )

    def to_dict(self):
        depends_on_step_ids = self.depends_on_step_ids or ([self.parent_step_id] if self.parent_step_id else [])
        display_title = self.display_title()
        metadata = self.metadata_json if isinstance(self.metadata_json, dict) else {}
        runtime_events = metadata.get('runtime_events') if isinstance(metadata.get('runtime_events'), list) else []
        runtime_event_counts = {}
        for event in runtime_events:
            if not isinstance(event, dict):
                continue
            kind = event.get('kind') or 'unknown'
            runtime_event_counts[kind] = runtime_event_counts.get(kind, 0) + 1
        return {
            'id': self.id,
            'workflow_run_id': self.workflow_run_id,
            'parent_step_id': self.parent_step_id,
            'depends_on_step_ids': depends_on_step_ids,
            'step_key': self.step_key,
            'type': self.step_type,
            'title': self.title,
            'display_title': display_title,
            'status': self.status,
            'sequence': self.sequence,
            'assigned_model_role': self.assigned_model_role,
            'created_by_user_id': self.created_by_user_id,
            'instructions': self.instructions,
            'capability_ids': self.capability_ids or [],
            'skill_ids': self.skill_ids or [],
            'tool_ids': self.tool_ids or [],
            'tool_view_ids': metadata.get('tool_view_ids') or [],
            'provider_requirements': self.provider_requirements or [],
            'resource_reference_ids': self.resource_reference_ids or [],
            'approval_ids': self.approval_ids or [],
            'job_ids': self.job_ids or [],
            'artifact_ids': self.artifact_ids or [],
            'source_ids': self.source_ids or [],
            'evidence_ids': self.evidence_ids or [],
            'input_schema': self.input_schema or {},
            'output_schema': self.output_schema or {},
            'input': self.input_payload or {},
            'output': self.output_payload or {},
            'error': self.error,
            'metadata': metadata,
            'runtime_events': runtime_events[-50:],
            'runtime_event_count': len(runtime_events),
            'runtime_event_counts': runtime_event_counts,
            'latest_runtime_event': runtime_events[-1] if runtime_events else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'log_source_ids': self.log_source_ids or [],
        }

    def display_title(self):
        for candidate in (self.title, self.step_key, self.step_type):
            if candidate:
                return str(candidate).replace('_', ' ').strip()
        return self.id[:8] if self.id else 'Step'


class RuntimeCollection(db.Model):
    """Run-scoped identity and policy for durable many-item work."""

    __tablename__ = 'runtime_collection'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_run_id = db.Column(db.String(36), nullable=False, index=True)
    owner_user_id = db.Column(db.Integer, nullable=True, index=True)
    collection_key = db.Column(db.String(96), nullable=False)
    title = db.Column(EncryptedType(db.String(255), strategy='system'), nullable=False)
    objective = db.Column(EncryptedType(db.Text, strategy='system'), nullable=True)
    kind = db.Column(db.String(100), nullable=False, default='items', index=True)
    adapter_id = db.Column(db.String(100), nullable=False, default='generic', index=True)
    status = db.Column(db.String(32), nullable=False, default='active', index=True)
    identity_keys = db.Column(db.JSON, nullable=True)
    schema_json = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    partitions_json = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='system'), nullable=True)
    storage_version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=utc_now_db,
        onupdate=utc_now_db,
        nullable=False,
    )
    completed_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['workflow_run_id'],
            ['workflow_run.id'],
            name='fk_runtime_collection_workflow_run_id_workflow_run',
        ),
        db.ForeignKeyConstraint(
            ['owner_user_id'],
            ['user.id'],
            name='fk_runtime_collection_owner_user_id_user',
        ),
        db.UniqueConstraint(
            'workflow_run_id',
            'collection_key',
            name='uq_runtime_collection_run_key',
        ),
        db.Index('ix_runtime_collection_run_created_id', 'workflow_run_id', 'created_at', 'id'),
    )

    def summary(self):
        from iarf_assistant.core.collections.projections import (
            load_collection_summaries,
        )

        return load_collection_summaries([self])[str(self.id)]

    def to_dict(self, *, include_items=False, status=None, limit=100):
        from iarf_assistant.core.collections.projections import (
            load_collection_summaries,
            serialize_runtime_collection,
        )

        summary = load_collection_summaries([self])[str(self.id)]
        rows = None
        total = None
        if include_items:
            query = RuntimeCollectionItem.query.filter_by(collection_id=self.id)
            if status:
                query = query.filter_by(status=str(status).lower())
            total = query.count()
            rows = query.order_by(
                RuntimeCollectionItem.created_at.asc(),
                RuntimeCollectionItem.item_key.asc(),
            ).limit(max(1, min(int(limit or 100), 1000))).all()
        return serialize_runtime_collection(
            self,
            summary=summary,
            items=rows,
            total_item_count=total,
        )


class RuntimeCollectionItem(db.Model):
    """One independently claimable and resumable item in a runtime collection."""

    __tablename__ = 'runtime_collection_item'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    collection_id = db.Column(db.String(36), nullable=False, index=True)
    item_key = db.Column(db.String(128), nullable=False)
    identity_hash = db.Column(db.String(64), nullable=False)
    identity_key = db.Column(
        EncryptedType(db.String(2048), strategy='system'),
        nullable=False,
    )
    fingerprint = db.Column(db.String(64), nullable=False, index=True)
    parent_item_key = db.Column(db.String(128), nullable=True, index=True)
    chunk_index = db.Column(db.Integer, nullable=True)
    chunk_count = db.Column(db.Integer, nullable=True)
    source_type = db.Column(db.String(100), nullable=True, index=True)
    source_id = db.Column(
        EncryptedType(db.String(1024), strategy='system'),
        nullable=True,
    )
    source_lookup_hash = db.Column(db.String(96), nullable=True)
    provider = db.Column(db.String(100), nullable=True, index=True)
    external_id = db.Column(
        EncryptedType(db.String(1024), strategy='system'),
        nullable=True,
    )
    external_lookup_hash = db.Column(db.String(96), nullable=True)
    uri = db.Column(EncryptedType(db.Text, strategy='system'), nullable=True)
    canonical_uri = db.Column(
        EncryptedType(db.Text, strategy='system'),
        nullable=True,
    )
    title = db.Column(
        EncryptedType(db.String(512), strategy='system'),
        nullable=True,
    )
    content_hash = db.Column(db.String(128), nullable=True, index=True)
    content_ref_json = db.Column(
        EncryptedType(db.JSON, strategy='system'),
        nullable=True,
    )
    source_ref_json = db.Column(
        EncryptedType(db.JSON, strategy='system'),
        nullable=True,
    )
    resource_reference_ids = db.Column(db.JSON, nullable=True)
    estimated_tokens = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(32), nullable=False, default='pending', index=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    failure_count = db.Column(db.Integer, nullable=False, default=0)
    annotation_count = db.Column(db.Integer, nullable=False, default=0)
    lease_token = db.Column(db.String(36), nullable=True, index=True)
    lease_owner = db.Column(db.String(255), nullable=True, index=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True, index=True)
    workflow_step_id = db.Column(db.String(36), nullable=True, index=True)
    annotations_json = db.Column(
        EncryptedType(db.JSON, strategy='system'),
        nullable=True,
    )
    result_ref_json = db.Column(
        EncryptedType(db.JSON, strategy='system'),
        nullable=True,
    )
    latest_error = db.Column(
        EncryptedType(db.Text, strategy='system'),
        nullable=True,
    )
    metadata_json = db.Column(
        EncryptedType(db.JSON, strategy='system'),
        nullable=True,
    )
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=utc_now_db,
        onupdate=utc_now_db,
        nullable=False,
    )
    leased_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['collection_id'],
            ['runtime_collection.id'],
            name='fk_runtime_collection_item_collection_id_runtime_collection',
        ),
        db.ForeignKeyConstraint(
            ['workflow_step_id'],
            ['workflow_step.id'],
            name='fk_runtime_collection_item_workflow_step_id_workflow_step',
        ),
        db.UniqueConstraint(
            'collection_id',
            'identity_hash',
            name='uq_runtime_collection_item_identity',
        ),
        db.UniqueConstraint(
            'collection_id',
            'item_key',
            name='uq_runtime_collection_item_key',
        ),
        db.Index('ix_collection_item_collection_status_created_key', 'collection_id', 'status', 'created_at', 'item_key'),
        db.Index('ix_collection_item_collection_source_lookup', 'collection_id', 'source_lookup_hash'),
        db.Index('ix_collection_item_collection_external_lookup', 'collection_id', 'external_lookup_hash'),
        db.Index(
            'ix_collection_item_expired_lease',
            'collection_id',
            'lease_expires_at',
            sqlite_where=db.text("status = 'leased' AND lease_expires_at IS NOT NULL"),
        ),
    )

    def to_dict(self):
        return {
            'id': self.item_key,
            'record_id': self.id,
            'collection_record_id': self.collection_id,
            'identity': self.identity_key,
            'identity_hash': self.identity_hash,
            'fingerprint': self.fingerprint,
            'parent_item_id': self.parent_item_key or self.item_key,
            'chunk_index': self.chunk_index,
            'chunk_count': self.chunk_count,
            'source_type': self.source_type,
            'source_id': self.source_id,
            'provider': self.provider,
            'external_id': self.external_id,
            'uri': self.uri,
            'canonical_uri': self.canonical_uri,
            'title': self.title,
            'content_hash': self.content_hash,
            'content_ref': self.content_ref_json or {},
            'source_ref': self.source_ref_json or {},
            'resource_reference_ids': self.resource_reference_ids or [],
            'estimated_tokens': self.estimated_tokens,
            'status': self.status,
            'attempt_count': self.attempt_count,
            'claim_count': self.attempt_count,
            'retry_count': self.failure_count,
            'annotation_count': self.annotation_count,
            'claim_id': self.lease_token,
            'lease_token': self.lease_token,
            'lease_owner': self.lease_owner,
            'lease_expires_at': (
                self.lease_expires_at.isoformat() + 'Z'
                if self.lease_expires_at
                else None
            ),
            'workflow_step_id': self.workflow_step_id,
            'annotations': self.annotations_json or [],
            'result_ref': self.result_ref_json or {},
            'error': self.latest_error,
            'metadata': self.metadata_json or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'leased_at': self.leased_at.isoformat() if self.leased_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


class RuntimeApprovalRequest(db.Model):
    __tablename__ = 'runtime_approval_request'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    requester_user_id = db.Column(db.Integer, nullable=True, index=True)
    approver_user_id = db.Column(db.Integer, nullable=True, index=True)
    subject_type = db.Column(db.String(50), nullable=False, index=True)
    subject_id = db.Column(db.String(255), nullable=False, index=True)
    tool_id = db.Column(db.String(255), nullable=True, index=True)
    workflow_run_id = db.Column(db.String(36), nullable=True, index=True)
    chat_session_id = db.Column(db.String(36), nullable=True)
    effects = db.Column(db.JSON, nullable=True)
    preview_payload = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    status = db.Column(db.String(32), nullable=False, default='pending', index=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    audit_log_source_id = db.Column(db.String(255), nullable=True)
    log_source_ids = db.Column(db.JSON, nullable=True)
    resolution_note = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(['requester_user_id'], ['user.id'], name='fk_runtime_approval_requester_user_id_user'),
        db.ForeignKeyConstraint(['approver_user_id'], ['user.id'], name='fk_runtime_approval_approver_user_id_user'),
        db.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_approval_workflow_run_id_workflow_run'),
        db.Index('ix_runtime_approval_user_status_created_id', 'requester_user_id', 'status', 'created_at', 'id'),
        db.Index(
            'ix_runtime_approval_chat_scope',
            'requester_user_id',
            'status',
            'subject_type',
            'chat_session_id',
            'created_at',
            'id',
        ),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'requester_user_id': self.requester_user_id,
            'approver_user_id': self.approver_user_id,
            'subject_type': self.subject_type,
            'subject_id': self.subject_id,
            'tool_id': self.tool_id,
            'workflow_run_id': self.workflow_run_id,
            'chat_session_id': self.chat_session_id,
            'effects': self.effects or [],
            'preview': self.preview_payload or {},
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'resolved_at': self.resolved_at.isoformat() if self.resolved_at else None,
            'audit_log_source_id': self.audit_log_source_id,
            'log_source_ids': self.log_source_ids or [],
            'resolution_note': self.resolution_note,
        }


class RuntimeInteraction(db.Model):
    """A small, durable user interaction that can suspend and resume one chat turn."""

    __tablename__ = 'runtime_interaction'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    requester_user_id = db.Column(db.Integer, nullable=False, index=True)
    resolver_user_id = db.Column(db.Integer, nullable=True, index=True)
    chat_session_id = db.Column(db.String(36), nullable=False, index=True)
    workflow_run_id = db.Column(db.String(36), nullable=True, index=True)
    approval_id = db.Column(db.String(36), nullable=True, index=True)
    tool_call_id = db.Column(db.String(512), nullable=True)
    tool_name = db.Column(db.String(255), nullable=True, index=True)
    interaction_type = db.Column(db.String(50), nullable=False, index=True)
    request_payload = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=False)
    response_payload = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    resume_payload = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    result_payload = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    status = db.Column(db.String(32), nullable=False, default='pending', index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resume_started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    chat_session = db.relationship('ChatSession', back_populates='interactions')

    __table_args__ = (
        db.ForeignKeyConstraint(
            ['requester_user_id'],
            ['user.id'],
            name='fk_runtime_interaction_requester_user_id_user',
        ),
        db.ForeignKeyConstraint(
            ['resolver_user_id'],
            ['user.id'],
            name='fk_runtime_interaction_resolver_user_id_user',
        ),
        db.ForeignKeyConstraint(
            ['chat_session_id'],
            ['chat_session.id'],
            name='fk_runtime_interaction_chat_session_id_chat_session',
        ),
        db.ForeignKeyConstraint(
            ['workflow_run_id'],
            ['workflow_run.id'],
            name='fk_runtime_interaction_workflow_run_id_workflow_run',
        ),
        db.ForeignKeyConstraint(
            ['approval_id'],
            ['runtime_approval_request.id'],
            name='fk_runtime_interaction_approval_id_runtime_approval_request',
        ),
        db.UniqueConstraint(
            'chat_session_id',
            'tool_call_id',
            name='uq_runtime_interaction_chat_tool_call',
        ),
    )

    def to_dict(self, *, include_resume=False):
        payload = {
            'id': self.id,
            'requester_user_id': self.requester_user_id,
            'resolver_user_id': self.resolver_user_id,
            'chat_session_id': self.chat_session_id,
            'workflow_run_id': self.workflow_run_id,
            'approval_id': self.approval_id,
            'tool_call_id': self.tool_call_id,
            'tool_name': self.tool_name,
            'interaction_type': self.interaction_type,
            'request': self.request_payload or {},
            'response': self.response_payload or {},
            'result': self.result_payload or {},
            'status': self.status,
            'version': self.version,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'resolved_at': self.resolved_at.isoformat() if self.resolved_at else None,
            'resume_started_at': self.resume_started_at.isoformat() if self.resume_started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }
        if include_resume:
            payload['resume'] = self.resume_payload or {}
        return payload


class RuntimeResourceReference(db.Model):
    __tablename__ = 'runtime_resource_reference'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = db.Column(db.Integer, nullable=True, index=True)
    resource_type = db.Column(db.String(50), nullable=False, index=True)
    resource_uri = db.Column(EncryptedType(db.String(1024), strategy='user'), nullable=False)
    provider = db.Column(db.String(100), nullable=True)
    label = db.Column(EncryptedType(db.String(255), strategy='user'), nullable=True)
    permissions = db.Column(db.JSON, nullable=True)
    scopes = db.Column(db.JSON, nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    source_addon_id = db.Column(db.String(100), nullable=True, index=True)
    workflow_run_id = db.Column(db.String(36), nullable=True, index=True)

    __table_args__ = (
        db.ForeignKeyConstraint(['owner_user_id'], ['user.id'], name='fk_runtime_resource_owner_user_id_user'),
        db.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_resource_workflow_run_id_workflow_run'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'owner_user_id': self.owner_user_id,
            'resource_type': self.resource_type,
            'resource_uri': self.resource_uri,
            'provider': self.provider,
            'label': self.label,
            'permissions': self.permissions or ['read'],
            'scopes': self.scopes or [],
            'metadata': self.metadata_json or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'source_addon_id': self.source_addon_id,
            'workflow_run_id': self.workflow_run_id,
        }


class RuntimeJob(db.Model):
    __tablename__ = 'runtime_job'

    id = db.Column(db.String(64), primary_key=True)
    source_type = db.Column(db.String(50), nullable=False, index=True)
    source_id = db.Column(db.String(255), nullable=True, index=True)
    source_addon_id = db.Column(db.String(100), nullable=True, index=True)
    owner_user_id = db.Column(db.Integer, nullable=True, index=True)
    task_name = db.Column(db.String(255), nullable=True)
    workflow_run_id = db.Column(db.String(36), nullable=True, index=True)
    status = db.Column(db.String(32), nullable=False, default='queued', index=True)
    progress = db.Column(db.Float, nullable=True)
    message = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    error = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    input_payload = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    result_payload = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    queued_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)
    log_source_ids = db.Column(db.JSON, nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(['owner_user_id'], ['user.id'], name='fk_runtime_job_owner_user_id_user'),
        db.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_job_workflow_run_id_workflow_run'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'source_type': self.source_type,
            'source_id': self.source_id,
            'source_addon_id': self.source_addon_id,
            'owner_user_id': self.owner_user_id,
            'task_name': self.task_name,
            'workflow_run_id': self.workflow_run_id,
            'status': self.status,
            'progress': self.progress,
            'message': self.message,
            'error': self.error,
            'input': self.input_payload or {},
            'result': self.result_payload,
            'metadata': self.metadata_json or {},
            'queued_at': self.queued_at.isoformat() if self.queued_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'log_source_ids': self.log_source_ids or [],
        }


class RuntimeArtifact(db.Model):
    __tablename__ = 'runtime_artifact'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id = db.Column(db.String(64), nullable=True, index=True)
    workflow_run_id = db.Column(db.String(36), nullable=True, index=True)
    source_addon_id = db.Column(db.String(100), nullable=True, index=True)
    created_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    artifact_type = db.Column(db.String(50), nullable=False, default='file', index=True)
    uri = db.Column(EncryptedType(db.String(1024), strategy='user'), nullable=True)
    path = db.Column(EncryptedType(db.String(1024), strategy='user'), nullable=True)
    remote_reference = db.Column(EncryptedType(db.String(1024), strategy='user'), nullable=True)
    mime_type = db.Column(db.String(255), nullable=True)
    content_hash = db.Column(db.String(128), nullable=True, index=True)
    size_bytes = db.Column(db.BigInteger, nullable=True)
    label = db.Column(EncryptedType(db.String(255), strategy='user'), nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db, nullable=False)
    current_version_id = db.Column(db.String(36), nullable=True, index=True)
    review_status = db.Column(db.String(32), nullable=False, default='draft', index=True)
    reviewed_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    review_note = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    retention_policy = db.Column(db.String(100), nullable=False, default='default')
    download_permission = db.Column(db.String(50), nullable=False, default='owner')

    __table_args__ = (
        db.ForeignKeyConstraint(['job_id'], ['runtime_job.id'], name='fk_runtime_artifact_job_id_runtime_job'),
        db.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_artifact_workflow_run_id_workflow_run'),
        db.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_runtime_artifact_created_by_user_id_user'),
        db.ForeignKeyConstraint(['reviewed_by_user_id'], ['user.id'], name='fk_runtime_artifact_reviewed_by_user_id_user'),
        db.Index('ix_runtime_artifact_run_created_id', 'workflow_run_id', 'created_at', 'id'),
    )

    def to_dict(self):
        download_url = None
        if self.download_permission != 'none' and (self.uri or '').startswith('instance://'):
            download_url = f'/api/runtime/artifacts/{self.id}/download'
        return {
            'id': self.id,
            'job_id': self.job_id,
            'workflow_run_id': self.workflow_run_id,
            'source_addon_id': self.source_addon_id,
            'created_by_user_id': self.created_by_user_id,
            'type': self.artifact_type,
            'uri': self.uri,
            'path': self.path,
            'remote_reference': self.remote_reference,
            'mime_type': self.mime_type,
            'content_hash': self.content_hash,
            'size_bytes': self.size_bytes,
            'label': self.label,
            'metadata': self.metadata_json or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'current_version_id': self.current_version_id,
            'review_status': self.review_status,
            'reviewed_by_user_id': self.reviewed_by_user_id,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'review_note': self.review_note,
            'retention_policy': self.retention_policy,
            'download_permission': self.download_permission,
            'download_url': download_url,
        }


class RuntimeArtifactVersion(db.Model):
    __tablename__ = 'runtime_artifact_version'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    artifact_id = db.Column(db.String(36), nullable=False, index=True)
    version_number = db.Column(db.Integer, nullable=False)
    job_id = db.Column(db.String(64), nullable=True, index=True)
    workflow_run_id = db.Column(db.String(36), nullable=True, index=True)
    step_id = db.Column(db.String(36), nullable=True, index=True)
    source_addon_id = db.Column(db.String(100), nullable=True, index=True)
    created_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    artifact_type = db.Column(db.String(50), nullable=False, default='file', index=True)
    uri = db.Column(EncryptedType(db.String(1024), strategy='user'), nullable=True)
    path = db.Column(EncryptedType(db.String(1024), strategy='user'), nullable=True)
    remote_reference = db.Column(EncryptedType(db.String(1024), strategy='user'), nullable=True)
    mime_type = db.Column(db.String(255), nullable=True)
    content_hash = db.Column(db.String(128), nullable=True, index=True)
    size_bytes = db.Column(db.BigInteger, nullable=True)
    label = db.Column(EncryptedType(db.String(255), strategy='user'), nullable=True)
    source_ids = db.Column(db.JSON, nullable=True)
    evidence_ids = db.Column(db.JSON, nullable=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    change_summary = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('artifact_id', 'version_number', name='uq_runtime_artifact_version_artifact_version_number'),
        db.ForeignKeyConstraint(['artifact_id'], ['runtime_artifact.id'], name='fk_runtime_artifact_version_artifact_id_runtime_artifact'),
        db.ForeignKeyConstraint(['job_id'], ['runtime_job.id'], name='fk_runtime_artifact_version_job_id_runtime_job'),
        db.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_artifact_version_workflow_run_id_workflow_run'),
        db.ForeignKeyConstraint(['step_id'], ['workflow_step.id'], name='fk_runtime_artifact_version_step_id_workflow_step'),
        db.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_runtime_artifact_version_created_by_user_id_user'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'artifact_id': self.artifact_id,
            'version_number': self.version_number,
            'job_id': self.job_id,
            'workflow_run_id': self.workflow_run_id,
            'step_id': self.step_id,
            'source_addon_id': self.source_addon_id,
            'created_by_user_id': self.created_by_user_id,
            'type': self.artifact_type,
            'uri': self.uri,
            'path': self.path,
            'remote_reference': self.remote_reference,
            'mime_type': self.mime_type,
            'content_hash': self.content_hash,
            'size_bytes': self.size_bytes,
            'label': self.label,
            'source_ids': self.source_ids or [],
            'evidence_ids': self.evidence_ids or [],
            'metadata': self.metadata_json or {},
            'change_summary': self.change_summary,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class RuntimeSourceRecord(db.Model):
    __tablename__ = 'runtime_source_record'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_run_id = db.Column(db.String(36), nullable=False, index=True)
    step_id = db.Column(db.String(36), nullable=True, index=True)
    source_type = db.Column(db.String(50), nullable=False, index=True)
    uri = db.Column(EncryptedType(db.String(2048), strategy='user'), nullable=False)
    canonical_uri = db.Column(db.String(2048), nullable=True, index=True)
    provider = db.Column(db.String(100), nullable=True, index=True)
    external_id = db.Column(EncryptedType(db.String(512), strategy='user'), nullable=True)
    title = db.Column(EncryptedType(db.String(512), strategy='user'), nullable=True)
    summary = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    content_hash = db.Column(db.String(128), nullable=True, index=True)
    access_scope = db.Column(db.String(50), nullable=False, default='private')
    created_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)
    accessed_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    __table_args__ = (
        db.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_source_workflow_run_id_workflow_run'),
        db.ForeignKeyConstraint(['step_id'], ['workflow_step.id'], name='fk_runtime_source_step_id_workflow_step'),
        db.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_runtime_source_created_by_user_id_user'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'workflow_run_id': self.workflow_run_id,
            'step_id': self.step_id,
            'type': self.source_type,
            'uri': self.uri,
            'provider': self.provider,
            'external_id': self.external_id,
            'title': self.title,
            'summary': self.summary,
            'content_hash': self.content_hash,
            'access_scope': self.access_scope,
            'created_by_user_id': self.created_by_user_id,
            'metadata': self.metadata_json or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class RuntimeEvidenceRecord(db.Model):
    __tablename__ = 'runtime_evidence_record'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_run_id = db.Column(db.String(36), nullable=False, index=True)
    step_id = db.Column(db.String(36), nullable=True, index=True)
    source_id = db.Column(db.String(36), nullable=False, index=True)
    artifact_id = db.Column(db.String(36), nullable=True, index=True)
    claim = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    quote = db.Column(EncryptedType(db.Text, strategy='user'), nullable=True)
    citation = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    relevance_score = db.Column(db.Float, nullable=True)
    novelty_score = db.Column(db.Float, nullable=True)
    used_in_artifact = db.Column(db.Boolean, default=False, nullable=False)
    created_by_user_id = db.Column(db.Integer, nullable=True, index=True)
    metadata_json = db.Column(EncryptedType(db.JSON, strategy='user'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db, nullable=False)

    __table_args__ = (
        db.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_evidence_workflow_run_id_workflow_run'),
        db.ForeignKeyConstraint(['step_id'], ['workflow_step.id'], name='fk_runtime_evidence_step_id_workflow_step'),
        db.ForeignKeyConstraint(['source_id'], ['runtime_source_record.id'], name='fk_runtime_evidence_source_id_runtime_source_record'),
        db.ForeignKeyConstraint(['artifact_id'], ['runtime_artifact.id'], name='fk_runtime_evidence_artifact_id_runtime_artifact'),
        db.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_runtime_evidence_created_by_user_id_user'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'workflow_run_id': self.workflow_run_id,
            'step_id': self.step_id,
            'source_id': self.source_id,
            'artifact_id': self.artifact_id,
            'claim': self.claim,
            'quote': self.quote,
            'citation': self.citation or {},
            'relevance_score': self.relevance_score,
            'novelty_score': self.novelty_score,
            'used_in_artifact': self.used_in_artifact,
            'created_by_user_id': self.created_by_user_id,
            'metadata': self.metadata_json or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class McpAccessKey(db.Model):
    """Owner-scoped bearer credential for the host MCP product gateway.

    Only an installation-keyed lookup digest is retained.  The raw bearer
    value is generated and returned by the issuance service once, then is
    intentionally absent from this record and its serializers.
    """

    __tablename__ = "mcp_access_key"

    key_id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = db.Column(db.Integer, nullable=False, index=True)
    label = db.Column(
        EncryptedType(db.String(255), strategy="user"), nullable=False
    )
    token_digest = db.Column(db.String(128), nullable=False, unique=True, index=True)
    scopes = db.Column(db.JSON, nullable=False)
    allowed_roots = db.Column(
        EncryptedType(db.JSON, strategy="user"), nullable=False
    )
    response_default_bytes = db.Column(
        db.Integer, nullable=False, default=8 * 1024 * 1024,
        server_default=str(8 * 1024 * 1024),
    )
    response_max_bytes = db.Column(
        db.Integer, nullable=False, default=64 * 1024 * 1024,
        server_default=str(64 * 1024 * 1024),
    )
    status = db.Column(
        db.String(16), nullable=False, default="active", server_default="active", index=True
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now_db)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["owner_user_id"], ["user.id"], ondelete="CASCADE",
            name="fk_mcp_access_key_owner",
        ),
        db.CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_mcp_access_key_status"
        ),
        db.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) "
            "OR (status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_mcp_access_key_revocation",
        ),
        db.CheckConstraint(
            "response_default_bytes BETWEEN 65536 AND 67108864",
            name="ck_mcp_access_key_response_default_bounds",
        ),
        db.CheckConstraint(
            "response_max_bytes BETWEEN 65536 AND 67108864 "
            "AND response_default_bytes <= response_max_bytes",
            name="ck_mcp_access_key_response_max_bounds",
        ),
        db.Index(
            "ix_mcp_access_key_owner_created", "owner_user_id", "created_at"
        ),
    )


class McpExecution(db.Model):
    """Owner-scoped MCP domain execution, independent of protocol sessions."""

    __tablename__ = "mcp_execution"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_user_id = db.Column(db.Integer, nullable=False, index=True)
    addon_id = db.Column(db.String(32), nullable=False, index=True)
    resource_kind = db.Column(db.String(64), nullable=False)
    resource_id = db.Column(db.String(255), nullable=False)
    # Enqueue may happen after the execution record is created.  A one-time
    # conditional bind closes that otherwise ambiguous producer race.
    queue_job_id = db.Column(db.String(64), nullable=True, index=True)
    mode = db.Column(db.String(16), nullable=False, default="managed", index=True)
    status = db.Column(db.String(32), nullable=False, default="queued", index=True)
    request_json = db.Column(
        ContentType(db.JSON, strategy="user"), nullable=False
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now_db)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now_db, onupdate=utc_now_db
    )
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["owner_user_id"], ["user.id"], ondelete="CASCADE",
            name="fk_mcp_execution_owner",
        ),
        db.CheckConstraint(
            "addon_id IN ('doc_processor', 'translator', 'news_pipeline')",
            name="ck_mcp_execution_addon",
        ),
        db.CheckConstraint(
            "mode IN ('managed', 'passive')", name="ck_mcp_execution_mode"
        ),
        db.CheckConstraint(
            "status IN ('queued', 'running', 'waiting_external', 'waiting_review', "
            "'suspended', 'completed', 'failed', 'cancelled')",
            name="ck_mcp_execution_status",
        ),
        db.UniqueConstraint(
            "owner_user_id", "addon_id", "queue_job_id",
            name="uq_mcp_execution_owner_addon_queue_job",
        ),
        db.Index(
            "ix_mcp_execution_owner_status_created",
            "owner_user_id", "status", "created_at",
        ),
    )


class McpWorkItem(db.Model):
    """One independently leased, retryable model-work packet."""

    __tablename__ = "mcp_work_item"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    execution_id = db.Column(db.String(36), nullable=False, index=True)
    request_fingerprint = db.Column(db.String(64), nullable=False, index=True)
    request_json = db.Column(
        ContentType(db.JSON, strategy="user"), nullable=False
    )
    request_pruned_at = db.Column(db.DateTime(timezone=True), nullable=True)
    accepted_response_json = db.Column(
        ContentType(db.JSON, strategy="user"), nullable=True
    )
    batch_id = db.Column(db.String(64), nullable=True, index=True)
    dispatch_phase = db.Column(db.String(32), nullable=True, index=True)
    unit_index = db.Column(db.Integer, nullable=True)
    is_batch_terminal_unit = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )
    context_artifact_hash = db.Column(db.String(64), nullable=True)
    boundary_input_json = db.Column(
        ContentType(db.JSON, strategy="user"), nullable=True
    )
    state = db.Column(db.String(16), nullable=False, default="pending", index=True)
    lease_token = db.Column(db.String(36), nullable=True, index=True)
    lease_owner = db.Column(db.String(255), nullable=True, index=True)
    lease_expires_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    version = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    attempt_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now_db)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now_db, onupdate=utc_now_db
    )
    leased_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["execution_id"], ["mcp_execution.id"], ondelete="CASCADE",
            name="fk_mcp_work_item_execution",
        ),
        db.UniqueConstraint(
            "execution_id", "request_fingerprint", name="uq_mcp_work_item_request"
        ),
        db.CheckConstraint(
            "state IN ('pending', 'leased', 'completed', 'cancelled')",
            name="ck_mcp_work_item_state",
        ),
        db.CheckConstraint(
            "version >= 0 AND attempt_count >= 0", name="ck_mcp_work_item_counters"
        ),
        db.CheckConstraint(
            "(state = 'leased' AND lease_token IS NOT NULL AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR state != 'leased'",
            name="ck_mcp_work_item_lease_fields",
        ),
        db.CheckConstraint(
            "batch_id IS NULL OR length(batch_id) BETWEEN 1 AND 64",
            name="ck_mcp_work_item_batch_id_length",
        ),
        db.CheckConstraint(
            "dispatch_phase IS NULL OR length(dispatch_phase) BETWEEN 1 AND 32",
            name="ck_mcp_work_item_dispatch_phase_length",
        ),
        db.CheckConstraint(
            "unit_index IS NULL OR unit_index > 0",
            name="ck_mcp_work_item_unit_index_positive",
        ),
        db.CheckConstraint(
            "context_artifact_hash IS NULL OR "
            "(length(context_artifact_hash) = 64 AND "
            "context_artifact_hash NOT GLOB '*[^0-9a-f]*')",
            name="ck_mcp_work_item_context_artifact_hash",
        ),
        db.CheckConstraint(
            "is_batch_terminal_unit = false OR "
            "(batch_id IS NOT NULL AND dispatch_phase = 'page_extraction' "
            "AND unit_index IS NOT NULL)",
            name="ck_mcp_work_item_terminal_dispatch",
        ),
        db.Index(
            "ix_mcp_work_item_state_created", "state", "created_at", "id"
        ),
        db.Index(
            "ix_mcp_work_item_expired_lease", "lease_expires_at", "id",
            sqlite_where=db.text("state = 'leased' AND lease_expires_at IS NOT NULL"),
        ),
    )


class McpWorkClaimReceipt(db.Model):
    """Content-free receipt for one keyed MCP work-claim request."""

    __tablename__ = "mcp_work_claim_receipt"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    execution_id = db.Column(db.String(36), nullable=False, index=True)
    key_id = db.Column(db.String(36), nullable=False, index=True)
    request_id = db.Column(db.String(128), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    work_item_id = db.Column(db.String(36), nullable=False, index=True)
    attempt_count = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now_db)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["execution_id"], ["mcp_execution.id"], ondelete="CASCADE",
            name="fk_mcp_work_claim_receipt_execution",
        ),
        db.ForeignKeyConstraint(
            ["key_id"], ["mcp_access_key.key_id"], ondelete="CASCADE",
            name="fk_mcp_work_claim_receipt_key",
        ),
        db.ForeignKeyConstraint(
            ["work_item_id"], ["mcp_work_item.id"], ondelete="CASCADE",
            name="fk_mcp_work_claim_receipt_work_item",
        ),
        db.UniqueConstraint(
            "execution_id", "key_id", "request_id",
            name="uq_mcp_work_claim_receipt_request",
        ),
        db.CheckConstraint(
            "attempt_count > 0", name="ck_mcp_work_claim_receipt_attempt_positive"
        ),
    )


class McpWorkSubmission(db.Model):
    """Append-only record of every attempted MCP work result."""

    __tablename__ = "mcp_work_submission"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    work_item_id = db.Column(db.String(36), nullable=False, index=True)
    response_json = db.Column(
        EncryptedType(db.JSON, strategy="user"), nullable=False
    )
    response_fingerprint = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(16), nullable=False, index=True)
    error_code = db.Column(db.String(64), nullable=True)
    error_detail = db.Column(
        EncryptedType(db.Text, strategy="user"), nullable=True
    )
    submitted_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now_db
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["work_item_id"], ["mcp_work_item.id"], ondelete="CASCADE",
            name="fk_mcp_work_submission_work_item",
        ),
        db.CheckConstraint(
            "status IN ('accepted', 'rejected', 'duplicate')",
            name="ck_mcp_work_submission_status",
        ),
        db.CheckConstraint(
            "error_code IS NULL OR length(error_code) BETWEEN 1 AND 64",
            name="ck_mcp_work_submission_error_code",
        ),
        db.Index(
            "ix_mcp_work_submission_item_submitted",
            "work_item_id", "submitted_at", "id",
        ),
    )
