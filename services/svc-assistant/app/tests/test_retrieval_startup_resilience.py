from pathlib import Path

from app import retrieval


def _configure(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(retrieval.settings, "DF_ASSISTANT_KNOWLEDGE_DIR", str(root))
    monkeypatch.setattr(retrieval.settings, "DF_ASSISTANT_EMBEDDING_MODEL", "")
    monkeypatch.setattr(retrieval.settings, "OPENAI_API_KEY", "")


def test_invalid_utf8_knowledge_file_is_skipped(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text("# Good\nusable knowledge", encoding="utf-8")
    (tmp_path / "bad.md").write_bytes(b"# Bad knowledge with invalid byte: \xa3")
    _configure(monkeypatch, tmp_path)

    result = retrieval.SafeKnowledgeRetriever()

    assert result.chunk_count >= 1
    assert result.loaded_file_count == 1
    assert result.skipped_file_count == 1
    assert result.skipped_files == ("bad.md",)


def test_utf8_bom_knowledge_file_loads(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "bom.md").write_text("# BOM\nvalid", encoding="utf-8-sig")
    _configure(monkeypatch, tmp_path)

    result = retrieval.SafeKnowledgeRetriever()

    assert result.chunk_count >= 1
    assert result.loaded_file_count == 1
    assert result.skipped_file_count == 0
