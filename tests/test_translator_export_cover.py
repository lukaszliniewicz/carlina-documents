import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from iarf_assistant.addons.translator import export_cover
from iarf_assistant.addons.translator.export_cover import (
    ExportCoverError,
    capture_export_cover,
    resolve_export_cover,
    resolve_export_cover_path,
)


@pytest.fixture
def upload_root(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    (root / "7").mkdir(parents=True)
    monkeypatch.setattr(export_cover, "uploads_root", lambda: root)
    return root


def _save_image(path: Path, image: Image.Image, *, image_format: str) -> bytes:
    output = io.BytesIO()
    image.save(output, format=image_format)
    path.write_bytes(output.getvalue())
    return output.getvalue()


def _uri_bytes(uri: str) -> bytes:
    prefix = "data:image/jpeg;base64,"
    assert uri.startswith(prefix)
    return base64.b64decode(uri.removeprefix(prefix), validate=True)


def test_capture_accepts_jpeg_and_png_and_resolves_normalized_jpeg(upload_root):
    owner = upload_root / "7"
    jpeg = Image.new("RGB", (20, 10), "navy")
    jpeg_path = owner / "source.jpg"
    jpeg_output = io.BytesIO()
    jpeg_exif = Image.Exif()
    jpeg_exif[274] = 6
    jpeg.save(jpeg_output, format="JPEG", exif=jpeg_exif, icc_profile=b"invalid")
    jpeg_original = jpeg_output.getvalue()
    jpeg_path.write_bytes(jpeg_original)
    png = Image.new("RGBA", (12, 8), (255, 0, 0, 0))
    for x in range(6):
        for y in range(4):
            png.putpixel((x, y), (255, 0, 0, 255))
    png_path = owner / "source.png"
    png_original = _save_image(png_path, png, image_format="PNG")

    jpeg_witness = capture_export_cover(user_id=7, upload_path="/uploads/7/source.jpg")
    png_witness = capture_export_cover(user_id=7, upload_path="/uploads/7/source.png")

    for witness, source_path in (
        (jpeg_witness, jpeg_path),
        (png_witness, png_path),
    ):
        assert set(witness) == {
            "upload_path",
            "media_type",
            "byte_length",
            "sha256",
        }
        assert witness["upload_path"].startswith("/uploads/7/translator-covers/")
        assert witness["upload_path"].endswith(".jpg")
        assert witness["media_type"] == "image/jpeg"
        assert witness["byte_length"] == len(
            (
                upload_root / witness["upload_path"].removeprefix("/uploads/")
            ).read_bytes()
        )
        assert source_path.read_bytes() in {jpeg_original, png_original}
        resolved = resolve_export_cover(user_id=7, cover=witness)
        output = _uri_bytes(resolved)
        with Image.open(io.BytesIO(output)) as image:
            assert image.format == "JPEG"
            assert image.width <= 1600 and image.height <= 2400
            assert "exif" not in image.info
            assert "icc_profile" not in image.info
            if witness is jpeg_witness:
                assert image.size == (10, 20)

    with Image.open(
        io.BytesIO(_uri_bytes(resolve_export_cover(user_id=7, cover=png_witness)))
    ) as image:
        assert min(image.getpixel((10, 7))) >= 245
        assert image.getpixel((2, 2))[0] >= 245


def test_capture_deduplicates_and_reuses_canonical_bytes_without_reencoding(
    upload_root,
):
    source = upload_root / "7" / "cover.png"
    _save_image(source, Image.new("RGBA", (32, 24), "purple"), image_format="PNG")

    first = capture_export_cover(user_id=7, upload_path="/uploads/7/cover.png")
    canonical = upload_root / first["upload_path"].removeprefix("/uploads/")
    canonical_bytes = canonical.read_bytes()
    second = capture_export_cover(user_id=7, upload_path="/uploads/7/cover.png")
    reused = capture_export_cover(user_id=7, upload_path=first["upload_path"])

    assert first == second == reused
    assert canonical.read_bytes() == canonical_bytes
    assert len(list((upload_root / "7" / "translator-covers").glob("*.jpg"))) == 1


@pytest.mark.parametrize(
    "upload_path",
    [
        "/uploads/8/foreign.jpg",
        "/uploads/7/../8/foreign.jpg",
        "/uploads/7/source.jpg?query=1",
        "/uploads/7/source.jpg#fragment",
        "/uploads/7/%2e%2e/source.jpg",
        "/uploads/7\\source.jpg",
        "https://example.test/source.jpg",
        "uploads/7/source.jpg",
    ],
)
def test_capture_rejects_untrusted_or_foreign_paths(upload_root, upload_path):
    with pytest.raises(ExportCoverError) as error:
        capture_export_cover(user_id=7, upload_path=upload_path)
    assert error.value.code.startswith("export-cover-")
    assert str(error.value) == error.value.code
    assert str(upload_root) not in str(error.value)


def test_capture_rejects_symlink_escape_and_canonical_name_mismatch(
    upload_root, tmp_path
):
    owner = upload_root / "7"
    outside = tmp_path / "outside.jpg"
    _save_image(outside, Image.new("RGB", (10, 10), "red"), image_format="JPEG")
    link = owner / "linked.jpg"
    link.symlink_to(outside)
    with pytest.raises(ExportCoverError):
        capture_export_cover(user_id=7, upload_path="/uploads/7/linked.jpg")

    canonical_dir = owner / "translator-covers"
    canonical_dir.mkdir()
    bad_digest = "0" * 64
    bad_path = canonical_dir / f"{bad_digest}.jpg"
    bad_bytes = outside.read_bytes()
    bad_path.write_bytes(bad_bytes)
    with pytest.raises(ExportCoverError) as error:
        capture_export_cover(
            user_id=7,
            upload_path=f"/uploads/7/translator-covers/{bad_digest}.jpg",
        )
    assert error.value.code == "export-cover-canonical-hash"


def test_capture_rejects_fake_unsupported_and_animated_images(upload_root):
    owner = upload_root / "7"
    fake = owner / "fake.jpg"
    fake.write_bytes(b"not an image")
    with pytest.raises(ExportCoverError) as fake_error:
        capture_export_cover(user_id=7, upload_path="/uploads/7/fake.jpg")
    assert fake_error.value.code == "export-cover-image-invalid"

    animated = owner / "animated.png"
    frames = [Image.new("RGBA", (8, 8), color) for color in ("red", "blue")]
    frames[0].save(
        animated,
        format="PNG",
        save_all=True,
        append_images=frames[1:],
        duration=10,
        loop=0,
    )
    with pytest.raises(ExportCoverError) as animated_error:
        capture_export_cover(user_id=7, upload_path="/uploads/7/animated.png")
    assert animated_error.value.code == "export-cover-image-animated"

    unsupported = owner / "unsupported.gif"
    _save_image(unsupported, Image.new("RGB", (8, 8), "red"), image_format="GIF")
    with pytest.raises(ExportCoverError) as unsupported_error:
        capture_export_cover(user_id=7, upload_path="/uploads/7/unsupported.gif")
    assert unsupported_error.value.code == "export-cover-image-format"


def test_capture_rejects_input_size_and_pixel_limits(upload_root):
    oversized = upload_root / "7" / "oversized.jpg"
    oversized.write_bytes(b"x" * (export_cover.MAX_INPUT_BYTES + 1))
    with pytest.raises(ExportCoverError) as size_error:
        capture_export_cover(user_id=7, upload_path="/uploads/7/oversized.jpg")
    assert size_error.value.code == "export-cover-input-size"

    huge = upload_root / "7" / "huge.png"
    _save_image(huge, Image.new("1", (5001, 5000), 1), image_format="PNG")
    with pytest.raises(ExportCoverError) as dimensions_error:
        capture_export_cover(user_id=7, upload_path="/uploads/7/huge.png")
    assert dimensions_error.value.code == "export-cover-image-dimensions"


def test_resolver_rejects_tampered_bytes_metadata_and_foreign_owner(upload_root):
    source = upload_root / "7" / "cover.jpg"
    _save_image(source, Image.new("RGB", (24, 24), "orange"), image_format="JPEG")
    witness = capture_export_cover(user_id=7, upload_path="/uploads/7/cover.jpg")
    canonical = upload_root / witness["upload_path"].removeprefix("/uploads/")

    tampered = bytearray(canonical.read_bytes())
    tampered[-1] ^= 1
    canonical.write_bytes(tampered)
    with pytest.raises(ExportCoverError) as tamper_error:
        resolve_export_cover(user_id=7, cover=witness)
    assert tamper_error.value.code == "export-cover-canonical-hash"

    canonical.write_bytes(bytes(tampered))
    for altered in (
        {**witness, "byte_length": witness["byte_length"] + 1},
        {**witness, "sha256": "f" * 64},
        {**witness, "extra": "rejected"},
        {**witness, "upload_path": witness["upload_path"].replace("/7/", "/8/")},
    ):
        with pytest.raises(ExportCoverError) as error:
            resolve_export_cover(user_id=7, cover=altered)
        assert error.value.code.startswith("export-cover-")


def test_resolve_export_cover_path_and_exact_inline_limit(upload_root, monkeypatch):
    source = upload_root / "7" / "cover.png"
    _save_image(source, Image.new("RGB", (40, 20), "teal"), image_format="PNG")
    witness = capture_export_cover(user_id=7, upload_path="/uploads/7/cover.png")

    uri = resolve_export_cover_path(user_id=7, upload_path=witness["upload_path"])
    assert _uri_bytes(uri)
    assert len(uri) <= 1_048_576
    monkeypatch.setattr(export_cover, "MAX_INLINE_IMAGE_CHARS", len(uri) - 1)
    with pytest.raises(ExportCoverError) as error:
        resolve_export_cover(user_id=7, cover=witness)
    assert error.value.code == "export-cover-inline-size"

    with pytest.raises(ExportCoverError) as path_error:
        resolve_export_cover_path(user_id=8, upload_path=witness["upload_path"])
    assert path_error.value.code == "export-cover-canonical-path"
