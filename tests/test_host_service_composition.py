"""Fresh-process acceptance of application-owned host service boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

_BOOT_PROBE = """
import importlib.abc
import json
import os
import sys

profile = sys.argv[1]
if profile == 'documents':
    class ForbidOptionalServices(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in {
                'iarf_assistant.core.client_api.device_auth',
                'iarf_assistant.core.client_api.principal',
                'iarf_assistant.core.addon_manager',
                'iarf_assistant.core.addons.container_orchestration',
                'iarf_assistant.core.addons.container_runtime',
                'iarf_assistant.rag_service',
                'iarf_assistant.maintenance',
                'iarf_assistant.knowledge.routes',
                'iarf_assistant.knowledge.worker',
                'iarf_assistant.core.context_fabric',
                'iarf_assistant.core.collection_service',
                'iarf_assistant.core.workflow_executor',
                'iarf_assistant.core.worker_process',
                'iarf_assistant.core.run_orchestrator',
                'iarf_assistant.core.runs.compiler',
                'iarf_assistant.core.code_session_service',
                'iarf_assistant.core.code_session_sdk',
                'iarf_assistant.core.code_session_bootstrap',
                'iarf_assistant.core.project_workspace_service',
                'iarf_assistant.core.project_process_supervisor',
                'iarf_assistant.core.runtime_interactions',
                'iarf_assistant.core.finalization_runner',
                'iarf_assistant.core.turn_finalization',
                'iarf_assistant.core.workflow_worker',
                'iarf_assistant.core.addons.audio_spoke',
                'iarf_assistant.core.mcp_news',
                'iarf_assistant.knowledge.document_parser',
                'iarf_assistant.administration_frontend',
                'iarf_assistant.automation_frontend',
                'iarf_assistant.context_frontend',
                'iarf_assistant.discovery_routes',
                'iarf_assistant.knowledge_frontend',
                'iarf_assistant.profile_routes',
                'iarf_assistant.run_frontend',
                'iarf_assistant.runtime_operations_frontend',
                'iarf_assistant.runtime_routes',
                'iarf_assistant.system_routes',
                'iarf_assistant.upload_routes',
            } or fullname.startswith((
                'iarf_assistant.chat_',
                'iarf_assistant.core.chat_sessions',
                'iarf_assistant.core.chat_turns',
                'iarf_assistant.core.workflow_steps',
            )) or fullname == 'docker' or fullname.startswith('docker.'):
                raise AssertionError('Documents loaded an optional host service: '
                    + fullname)
    sys.meta_path.insert(0, ForbidOptionalServices())

from iarf_assistant import bootstrap
assert bootstrap.setup_main([
    '--application-profile', profile,
    '--skip-install', '--skip-tailwind', '--skip-frontend', '--no-admin-prompt',
]) == 0

os.environ['CARLINA_RUNTIME_ROLE'] = 'web'
from carlina import create_app
app = create_app(profile=profile)
extensions = sorted(key for key in app.extensions if 'compass_client' in key)
modules = sorted(key for key in sys.modules if key.startswith(
    'iarf_assistant.core.client_api.'))
host_module_count = sum(name.startswith(('carlina.', 'iarf_assistant.'))
    for name in sys.modules)
container_modules = sorted(name for name in sys.modules if name in {
    'iarf_assistant.core.addon_manager',
    'iarf_assistant.core.addons.container_orchestration',
    'iarf_assistant.core.addons.container_runtime',
} or name == 'docker' or name.startswith('docker.'))
manager = app.addon_manager
client = app.test_client()
print(json.dumps({
    'extensions': extensions,
    'modules': modules,
    'host_module_count': host_module_count,
    'container_modules': container_modules,
    'manager_class': type(manager).__name__,
    'container_backend_state': any(hasattr(manager, name) for name in (
        '_container_lock', '_addon_container_locks', '_docker_status_lock',
        '_docker_status_ttl_seconds')),
    'ready': client.get('/health/ready').status_code,
    'login': client.get('/login').status_code,
    'mcp_key_cli': 'mcp' in app.cli.commands,
    'assistant_cli': sorted({'rebuild-rag-index', 'import-news-data'}
        & app.cli.commands.keys()),
    'capture_routes': sorted(str(rule) for rule in app.url_map.iter_rules()
        if str(rule).startswith('/api/compass/client/v1/')),
}))
"""


@pytest.mark.integration
@pytest.mark.parametrize("profile", ["documents", "workbench"])
def test_profile_owns_optional_services_in_fresh_process(
    tmp_path: Path, profile: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CARLINA_", "IARF_", "FLASK_", "GOOGLE_"))
    }
    environment.update(
        CARLINA_HOME=str(tmp_path / "fresh-root"),
        CARLINA_PROFILE=profile,
        CARLINA_PROGRAM_ROOT=str(root),
        CARLINA_ADDON_ENVIRONMENTS_DIR=str(tmp_path / "worker-environments"),
        CARLINA_RUNTIME_ROLE="command",
        PYTHONNOUSERSITE="1",
    )
    result = subprocess.run(
        [sys.executable, "-c", _BOOT_PROBE, profile],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    report = cast(dict[str, object], json.loads(result.stdout.splitlines()[-1]))
    assert report["ready"] == 200
    assert report["login"] == 200
    assert report["mcp_key_cli"] is True
    if profile == "documents":
        policy = cast(
            dict[str, object],
            json.loads(
                (root / "policy/documents-closure.json").read_text(encoding="utf-8")
            ),
        )
        assert cast(int, report["host_module_count"]) <= cast(
            int, policy["fresh_documents_host_module_limit"]
        )
        assert report["extensions"] == []
        assert report["modules"] == []
        assert report["container_modules"] == []
        assert report["manager_class"] == "NativeAddonManager"
        assert report["container_backend_state"] is False
        assert report["capture_routes"] == []
        assert report["assistant_cli"] == []
    else:
        assert report["manager_class"] == "AddonManager"
        assert report["container_backend_state"] is True
        assert {
            "iarf_assistant.core.addon_manager",
            "iarf_assistant.core.addons.container_orchestration",
            "iarf_assistant.core.addons.container_runtime",
        } <= set(cast(list[str], report["container_modules"]))
        assert report["extensions"] == [
            "carlina.compass_client_auth_service",
            "carlina.compass_client_principal_resolver",
            "carlina.compass_client_token_limiter",
        ]
        assert "iarf_assistant.core.client_api.device_auth" in cast(
            list[str], report["modules"]
        )
        assert report["capture_routes"]
        assert report["assistant_cli"] == ["import-news-data", "rebuild-rag-index"]
