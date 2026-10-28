from collections.abc import Callable
from functools import wraps
from typing import cast

from flask import Response, jsonify
from flask_login import current_user
from flask_login import login_required as _login_required


def login_required[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Keep Flask-Login's runtime boundary behind a typed decorator."""
    return cast("Callable[P, R]", _login_required(function))


def current_user_id() -> int:
    """Return the authenticated user's model ID within the request context."""
    return int(current_user.id)


def admin_required[**P, R](
    f: Callable[P, R],
) -> Callable[P, R | tuple[Response, int]]:
    """Requires the current user to be an authenticated administrator."""
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> R | tuple[Response, int]:
        if not current_user.is_authenticated or not current_user.is_admin:
            return jsonify({"error": "Administrator access required."}), 403
        return f(*args, **kwargs)

    return decorated_function
