"""Compatibility of public imports, stored model references and file adapters."""
import importlib
import json
import pickle
import pickletools
from pathlib import Path
from typing import get_type_hints

import pytest

import whatsapp_argo as argo
from whatsapp_argo import models


@pytest.mark.parametrize("module, names", [
    ("binary", ("ArgoError", "Header", "Limits")),
    ("codec", ("Codec", "Document", "FieldErrors", "Layout", "NonNull", "read", "write")),
    ("schema", ("load_schema", "load_schema_store", "read_schema_store", "write_schema_store", "validate_schema")),
    ("adapters", ("OperationRegistry",)),
])
def test_original_public_imports_are_identical(module, names):
    submodule = importlib.import_module(f"whatsapp_argo.{module}")
    for name in names:
        assert name in argo.__all__
        assert name in submodule.__all__
        assert getattr(submodule, name) is getattr(argo, name)


@pytest.mark.parametrize("name", ["Document", "FieldErrors", "Layout", "NonNull"])
def test_legacy_pickled_model_references_still_resolve(name):
    # Protocol-0 GLOBAL reference emitted for the original defining module.
    # This is a fixed local class reference, not an external pickle input.
    legacy_reference = f"cwhatsapp_argo.codec\n{name}\n.".encode("ascii")
    assert pickle.loads(legacy_reference) is getattr(models, name)
    assert getattr(models, name) is getattr(argo, name)
    # New pickles must also resolve in releases predating models.py.
    assert pickletools.optimize(pickle.dumps(getattr(models, name), protocol=0)) == legacy_reference
    assert get_type_hints(getattr(models, name))


@pytest.mark.parametrize("name", [
    "load_schema", "load_schema_store", "read_schema_store", "write_schema_store",
    "validate_schema", "validate_schema_store",
])
def test_schema_function_pickles_keep_public_module(name):
    schema = importlib.import_module("whatsapp_argo.schema")
    function = getattr(schema, name)
    legacy_reference = f"cwhatsapp_argo.schema\n{name}\n.".encode("ascii")
    assert pickletools.optimize(pickle.dumps(function, protocol=0)) == legacy_reference
    assert pickle.loads(legacy_reference) is function


def test_document_models_survive_pickle_and_preserved_rewrite():
    schema = {"type": "ARRAY", "of": {"type": "NULLABLE", "of": "DESC"}}
    value = [argo.NonNull(None), argo.FieldErrors(), "same", "same"]
    payload = argo.write(value, schema)
    document = argo.read(payload, schema, preserve_layout=True)
    restored = pickle.loads(pickle.dumps(document))
    assert restored == document
    assert argo.write(restored, preserve_layout=True) == payload


@pytest.mark.parametrize("suffix", [".json", ".argo"])
def test_registry_loads_external_store_and_document_ids(tmp_path, suffix):
    store = {"Greeting": "STRING"}
    schema_file = tmp_path / ("schemas" + suffix)
    if suffix == ".json":
        schema_file.write_text(json.dumps(store), encoding="utf-8")
    else:
        schema_file.write_bytes(argo.write_schema_store(store))
    document_file = tmp_path / "documents.json"
    document_file.write_text(json.dumps({"Greeting": {"doc_id": 42}}), encoding="utf-8")

    registry = argo.OperationRegistry.from_files(schema_file, document_file)
    codec = registry.codec_for("42")
    assert registry.operation(42) == "Greeting"
    assert codec.read(codec.write("hello")).value == "hello"


def test_in_memory_schema_operations_do_not_access_files(monkeypatch):
    def unexpected_file_access(*args, **kwargs):
        raise AssertionError("in-memory schemas must not access files")

    monkeypatch.setattr(Path, "open", unexpected_file_access)
    monkeypatch.setattr(Path, "stat", unexpected_file_access)
    schema = argo.load_schema("STRING")
    store = {"Greeting": schema}
    payload = argo.write_schema_store(store)
    assert argo.read_schema_store(payload) == store
    assert argo.load_schema_store(payload) == store
    assert argo.load_schema_store(store) == store
    codec = argo.OperationRegistry(store, {42: "Greeting"}).codec_for(42)
    assert codec.read(codec.write("hello")).value == "hello"
