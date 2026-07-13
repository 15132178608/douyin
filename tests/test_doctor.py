import os
from pathlib import Path

import pytest

from src import doctor


def _isolate_cache_candidates(monkeypatch, tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(doctor, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(
        doctor,
        "WINDOWS_RUNTIME_MODEL_CACHE",
        tmp_path / "missing-windows-runtime-cache",
    )
    for variable in doctor.MODEL_CACHE_ENV_VARS:
        monkeypatch.delenv(variable, raising=False)
    return project_root


def test_model_cache_check_warns_for_empty_or_metadata_only_roots(monkeypatch, tmp_path: Path) -> None:
    _isolate_cache_candidates(monkeypatch, tmp_path)
    cache_root = tmp_path / "hf"
    (cache_root / ".locks").mkdir(parents=True)
    (cache_root / "xet").mkdir()
    (cache_root / "CACHEDIR.TAG").write_text("cache", encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(cache_root))

    result = doctor._model_cache_check()

    assert result["status"] == "warning"
    assert result["ok"] is True
    assert result["details"]["found"] is False
    assert str(cache_root) in result["details"]["existing_roots"]


def test_model_cache_check_finds_sentence_transformers_snapshot(monkeypatch, tmp_path: Path) -> None:
    _isolate_cache_candidates(monkeypatch, tmp_path)
    cache_root = tmp_path / "sentence-transformers"
    snapshot = cache_root / "models--BAAI--bge-m3" / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", str(cache_root))

    result = doctor._model_cache_check()

    assert result["status"] == "ok"
    assert result["details"]["found"] is True
    assert result["details"]["detected_source"] == "SENTENCE_TRANSFORMERS_HOME"
    assert result["details"]["evidence_path"] == str(snapshot / "config.json")


def test_model_cache_check_finds_huggingface_hub_snapshot(monkeypatch, tmp_path: Path) -> None:
    _isolate_cache_candidates(monkeypatch, tmp_path)
    cache_root = tmp_path / "huggingface"
    snapshot = cache_root / "hub" / "models--org--model" / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").write_bytes(b"weights")
    monkeypatch.setenv("HF_HOME", str(cache_root))

    result = doctor._model_cache_check()

    assert result["status"] == "ok"
    assert result["details"]["detected_source"] == "HF_HOME"
    assert result["details"]["detected_root"] == str(cache_root)


def test_model_cache_check_keeps_legacy_project_cache_compatible(monkeypatch, tmp_path: Path) -> None:
    project_root = _isolate_cache_candidates(monkeypatch, tmp_path)
    legacy_root = project_root / "data" / "models"
    legacy_root.mkdir(parents=True)
    (legacy_root / "custom-model.bin").write_bytes(b"model")

    result = doctor._model_cache_check()

    assert result["status"] == "ok"
    assert result["details"]["detected_source"] == "project_data_models"
    assert result["details"]["evidence_path"] == str(legacy_root / "custom-model.bin")


def test_model_cache_check_rejects_nested_empty_legacy_directories(monkeypatch, tmp_path: Path) -> None:
    project_root = _isolate_cache_candidates(monkeypatch, tmp_path)
    (project_root / "data" / "models" / "empty-model" / "0_Transformer").mkdir(parents=True)

    result = doctor._model_cache_check()

    assert result["status"] == "warning"
    assert result["details"]["found"] is False


def test_model_cache_check_rejects_partial_or_empty_snapshot(monkeypatch, tmp_path: Path) -> None:
    _isolate_cache_candidates(monkeypatch, tmp_path)
    cache_root = tmp_path / "huggingface"
    snapshot = cache_root / "hub" / "models--org--partial" / "snapshots" / "revision"
    (snapshot / "0_Transformer").mkdir(parents=True)
    (snapshot / "config.json").write_bytes(b"")
    (snapshot / "README.md").write_text("model card only", encoding="utf-8")
    (snapshot / "model.safetensors.incomplete").write_bytes(b"partial")
    (snapshot / ".locks").mkdir()
    (snapshot / ".locks" / "model.safetensors").write_bytes(b"not-a-ready-model")
    monkeypatch.setenv("HF_HOME", str(cache_root))

    result = doctor._model_cache_check()

    assert result["status"] == "warning"
    assert result["details"]["found"] is False


def test_model_cache_check_prefers_explicit_environment_over_legacy(monkeypatch, tmp_path: Path) -> None:
    project_root = _isolate_cache_candidates(monkeypatch, tmp_path)
    legacy_payload = project_root / "data" / "models" / "legacy-model.bin"
    legacy_payload.parent.mkdir(parents=True)
    legacy_payload.write_bytes(b"legacy")
    cache_root = tmp_path / "explicit-cache"
    snapshot = cache_root / "models--org--model" / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    payload = snapshot / "config.json"
    payload.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(cache_root))

    result = doctor._model_cache_check()

    assert result["status"] == "ok"
    assert result["details"]["detected_source"] == "HF_HOME"
    assert result["details"]["evidence_path"] == str(payload)


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime cache fallback is Windows-only")
def test_model_cache_check_finds_windows_runtime_default(monkeypatch, tmp_path: Path) -> None:
    _isolate_cache_candidates(monkeypatch, tmp_path)
    cache_root = tmp_path / "windows-runtime-cache"
    snapshot = cache_root / "sentence-transformers" / "models--org--model" / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    payload = snapshot / "model.safetensors"
    payload.write_bytes(b"weights")
    monkeypatch.setattr(doctor, "WINDOWS_RUNTIME_MODEL_CACHE", cache_root)

    result = doctor._model_cache_check()

    assert result["status"] == "ok"
    assert result["details"]["detected_source"] == "windows_runtime_default"
    assert result["details"]["evidence_path"] == str(payload)
