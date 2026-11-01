"""Configuration limits must apply equally to files and in-memory registries."""
import json
from pathlib import Path
import sys

import pytest

from whatsapp_argo import (
    ArgoError, Limits, OperationRegistry, load_schema, load_schema_store,
    write_schema_store,
)
from whatsapp_argo.schema import validate_schema_store


@pytest.mark.parametrize("size_limit", [sys.maxsize, 10 ** 100], ids=["platform-max", "larger-than-platform"])
@pytest.mark.parametrize("loader", ["schema", "json-store", "binary-store", "registry"])
def test_large_file_budget_accepts_small_configuration(tmp_path, size_limit, loader):
    limits = Limits(max_message_bytes=size_limit)
    path = tmp_path / "config.json"
    expected = {"Q": {"type": "STRING"}}
    if loader == "schema":
        path.write_text('"STRING"', encoding="utf-8")
        assert load_schema(path, limits) == expected["Q"]
    elif loader == "json-store":
        path.write_text('{"Q":"STRING"}', encoding="utf-8")
        assert load_schema_store(path, limits) == expected
    elif loader == "binary-store":
        path = path.with_suffix(".argo")
        path.write_bytes(write_schema_store(expected))
        assert load_schema_store(path, limits) == expected
    else:
        path.write_text('{"42":"Q"}', encoding="utf-8")
        registry = OperationRegistry.from_files(expected, path, limits=limits)
        assert registry.schema_for(42) == expected["Q"]


@pytest.mark.parametrize("extra_budget", [-1, 0, 1])
def test_configuration_size_limit_across_read_chunks(tmp_path, extra_budget):
    raw = b'"STRING"' + b" " * 70_000
    path = tmp_path / "schema.json"
    path.write_bytes(raw)
    limits = Limits(max_message_bytes=len(raw) + extra_budget)
    if extra_budget < 0:
        with pytest.raises(ArgoError, match="exceeds limit"):
            load_schema(path, limits)
    else:
        assert load_schema(path, limits) == {"type": "STRING"}


@pytest.mark.parametrize("growth", [8, 9])
def test_configuration_growth_after_stat_still_obeys_limit(tmp_path, monkeypatch, growth):
    path = tmp_path / "schema.json"
    path.write_bytes(b'"STRING"')
    original_open = Path.open

    def grow_before_open(self, *args, **kwargs):
        if self == path:
            with original_open(self, "ab") as stream:
                stream.write(b" " * growth)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow_before_open)
    limits = Limits(max_message_bytes=16)
    if growth == 8:
        assert load_schema(path, limits) == {"type": "STRING"}
    else:
        with pytest.raises(ArgoError, match="exceeds limit"):
            load_schema(path, limits)


def test_configuration_parser_does_not_ignore_later_chunks(tmp_path):
    path = tmp_path / "schema.json"
    path.write_bytes(b'"STRING"' + b" " * 70_000 + b"invalid suffix")
    with pytest.raises(ArgoError, match="invalid schema JSON"):
        load_schema(path)


@pytest.mark.parametrize("source", ["mapping", "json", "binary"])
def test_registry_shares_node_budget_including_unused_operations(tmp_path, source):
    schemas = {"A": {"type": "ARRAY", "of": "STRING"}, "B": "BOOLEAN"}
    documents = {"42": "A"}
    if source == "mapping":
        build = lambda limits: OperationRegistry(schemas, documents, limits=limits)
    else:
        schema_path = tmp_path / ("schemas.json" if source == "json" else "schemas.argo")
        if source == "json":
            schema_path.write_text(json.dumps(schemas), encoding="utf-8")
        else:
            schema_path.write_bytes(write_schema_store(schemas))
        document_path = tmp_path / "documents.json"
        document_path.write_text(json.dumps(documents), encoding="utf-8")
        build = lambda limits: OperationRegistry.from_files(schema_path, document_path, limits=limits)
    with pytest.raises(ArgoError, match="node limit"):
        build(Limits(max_total_values=2))
    registry = build(Limits(max_total_values=3))
    assert registry.schema_for(42) == {"type": "ARRAY", "of": {"type": "STRING"}}


@pytest.mark.parametrize("name, limit, message", [
    (42, 100, "must be text"),
    (b"Q", 100, "must be text"),
    ("\ud800", 100, "invalid UTF-8"),
    ("long", 3, "exceeds limit"),
    ("界", 2, "exceeds limit"),
])
def test_registry_validates_unused_operation_names(name, limit, message):
    schemas = {"A": "STRING", name: "BOOLEAN"}
    limits = Limits(max_string_bytes=limit)
    with pytest.raises(ArgoError, match=message):
        validate_schema_store(schemas, limits)
    with pytest.raises(ArgoError, match=message):
        OperationRegistry(schemas, {42: "A"}, limits=limits)


def test_registry_validation_keeps_independent_schema_copy_and_empty_store():
    schema = {"type": "ARRAY", "of": {"type": "STRING"}}
    registry = OperationRegistry({"界": schema}, {42: "界"}, limits=Limits(max_string_bytes=3))
    schema["of"]["type"] = "BOOLEAN"
    returned = registry.schema_for(42)
    returned["of"]["type"] = "VARINT"
    assert registry.schema_for(42) == {"type": "ARRAY", "of": {"type": "STRING"}}
    assert OperationRegistry({}, {}, limits=Limits(max_total_values=0)).schemas == {}
