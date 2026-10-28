from flask_compress import Compress
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from iarf_assistant.core.persistence.query_metrics import configure_query_metrics_engine
from iarf_assistant.core.persistence.sqlite import configure_sqlite_engine


class CarlinaSQLAlchemy(SQLAlchemy):
    """SQLAlchemy extension that scopes engine policy to Carlina apps."""

    def init_app(self, app):
        super().init_app(app)
        with app.app_context():
            configure_sqlite_engine(self.engine)
            configure_query_metrics_engine(self.engine)


db = CarlinaSQLAlchemy()
login_manager = LoginManager()
compress = Compress()
csrf = CSRFProtect()
