"""Process-local coordination between managed-worker spawn and restart."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Condition, RLock


class ManagedWorkerSpawnBlocked(RuntimeError):
    """Raised when a managed worker spawn races with restart quiescence."""


class ManagedWorkerQuiescenceAlreadyActive(RuntimeError):
    """Raised when another restart attempt owns the quiescence gate."""


class ManagedWorkerQuiescenceNotOwner(RuntimeError):
    """Raised when a caller tries to reopen another restart attempt's gate."""


class ManagedWorkerQuiescenceLease:
    """Opaque ownership token returned to the caller that starts quiescence."""


class ManagedWorkerQuiescenceGate:
    """Fence the final managed-worker spawn section during supervisor restart.

    The gate has an implicit draining state while ``begin_quiescence`` waits:
    workers already inside ``spawn_section`` are allowed to finish, while new
    entrants fail immediately.  Once the in-flight count reaches zero,
    quiescing is marked and restart can perform its process-tree scan.
    """

    def __init__(self) -> None:
        self._condition = Condition(RLock())
        self._accepting_spawns = True
        self._quiescing = False
        self._in_flight_spawns = 0
        self._owner: ManagedWorkerQuiescenceLease | None = None

    @contextmanager
    def spawn_section(self) -> Iterator[None]:
        """Guard the final preparation-to-``Popen`` worker section."""

        with self._condition:
            if not self._accepting_spawns:
                raise ManagedWorkerSpawnBlocked(
                    "Managed worker spawn rejected while restart quiescence "
                    "is in progress."
                )
            self._in_flight_spawns += 1
        try:
            yield
        finally:
            with self._condition:
                self._in_flight_spawns -= 1
                if self._in_flight_spawns == 0:
                    self._condition.notify_all()

    def begin_quiescence(self) -> ManagedWorkerQuiescenceLease:
        """Claim ownership, drain an entered spawn, then mark quiescing.

        The owner is recorded before waiting so a concurrent restart attempt
        cannot interfere while this caller drains an already-entered spawn.
        If the wait itself fails, this method restores open admission before
        propagating the failure; the caller never owns a partially-closed
        gate in that case.
        """

        with self._condition:
            if self._owner is not None:
                raise ManagedWorkerQuiescenceAlreadyActive(
                    "Managed worker restart quiescence is already owned by "
                    "another attempt."
                )
            owner = ManagedWorkerQuiescenceLease()
            self._owner = owner
            self._accepting_spawns = False
            try:
                while self._in_flight_spawns:
                    self._condition.wait()
                self._quiescing = True
            except BaseException:
                self._owner = None
                self._accepting_spawns = True
                self._quiescing = False
                self._condition.notify_all()
                raise
            return owner

    def reopen(self, owner: ManagedWorkerQuiescenceLease) -> None:
        """Reopen admission for the owning restart attempt only."""

        with self._condition:
            if owner is not self._owner:
                raise ManagedWorkerQuiescenceNotOwner(
                    "Only the active restart attempt may reopen worker admission."
                )
            self._owner = None
            self._accepting_spawns = True
            self._quiescing = False
            self._condition.notify_all()

    @property
    def quiescing(self) -> bool:
        """Whether restart has drained spawns and marked the process closed."""

        with self._condition:
            return self._quiescing

    @property
    def in_flight_spawns(self) -> int:
        """Return the number of final spawn sections currently in flight."""

        with self._condition:
            return self._in_flight_spawns

    @property
    def accepting_spawns(self) -> bool:
        """Whether a new final spawn section may be admitted."""

        with self._condition:
            return self._accepting_spawns

managed_worker_quiescence_gate = ManagedWorkerQuiescenceGate()


__all__ = [
    "ManagedWorkerQuiescenceAlreadyActive",
    "ManagedWorkerQuiescenceGate",
    "ManagedWorkerQuiescenceLease",
    "ManagedWorkerQuiescenceNotOwner",
    "ManagedWorkerSpawnBlocked",
    "managed_worker_quiescence_gate",
]
