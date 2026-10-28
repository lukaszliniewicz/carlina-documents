import copy

from carlina.addons.api import AddonBase
from carlina.runtime_environment import is_worker_process


class DocProcessorAddon(AddonBase):
    def __init__(self, context, manifest):
        # The package can be inspected before its host dependency lock is
        # materialized. Import host-only modules only once activation reaches
        # the loader, which has already added that environment to sys.path.
        from .api_client import DocProcessorClient
        from .routes import blueprint

        super().__init__(context, manifest)
        self.prefix = self.id
        self.api_client_class = DocProcessorClient

        self.blueprint = blueprint
        self.blueprint.addon_context = context

    def get_preserved_models(self) -> list:
        from .models import (
            DocumentMetadata,
            DocumentProject,
            ProcessedDocument,
            ProjectDocument,
            ProjectRun,
            ProjectRunDocument,
        )

        return [
            ProcessedDocument,
            DocumentMetadata,
            DocumentProject,
            ProjectDocument,
            ProjectRun,
            ProjectRunDocument,
        ]

    def on_activate(self):
        super().on_activate()

    def on_register_jobs(self, scheduler):
        if is_worker_process():
            return
        self.context.ensure_worker_pool("worker.py", num_workers=1)

    def initialize_client(self, config):
        return self.api_client_class(self.context, config)

    def preflight_passive_completion(self, *, request, response):
        """Validate only the exact leased page-batch response envelope."""

        from .passive_preflight import preflight_completion

        return preflight_completion(request, response)

    def iter_extractor_candidates(self, *, include_inactive=False):
        """Return compatible document extractors for catalog surfaces."""

        return self.context.capability_candidates(
            "extractors",
            include_inactive=include_inactive,
        )

    def _parse_document_for_tools(self, client, *args, **kwargs):
        import re

        from .format_utils import (
            html_to_markdown_on_the_fly,
            markdown_to_html_on_the_fly,
        )

        result = client.parse_document(*args, **kwargs)
        if not result or (isinstance(result, str) and result.startswith("Error:")):
            return result

        raw_html = None
        raw_text = result

        if isinstance(result, tuple):
            if len(result) > 1 and isinstance(result[1], str):
                raw_html = result[1]
            else:
                raw_text = next(
                    (part for part in reversed(result) if isinstance(part, str)), ""
                )

        if raw_html:
            return html_to_markdown_on_the_fly(raw_html)

        if isinstance(raw_text, str) and re.search(
            r"<(?:main|html|body|article|section)\b", raw_text, re.IGNORECASE
        ):
            return html_to_markdown_on_the_fly(raw_text)

        return html_to_markdown_on_the_fly(markdown_to_html_on_the_fly(str(raw_text)))

    def get_tools(self):
        from .tools import _tools_def

        prefixed = copy.deepcopy(_tools_def)
        for tool in prefixed:
            tool["function"]["name"] = f"{self.prefix}_{tool['function']['name']}"
        return prefixed

    def get_function_mapping(self, client=None):
        active_client = client or self.initialize_client({})

        def parse_document_for_tools(*args, **kwargs):
            return self._parse_document_for_tools(active_client, *args, **kwargs)

        return {f"{self.prefix}_parse_document": parse_document_for_tools}
