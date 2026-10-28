import logging

from iarf_assistant.extensions import db
from iarf_assistant.models import User
from iarf_assistant.core.runtime_state import create_workflow_run, create_workflow_step
from iarf_assistant.core.workflow_executor import WorkflowOrchestrator
from carlina.product_identity import product_environment_value


def memory_pruner_enabled(app) -> bool:
    raw_value = app.config.get("ENABLE_MEMORY_PRUNER")
    if raw_value is None:
        raw_value = product_environment_value(
            "ENABLE_MEMORY_PRUNER",
            default="false",
        )
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}

def trigger_memory_pruner_workflow(app):
    """Triggers a nightly workflow run to consolidate memory."""
    with app.app_context():
        try:
            if not memory_pruner_enabled(app):
                logging.debug("Memory pruner workflow trigger skipped because ENABLE_MEMORY_PRUNER is disabled.")
                return

            admin_user = User.query.filter_by(is_admin=True).first() or User.query.first()
            if not admin_user:
                return

            run = create_workflow_run(
                title="Nightly Memory Pruning",
                kind="ad_hoc",
                status="running",
                created_by_user_id=admin_user.id,
                understanding={
                    "task": "System maintenance task: consolidate redundant and obsolete memory facts."
                },
            )

            instructions = (
                "You are the System Maintenance Agent. Your task is to query the long-term UserMemory and OrgMemory, "
                "identify redundant or obsolete facts, and consolidate them using the memorize_fact, recall_fact, and manage_long_term_memory tools, "
                "and produce a concise maintenance report artifact with findings and recommended cleanup actions. "
                "Treat this as best effort: if some sources or tools are unavailable, continue with partial findings and note limitations."
            )

            create_workflow_step(
                workflow_run_id=run.id,
                step_type="maintenance",
                title="Prune Memories",
                instructions=instructions,
                status="planned",
                assigned_model_role="powerful",
                created_by_user_id=admin_user.id,
                input_payload={},
            )

            orchestrator = WorkflowOrchestrator(app)
            try:
                orchestrator.poll_and_dispatch()
            finally:
                orchestrator.close()
            logging.info(f"Queued nightly memory pruner workflow (Run ID: {run.id})")
        except Exception as exc:
            db.session.rollback()
            logging.error(f"Failed to trigger memory pruner workflow: {exc}")
