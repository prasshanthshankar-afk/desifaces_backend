import base64

from PIL import Image

from app.services.providers import openai_image_client as module
from app.services.providers.openai_image_client import OpenAIImageClient


class _Response:
    status_code = 200
    headers = {}

    def json(self):
        return {
            "data": [
                {
                    "b64_json": base64.b64encode(b"image-bytes").decode("ascii"),
                }
            ]
        }


def test_moderation_defaults_to_low(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_IMAGE_MODERATION", raising=False)

    client = OpenAIImageClient()

    assert client.moderation == "low"


def test_moderation_auto_can_be_restored_by_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_IMAGE_MODERATION", "auto")

    client = OpenAIImageClient()

    assert client.moderation == "auto"


def test_invalid_moderation_falls_back_to_low(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_IMAGE_MODERATION", "invalid")

    client = OpenAIImageClient()

    assert client.moderation == "low"


def test_t2i_sends_low_moderation(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_IMAGE_MODERATION", "low")

    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _Response()

    monkeypatch.setattr(module.requests, "post", fake_post)

    client = OpenAIImageClient()
    result = client.generate_image(
        prompt="adult fashion editorial portrait"
    )

    assert result == b"image-bytes"
    assert captured["json"]["moderation"] == "low"


def test_edit_sends_low_moderation(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_IMAGE_MODERATION", "low")

    image_path = tmp_path / "source.png"
    Image.new("RGB", (32, 32)).save(image_path)

    captured = {}

    def fake_post(url, *, headers, data, files, timeout):
        captured["url"] = url
        captured["data"] = data
        return _Response()

    monkeypatch.setattr(module.requests, "post", fake_post)

    client = OpenAIImageClient()
    result = client.edit_image(
        prompt="adult fashion editorial wardrobe edit",
        image_path=str(image_path),
    )

    assert result == b"image-bytes"
    assert captured["data"]["moderation"] == "low"
