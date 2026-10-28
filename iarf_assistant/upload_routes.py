import os
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from iarf_assistant.core.upload_paths import upload_web_path, uploads_root

upload_blueprint = Blueprint('upload_api', __name__)


def get_user_uploads_dir(for_day=None):
    """Returns the current user's upload directory, optionally scoped to a day."""
    if not current_user.is_authenticated:
        return None

    base_uploads_dir = uploads_root()
    user_uploads_dir = base_uploads_dir / str(current_user.id)

    if for_day:
        day_string = for_day if isinstance(for_day, str) else for_day.strftime('%Y-%m-%d')
        final_dir = user_uploads_dir / day_string
    else:
        final_dir = user_uploads_dir

    final_dir.mkdir(parents=True, exist_ok=True)
    return final_dir


@upload_blueprint.route('/api/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    uploaded = request.files['file']
    if uploaded.filename == '':
        return jsonify({"error": "No selected file"}), 400

    filename = secure_filename(uploaded.filename)
    daily_dir = get_user_uploads_dir(for_day=datetime.now())
    file_path = daily_dir / filename
    if file_path.exists():
        base, ext = os.path.splitext(filename)
        filename = f"{base}_{uuid.uuid4().hex[:6]}{ext}"
        file_path = daily_dir / filename

    uploaded.save(file_path)

    web_path = upload_web_path(file_path)
    return jsonify({"success": True, "file_path": web_path, "filename": uploaded.filename})


def register_upload_blueprint(app):
    app.register_blueprint(upload_blueprint)
    app.add_url_rule('/api/upload', endpoint='upload_file', build_only=True)
