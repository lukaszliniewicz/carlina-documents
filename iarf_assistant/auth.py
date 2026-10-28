from flask import (
    Blueprint,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from iarf_assistant.core.encryption import (
    derive_legacy_user_key,
    revoke_legacy_user_key,
    store_legacy_user_key,
)
from iarf_assistant.models import User

auth_blueprint = Blueprint('auth', __name__)
_DUMMY_PASSWORD_HASH = (
    "scrypt:32768:8:1$0UKpBcUIMBonNJDu$"
    "28b492af5f3b7fcd1840fbe79d025027fa2cd57d1bcdf801ea43db6885c7f3208"
    "b90778d6c4f65a3a420e07da7055766f163a8fba86b8d278bdf0687383dcca0"
)


def _login_failure(*, status: int = 401, retry_after: int = 0):
    flash('Invalid email or password.')
    response = make_response(render_template('login.html'), status)
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


@auth_blueprint.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = str(request.form.get('email') or '').strip()
        password = str(request.form.get('password') or '')
        source = request.remote_addr or "unknown"
        limiter = current_app.extensions["carlina_login_limiter"]
        decision = limiter.check(email, source)
        if not decision.allowed:
            return _login_failure(status=429, retry_after=decision.retry_after)
        user = User.query.filter_by(email=email).first()
        password_valid = (
            user.check_password(password)
            if user is not None
            else check_password_hash(_DUMMY_PASSWORD_HASH, password)
        )
        if user is not None and password_valid:
            limiter.record_success(email, source)
            session.clear()
            login_user(user)
            session.permanent = True

            key = derive_legacy_user_key(password, user.password_hash)
            session['encryption_key'] = store_legacy_user_key(key, user.id)

            return redirect(url_for('index'))
        failure = limiter.record_failure(email, source)
        return _login_failure(
            status=429 if failure.retry_after else 401,
            retry_after=failure.retry_after,
        )
    return render_template('login.html')


@auth_blueprint.route('/logout', methods=['POST'])
@login_required
def logout():
    revoke_legacy_user_key(session.get('encryption_key'))
    logout_user()
    session.clear()
    return redirect(url_for('login'))


def register_auth_blueprint(app):
    app.register_blueprint(auth_blueprint)
    app.add_url_rule('/login', endpoint='login', build_only=True)
    app.add_url_rule('/logout', endpoint='logout', build_only=True)
