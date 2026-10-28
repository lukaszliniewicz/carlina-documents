import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from carlina.addons.api import AddonBase
from carlina.runtime_environment import is_worker_process
from iarf_assistant.core.addons.capability_runtime import CapabilityUseError
from iarf_assistant.extensions import db

from .api_client import TranslatorClient
from .embedding_reclaimer import reconcile_embedding_vector_tables
from .operations import reap_expired_operation_leases
from .routes import blueprint
from .source_providers import resolve_capability_provider, resolve_source_provider

logger = logging.getLogger(__name__)


class TranslatorAddon(AddonBase):
    def __init__(self, context, manifest):
        super().__init__(context, manifest)
        self.blueprint = blueprint
        self.blueprint.url_prefix = f"/addons/{self.id}"
        self.blueprint.addon_context = context

    def preflight_passive_completion(self, *, request, response):
        """Check the leased transport without loading the anthology or writing data."""
        from .passive_preflight import preflight_completion
        return preflight_completion(request, response)

    def initialize_client(self, config):
        user_id = self._capability_user_id(config)

        def document_provider(reference):
            return resolve_source_provider(
                self.context,
                requirement_id="document_processor",
                capability="document.processing",
                user_id=user_id,
                reference=reference,
            )

        def subtitle_provider(reference):
            return resolve_source_provider(
                self.context,
                requirement_id="subtitle_source",
                capability="subtitles.authoring",
                user_id=user_id,
                reference=reference,
            )

        def semantic_export_provider(reference):
            return resolve_capability_provider(
                self.context,
                requirement_id="semantic_export",
                capability="document.semantic_export",
                user_id=user_id,
                settings=(
                    {"semantic_export_reference": reference} if reference else {}
                ),
                setting_key="semantic_export_reference",
            )

        def sentence_segmenter():
            try:
                return self.initialize_sentence_segmenter(config)
            except CapabilityUseError, LookupError, RuntimeError:
                return None

        return TranslatorClient(
            self.context,
            config,
            document_provider_factory=document_provider,
            semantic_export_provider_factory=semantic_export_provider,
            subtitle_provider_factory=subtitle_provider,
            sentence_segmenter_factory=sentence_segmenter,
        )

    def initialize_sentence_segmenter(self, config):
        """Resolve the optional sentence segmentation provider for one user."""

        child_config = self.child_capability_config(
            "sentence_segmentation",
            config,
            connection_id=None,
        )
        return self.initialize_capability_client(
            "sentence_segmentation",
            child_config,
        )

    def get_preserved_models(self) -> list:
        from .models import (
            GlossaryTerm,
            TranslationAlignmentGroup,
            TranslationAlignmentRevision,
            TranslationAnalysisAttempt,
            TranslationAnalysisAttemptEvent,
            TranslationBlock,
            TranslationBlockReviewHead,
            TranslationBlockReviewRevision,
            TranslationContextArtifact,
            TranslationContextArtifactRevision,
            TranslationContextBaseline,
            TranslationContextBaselineItem,
            TranslationContextImpact,
            TranslationEmbeddingActive,
            TranslationEmbeddingEntry,
            TranslationEmbeddingGeneration,
            TranslationGlossaryActive,
            TranslationGlossaryConcept,
            TranslationGlossaryOccurrence,
            TranslationGlossaryRevision,
            TranslationGlossarySuggestion,
            TranslationMemoryEntry,
            TranslationNote,
            TranslationNumberingOverlay,
            TranslationOperation,
            TranslationProject,
            TranslationProjectGlossaryBinding,
            TranslationReaderNote,
            TranslationReaderNoteHead,
            TranslationReaderNoteRevision,
            TranslationResearchDossier,
            TranslationResearchDossierGlossaryProposal,
            TranslationResearchDossierRevision,
            TranslationResearchRun,
            TranslationResearchToolCall,
            TranslationResearchToolCallEvent,
            TranslationResearchToolResultBlob,
            TranslationResearchTurn,
            TranslationRevision,
            TranslationSegment,
            TranslationSharedGlossary,
            TranslationSharedGlossaryProposal,
            TranslationSharedGlossaryReceipt,
            TranslationSharedGlossaryRevision,
            TranslationSemanticCensus,
            TranslationSemanticOccurrence,
            TranslationSourceAnchor,
            TranslationSourcePlan,
            TranslationShareGrant,
            TranslationShareSession,
            TranslationTargetAlignmentActive,
            TranslationTargetBlockActive,
            TranslationTargetBlockNote,
            TranslationTargetBlockRevision,
            TranslationTargetRevisionShareAttribution,
            TranslationTargetSpan,
            TranslationTargetSpanSet,
            TranslationRetranslationRequest,
            TranslationWindowAttempt,
            TranslationWindowAttemptEvent,
            TranslationWindowAttemptRevision,
        )

        return [
            TranslationProject,
            TranslationProjectGlossaryBinding,
            TranslationRevision,
            TranslationSegment,
            TranslationNote,
            TranslationNumberingOverlay,
            GlossaryTerm,
            TranslationOperation,
            TranslationSourcePlan,
            TranslationBlock,
            TranslationSourceAnchor,
            TranslationTargetBlockRevision,
            TranslationTargetBlockNote,
            TranslationTargetBlockActive,
            TranslationTargetRevisionShareAttribution,
            TranslationTargetSpanSet,
            TranslationTargetSpan,
            TranslationAlignmentRevision,
            TranslationAlignmentGroup,
            TranslationTargetAlignmentActive,
            TranslationWindowAttempt,
            TranslationWindowAttemptEvent,
            TranslationWindowAttemptRevision,
            TranslationSemanticCensus,
            TranslationSemanticOccurrence,
            TranslationContextArtifact,
            TranslationContextArtifactRevision,
            TranslationGlossaryConcept,
            TranslationGlossaryRevision,
            TranslationGlossaryActive,
            TranslationGlossaryOccurrence,
            TranslationGlossarySuggestion,
            TranslationSharedGlossary,
            TranslationSharedGlossaryRevision,
            TranslationSharedGlossaryProposal,
            TranslationSharedGlossaryReceipt,
            TranslationContextBaseline,
            TranslationContextBaselineItem,
            TranslationContextImpact,
            TranslationResearchDossier,
            TranslationResearchDossierRevision,
            TranslationResearchDossierGlossaryProposal,
            TranslationReaderNote,
            TranslationReaderNoteRevision,
            TranslationReaderNoteHead,
            TranslationShareGrant,
            TranslationShareSession,
            TranslationBlockReviewRevision,
            TranslationBlockReviewHead,
            TranslationRetranslationRequest,
            TranslationEmbeddingGeneration,
            TranslationEmbeddingEntry,
            TranslationEmbeddingActive,
            TranslationAnalysisAttempt,
            TranslationAnalysisAttemptEvent,
            TranslationResearchRun,
            TranslationResearchTurn,
            TranslationResearchToolCall,
            TranslationResearchToolResultBlob,
            TranslationResearchToolCallEvent,
            TranslationMemoryEntry,
        ]

    def on_activate(self):
        super().on_activate()
        # Worker startup remains scheduler-owned to prevent duplicate pools on UI activation.
        # AddonManager automatically invokes on_register_jobs immediately after this.

    def on_register_jobs(self, scheduler):
        # Prevent fork bombs: do not spawn workers if we are ALREADY inside a worker process
        if is_worker_process():
            return

        # We ensure the workers run on app startup if the addon is active
        self.context.ensure_worker_pool("worker.py", num_workers=3)
        if scheduler is None:
            return

        # Vector roots are rebuildable projections.  Reconcile once after the
        # scheduler starts and then at a low frequency; the relational
        # generation rows remain the authority.  These jobs run in the web
        # scheduler and deliberately do not enter the Translator worker pool.
        scheduler.add_job(
            reconcile_embedding_vector_tables,
            "date",
            run_date=datetime.now(UTC),
            args=[self.context.application],
            id=f"{self.id}_embedding_reconcile_startup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            reconcile_embedding_vector_tables,
            "interval",
            hours=1,
            args=[self.context.application],
            id=f"{self.id}_embedding_reconcile_periodic",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # Expired worker leases must not hold the single-flight fence or leave
        # a cancelled operation stuck in cancelling.  The sweep is metadata
        # capped, short-transaction bounded, and stops on busy instead of
        # retrying every candidate.  IDs use the translator_ prefix so host
        # deactivation/removal cleanup removes them; no custom teardown hook
        # is needed.  Jobs run in the web scheduler, never the worker pool,
        # and the existing worker-process guard above prevents recursion.
        scheduler.add_job(
            reap_expired_operation_leases,
            "date",
            run_date=datetime.now(UTC),
            args=[self.context.application],
            id=f"{self.id}_operation_lease_reap_startup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            reap_expired_operation_leases,
            "interval",
            minutes=1,
            args=[self.context.application],
            id=f"{self.id}_operation_lease_reap_periodic",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    def on_rag_rebuild_triggered(self):
        """Drops isolated TM tables when global embeddings are rebuilt to prevent dimension crashes."""
        from .models import TranslationSegment

        try:
            with db.engine.connect() as conn, conn.begin():
                tables = conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' AND "
                        "(name LIKE 'vec_translator_tm_%' OR "
                        "name LIKE 'fts_vec_translator_tm_%');"
                    )
                ).fetchall()
                for (table_name,) in tables:
                    quoted_name = conn.dialect.identifier_preparer.quote(table_name)
                    conn.execute(text(f"DROP TABLE IF EXISTS {quoted_name};"))
                    logger.info("Dropped translator TM vector table: %s", table_name)

            db.session.query(TranslationSegment).update(
                {TranslationSegment.vec_id: None}
            )
            db.session.commit()
        except SQLAlchemyError:
            logger.exception("Failed to handle RAG rebuild in Translator add-on")
