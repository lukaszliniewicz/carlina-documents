from pathlib import Path


def test_doc_processor_frontend_assets_do_not_contain_null_bytes():
    root = Path(__file__).resolve().parents[1]
    asset_paths = [
        root / "iarf_assistant" / "addons" / "doc_processor" / "frontend" / "src" / "components" / "DocumentCard.svelte",
        root / "iarf_assistant" / "addons" / "doc_processor" / "frontend" / "src" / "components" / "DocumentPreviewModal.svelte",
        root / "iarf_assistant" / "addons" / "doc_processor" / "frontend" / "src" / "components" / "ProjectView.svelte",
        root / "iarf_assistant" / "addons" / "doc_processor" / "static" / "dist" / "workspace.js",
        root / "iarf_assistant" / "addons" / "doc_processor" / "static" / "dist" / "workspace.css",
    ]

    missing = [str(path) for path in asset_paths if not path.exists()]
    assert not missing, f"Missing expected doc processor assets: {missing}"

    corrupted = [str(path) for path in asset_paths if b"\x00" in path.read_bytes()]
    assert not corrupted, f"Doc processor assets contain unexpected NUL bytes: {corrupted}"
