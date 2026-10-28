"""Small shared runtime port used by product MCP registrations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from flask import Flask

if TYPE_CHECKING:
    from .mcp_jobs import McpPrincipal


class InAppRunner(Protocol):
    """Run a synchronous product callback in the host application context."""

    def __call__[Result](
        self, app: Flask, function: Callable[[], Result]
    ) -> Awaitable[Result]: ...


@dataclass(frozen=True, slots=True)
class McpToolRuntime:
    """Host services exposed to a product MCP registration module."""

    app: Flask
    authorize: Callable[[str], McpPrincipal]
    in_app: InAppRunner
    failure: Callable[[BaseException], Exception]

    async def run[Result](self, function: Callable[[], Result]) -> Result:
        """Run one synchronous callback through the host's context adapter."""

        return await self.in_app(self.app, function)
