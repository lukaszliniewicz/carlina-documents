"""Persisted, journaled Add-on API v1 lifecycle state machine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

DISCOVERED = "discovered"
STAGED = "staged"
VERIFIED = "verified"
VALIDATED = "validated"
PREPARED = "prepared"
MIGRATED = "migrated"
ACTIVATED = "activated"
DEGRADED = "degraded"
FAILED = "failed"
DISABLING = "disabling"
DISABLED = "disabled"
REMOVAL_PENDING = "removal_pending"
CODE_REMOVED = "code_removed"
DATA_PRESERVED = "data_preserved"
PURGE_PENDING = "purge_pending"
DATA_PURGED = "data_purged"
ROLLBACK_PENDING = "rollback_pending"
ROLLED_BACK = "rolled_back"

LIFECYCLE_STATES = frozenset(
    {
        DISCOVERED,
        STAGED,
        VERIFIED,
        VALIDATED,
        PREPARED,
        MIGRATED,
        ACTIVATED,
        DEGRADED,
        FAILED,
        DISABLING,
        DISABLED,
        REMOVAL_PENDING,
        CODE_REMOVED,
        DATA_PRESERVED,
        PURGE_PENDING,
        DATA_PURGED,
        ROLLBACK_PENDING,
        ROLLED_BACK,
    }
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    DISCOVERED: frozenset({STAGED, VERIFIED, VALIDATED, FAILED, DISABLED}),
    STAGED: frozenset({VERIFIED, FAILED, ROLLBACK_PENDING}),
    VERIFIED: frozenset({VALIDATED, FAILED, ROLLBACK_PENDING}),
    VALIDATED: frozenset({PREPARED, DISABLED, FAILED, REMOVAL_PENDING}),
    PREPARED: frozenset({MIGRATED, ACTIVATED, FAILED, ROLLBACK_PENDING}),
    MIGRATED: frozenset({ACTIVATED, FAILED, ROLLBACK_PENDING}),
    ACTIVATED: frozenset({DISABLING, DEGRADED, FAILED}),
    DEGRADED: frozenset({PREPARED, ACTIVATED, DISABLING, FAILED}),
    FAILED: frozenset({VALIDATED, PREPARED, DISABLED, ROLLBACK_PENDING}),
    DISABLING: frozenset({DISABLED, DEGRADED, FAILED}),
    DISABLED: frozenset({PREPARED, REMOVAL_PENDING, PURGE_PENDING}),
    REMOVAL_PENDING: frozenset({CODE_REMOVED, DISABLED, ROLLBACK_PENDING}),
    CODE_REMOVED: frozenset({DATA_PRESERVED, PURGE_PENDING, STAGED}),
    DATA_PRESERVED: frozenset({PURGE_PENDING, STAGED}),
    PURGE_PENDING: frozenset({DATA_PURGED, DATA_PRESERVED, FAILED}),
    DATA_PURGED: frozenset({STAGED}),
    ROLLBACK_PENDING: frozenset({ROLLED_BACK, FAILED}),
    ROLLED_BACK: frozenset({VALIDATED, DISABLED}),
}


class AddonLifecycleError(RuntimeError):
    pass


class AddonLifecycleOwnershipError(AddonLifecycleError):
    """Raised when a non-owner process attempts a durable lifecycle mutation."""


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    addon_id: str
    from_state: str
    to_state: str
    generation: int
    changed: bool


class AddonLifecycleService:
    """Apply legal state transitions and write an append-only journal."""

    def __init__(self, session) -> None:
        self.session = session

    def transition(
        self,
        addon,
        to_state: str,
        *,
        event: str | None = None,
        detail: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        actor_user_id: int | None = None,
        commit: bool = True,
        force: bool = False,
    ) -> LifecycleTransition:
        from iarf_assistant.models import AddonLifecycleEvent

        target = str(to_state or "").strip()
        if target not in LIFECYCLE_STATES:
            raise AddonLifecycleError(f"Unknown add-on lifecycle state: {target!r}.")
        current = str(getattr(addon, "lifecycle_state", None) or DISCOVERED)
        generation = int(getattr(addon, "lifecycle_generation", 0) or 0)
        if current == target:
            return LifecycleTransition(addon.id, current, target, generation, False)
        if not force and target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise AddonLifecycleError(
                f"Illegal add-on lifecycle transition for '{addon.id}': "
                f"{current} -> {target}."
            )

        now = datetime.now(UTC)
        addon.lifecycle_state = target
        addon.lifecycle_generation = generation + 1
        addon.lifecycle_updated_at = now
        if target == FAILED:
            addon.lifecycle_error = str(detail or "Lifecycle operation failed.")
        elif target not in {DEGRADED}:
            addon.lifecycle_error = None
        addon.is_active = target == ACTIVATED
        journal = AddonLifecycleEvent()
        journal.addon_id = addon.id
        journal.from_state = current
        journal.to_state = target
        journal.event = str(event or target)
        journal.success = target != FAILED
        journal.detail = str(detail) if detail else None
        journal.metadata_json = dict(metadata or {})
        journal.actor_user_id = actor_user_id
        journal.generation = addon.lifecycle_generation
        journal.created_at = now
        self.session.add(addon)
        self.session.add(journal)
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return LifecycleTransition(
            addon.id,
            current,
            target,
            addon.lifecycle_generation,
            True,
        )

    def record_event(
        self,
        addon,
        *,
        event: str,
        success: bool = True,
        detail: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        actor_user_id: int | None = None,
        commit: bool = True,
    ) -> None:
        """Journal work that does not replace the currently active version."""

        from iarf_assistant.models import AddonLifecycleEvent

        state = str(getattr(addon, "lifecycle_state", None) or DISCOVERED)
        journal = AddonLifecycleEvent()
        journal.addon_id = addon.id
        journal.from_state = state
        journal.to_state = state
        journal.event = str(event)
        journal.success = bool(success)
        journal.detail = str(detail) if detail else None
        journal.metadata_json = dict(metadata or {})
        journal.actor_user_id = actor_user_id
        journal.generation = int(getattr(addon, "lifecycle_generation", 0) or 0)
        journal.created_at = datetime.now(UTC)
        self.session.add(journal)
        if commit:
            self.session.commit()
        else:
            self.session.flush()

    def fail(
        self,
        addon,
        *,
        event: str,
        error: BaseException | str,
        metadata: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> LifecycleTransition:
        return self.transition(
            addon,
            FAILED,
            event=event,
            detail=str(error),
            metadata=metadata,
            commit=commit,
            force=True,
        )
