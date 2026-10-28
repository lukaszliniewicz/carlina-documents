import logging
import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from carlina.runtime_environment import mark_worker_environment

mark_worker_environment(os.environ)

from iarf_assistant.app import create_app
from iarf_assistant.core.task_queue import WorkerNode
from iarf_assistant.core.workflow_worker import handle_execute_workflow_step

logger = logging.getLogger("WorkflowWorker")
logger.setLevel(logging.INFO)

app = create_app()

worker = WorkerNode(app.instance_path, "core_workflow", idle_timeout=0)

@worker.handler("execute_workflow_step")
def execute_workflow_step_wrapper(payload: dict, task_queue, job_id: str):
    return handle_execute_workflow_step(payload, task_queue, job_id)

if __name__ == "__main__":
    logger.info("Starting Core Workflow Worker...")
    with app.app_context():
        worker.start()
