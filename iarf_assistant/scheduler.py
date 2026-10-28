import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR
from functools import wraps

# We use an in-memory job store because addons and their functions are dynamically 
# loaded. Storing function references in a database (SQLAlchemyJobStore) can cause 
# pickling errors if addons are uninstalled or code changes.
scheduler = BackgroundScheduler()
_flask_app_ref = None

def _job_error_listener(event):
    logging.error(f"Scheduled Job crashed: {event.job_id}", exc_info=event.exception)

def init_scheduler(app):
    """Initialize the scheduler without running jobs during model registration."""
    global _flask_app_ref
    _flask_app_ref = app  # Store a reference to the app
    
    scheduler.add_listener(_job_error_listener, EVENT_JOB_ERROR)
    scheduler.start(paused=True)
    logging.info("Background job scheduler initialized; jobs await model registration.")
    return scheduler

def with_app_context(func):
    """Decorator to easily run a background job within the Flask Application Context."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if _flask_app_ref:
            with _flask_app_ref.app_context():
                return func(*args, **kwargs)
        else:
            logging.error("Cannot run background job: Flask app context reference is missing.")
    return wrapper
