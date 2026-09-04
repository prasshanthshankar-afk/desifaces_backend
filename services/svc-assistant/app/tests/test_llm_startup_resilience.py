from __future__ import annotations

import app.llm as llm_module
from app.config import settings


def test_assistant_llm_missing_model_does_not_crash(monkeypatch):
    monkeypatch.setattr(settings, "DF_ASSISTANT_LLM_MODEL", "")
    llm = llm_module.AssistantLLM()
    assert llm.configured is False
    assert llm.configuration_error == "assistant_llm_model_not_configured"


def test_assistant_llm_constructor_error_does_not_crash(monkeypatch):
    monkeypatch.setattr(settings, "DF_ASSISTANT_LLM_MODEL", "gpt-test")

    class BrokenChatOpenAI:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("bad_llm_configuration")

    monkeypatch.setattr(llm_module, "ChatOpenAI", BrokenChatOpenAI)
    llm = llm_module.AssistantLLM()
    assert llm.configured is False
    assert llm.configuration_error is not None
    assert "bad_llm_configuration" in llm.configuration_error
