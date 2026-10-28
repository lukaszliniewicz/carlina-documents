"""Request-scoped settings snapshots with explicit invalidation."""

from __future__ import annotations

from typing import Any
import threading

from flask import g, has_app_context


_CACHE_ATTRIBUTE = "carlina_settings_snapshot"
_listener_lock = threading.Lock()
_listeners_configured = False


def get_settings_snapshot(*, refresh: bool = False) -> dict[str, Any]:
    """Return all global settings, querying at most once per app context."""

    if has_app_context() and not refresh:
        cached = getattr(g, _CACHE_ATTRIBUTE, None)
        if isinstance(cached, dict):
            return dict(cached)

    from iarf_assistant.models import Setting

    snapshot = {row.key: row.value for row in Setting.query.all()}
    if has_app_context():
        setattr(g, _CACHE_ATTRIBUTE, snapshot)
    return dict(snapshot)


def get_setting_value(key: str, default: Any = None) -> Any:
    return get_settings_snapshot().get(str(key), default)


def invalidate_settings_cache() -> None:
    """Discard the current context snapshot after any settings mutation."""

    if has_app_context():
        g.pop(_CACHE_ATTRIBUTE, None)


def _setting_changed(*_args: Any, **_kwargs: Any) -> None:
    invalidate_settings_cache()


def configure_settings_cache_invalidation() -> None:
    """Invalidate snapshots whenever SQLAlchemy persists a Setting mutation."""

    global _listeners_configured
    if _listeners_configured:
        return
    with _listener_lock:
        if _listeners_configured:
            return
        from sqlalchemy import event

        from iarf_assistant.models import Setting

        for event_name in ("after_insert", "after_update", "after_delete"):
            if not event.contains(Setting, event_name, _setting_changed):
                event.listen(Setting, event_name, _setting_changed)
        _listeners_configured = True


__all__ = [
    "get_setting_value",
    "get_settings_snapshot",
    "configure_settings_cache_invalidation",
    "invalidate_settings_cache",
]
