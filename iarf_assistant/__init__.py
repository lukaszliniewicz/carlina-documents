def create_app():
    """Creates and configures the Flask application."""
    from iarf_assistant.app import _create_app

    return _create_app()
