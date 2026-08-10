from app.services.providers.openai_image_client import OpenAIImageClient


def test_gpt_image_2_is_default_for_t2i_and_edit(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_IMAGE_MODEL_T2I", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_MODEL_EDIT", raising=False)

    client = OpenAIImageClient()

    assert client.model_t2i == "gpt-image-2"
    assert client.model_edit == "gpt-image-2"


def test_image_model_environment_overrides_remain_supported(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_IMAGE_MODEL_T2I", "custom-t2i")
    monkeypatch.setenv("OPENAI_IMAGE_MODEL_EDIT", "custom-edit")

    client = OpenAIImageClient()

    assert client.model_t2i == "custom-t2i"
    assert client.model_edit == "custom-edit"
