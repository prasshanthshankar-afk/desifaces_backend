import io

from PIL import Image

from app.services.group_photo_quality import analyze_group_photo


def _jpeg(width: int, height: int, value: int = 128) -> bytes:
    image = Image.new("RGB", (width, height), (value, value, value))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_group_photo_fail_explains_every_blocking_issue():
    decision = analyze_group_photo(_jpeg(640, 360, 128), expected_speakers=2)
    payload = decision.to_dict()

    assert payload["status"] == "FAIL"
    assert payload["usable"] is False
    assert "cannot be used" in payload["summary"].lower()

    failures = [item for item in payload["checks"] if item["status"] == "FAIL"]
    assert failures
    for item in failures:
        assert item["title"]
        assert item["reason"]
        assert item["required_action"]

    codes = {item["code"] for item in failures}
    assert "IMAGE_RESOLUTION" in codes
    assert "FACE_COUNT" in codes


def test_group_photo_extra_or_missing_faces_never_silently_passes():
    decision = analyze_group_photo(_jpeg(1280, 720, 128), expected_speakers=2)
    payload = decision.to_dict()

    face_count = next(item for item in payload["checks"] if item["code"] == "FACE_COUNT")
    assert face_count["status"] == "FAIL"
    assert "2 speakers" in face_count["reason"]
    assert face_count["required_action"]
