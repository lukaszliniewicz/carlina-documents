from flask import Blueprint, request, jsonify
from flask_login import login_required

blueprint = Blueprint(
    "sentence_segmenter",
    __name__,
    static_folder="static",
)

@blueprint.route("/api/split", methods=["POST"])
@login_required
def api_test_split():
    """Proxy endpoint for the UI to test the segmenter."""
    data = request.json or {}
    text = data.get("text", "")
    
    addon = blueprint.addon_context.get_addon("sentence_segmenter")
    if not addon:
        return jsonify({"error": "Addon not loaded"}), 500
        
    try:
        sentences = addon.split_text(text)
        return jsonify({"sentences": sentences})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@blueprint.route("/api/health", methods=["GET"])
@login_required
def api_health():
    addon = blueprint.addon_context.get_addon("sentence_segmenter")
    if not addon:
        return jsonify({"error": "Addon not loaded"}), 500

    try:
        return jsonify(addon.service_health())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@blueprint.route("/api/boundaries", methods=["POST"])
@login_required
def api_test_boundaries():
    """Proxy endpoint for inspecting SaT boundary probabilities."""
    data = request.json or {}
    text = data.get("text", "")

    addon = blueprint.addon_context.get_addon("sentence_segmenter")
    if not addon:
        return jsonify({"error": "Addon not loaded"}), 500

    try:
        return jsonify(addon.predict_boundaries(text, threshold=data.get("threshold")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@blueprint.route("/api/paragraphs", methods=["POST"])
@login_required
def api_test_paragraphs():
    """Proxy endpoint for inspecting SaT paragraph segmentation."""
    data = request.json or {}
    text = data.get("text", "")

    addon = blueprint.addon_context.get_addon("sentence_segmenter")
    if not addon:
        return jsonify({"error": "Addon not loaded"}), 500

    try:
        paragraphs = addon.split_paragraphs(
            text,
            threshold=data.get("threshold"),
            paragraph_threshold=data.get("paragraph_threshold"),
        )
        return jsonify({"paragraphs": paragraphs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
