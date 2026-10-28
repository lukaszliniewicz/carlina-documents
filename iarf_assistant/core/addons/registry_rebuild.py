"""Compilation of live add-on registries from immutable package state."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.exc import OperationalError

from ...extensions import db
from ...models import Addon
from ..addon_contracts import AddonRegistries

logger = logging.getLogger(__name__)


class AddonRegistryHost(Protocol):
    """Manager state required to compile the public add-on registries."""

    app: Any
    manifests: dict[str, dict[str, Any]]
    loaded_addons: dict[str, Any]
    compatibility_parser: Any
    docker_available: bool
    docker_status_checked: bool
    registries: AddonRegistries

    def get_addon_directory(self, addon_id: str) -> Path: ...

    def _apply_dependency_status(
        self,
        elements: Any,
        reasons: list[str],
    ) -> None: ...

    def _dependency_status_reasons(
        self,
        manifest: dict[str, Any],
        active_addon_ids: set[str],
    ) -> list[str]: ...

    def _apply_frontend_build_status(
        self,
        elements: Any,
        build_status: dict[str, Any],
    ) -> None: ...

    def get_frontend_build_status(self, addon_id: str) -> dict[str, Any]: ...


if TYPE_CHECKING:

    class _AddonRegistryRebuildMixinBase(AddonRegistryHost):
        pass

else:

    class _AddonRegistryRebuildMixinBase:
        pass


class AddonRegistryRebuildMixin(_AddonRegistryRebuildMixinBase):
    """Rebuild public registries without owning package lifecycle work."""

    def rebuild_registries(self) -> None:
        """Rebuild registries from manifests, runtimes, and personal skills."""
        self.registries = self._compile_registries()

    def _compile_registries(
        self,
        *,
        manifests: dict[str, dict[str, Any]] | None = None,
        loaded_addons: dict[str, Any] | None = None,
        addon_dirs: dict[str, Path] | None = None,
    ) -> AddonRegistries:
        """Compile a candidate projection without publishing manager state."""

        selected_manifests = self.manifests if manifests is None else manifests
        selected_loaded = self.loaded_addons if loaded_addons is None else loaded_addons
        registries = AddonRegistries()

        try:
            active_addon_ids = {
                addon.id for addon in Addon.query.filter_by(is_active=True).all()
            }
        except OperationalError:
            db.session.rollback()
            active_addon_ids = set(selected_loaded)
        except Exception as exc:  # noqa: BLE001 - registry rebuild is best effort.
            logger.warning(
                "Could not determine active addons for registry rebuild: %s",
                exc,
            )
            active_addon_ids = set(selected_loaded)

        for addon_id, manifest_data in sorted(selected_manifests.items()):
            manifest = manifest_data.get("manifest") or {}
            addon_dir = (
                addon_dirs.get(addon_id)
                if addon_dirs is not None and addon_id in addon_dirs
                else self.get_addon_directory(addon_id)
            )
            instance = selected_loaded.get(addon_id)
            try:
                elements = self.compatibility_parser.parse(
                    manifest,
                    instance=instance,
                    addon_dir=addon_dir,
                    is_active=addon_id in active_addon_ids,
                    docker_available=(
                        self.docker_available
                        if getattr(
                            self,
                            "docker_status_checked",
                            True,
                        )
                        else None
                    ),
                )
                if addon_id in active_addon_ids:
                    self._apply_dependency_status(
                        elements,
                        self._dependency_status_reasons(manifest, active_addon_ids),
                    )
                self._apply_frontend_build_status(
                    elements, self.get_frontend_build_status(addon_id)
                )
                registries.register_elements(elements)
            except Exception as exc:
                logger.warning(
                    "Failed to build compatibility records for addon '%s': %s",
                    addon_id,
                    exc,
                    exc_info=True,
                )

        personal_skills_dir = Path(self.app.instance_path) / "skills"
        for skill in self.compatibility_parser.discover_personal_skills(
            personal_skills_dir
        ):
            registries.skills.register(skill)

        registries.rebuild_capabilities()
        return registries


__all__ = ["AddonRegistryHost", "AddonRegistryRebuildMixin"]
