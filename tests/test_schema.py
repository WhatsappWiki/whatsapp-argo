import json
from dataclasses import replace

import pytest

from whatsapp_argo.binary import ArgoError, Header, Limits
from whatsapp_argo.schema import (
    load_schema, load_schema_store, read_schema_store, validate_schema,
    validate_schema_store, write_schema_store,
)


def record(*fields):
    return {"type": "RECORD", "fields": list(fields)}


def block(kind="STRING", key="String", dedupe=True):
    return {"type": "BLOCK", "of": {"type": kind}, "key": key, "dedupe": dedupe}


def all_wire_types():
    return record(*[
        {"name": str(index), "of": wire, "omittable": bool(index % 2)}
        for index, wire in enumerate([
            *({"type": kind} for kind in ("STRING", "BYTES", "BOOLEAN", "VARINT", "FLOAT64", "DESC", "ERROR", "PATH", "EXTENSIONS")),
            {"type": "FIXED", "length": 3}, block(),
            {"type": "ARRAY", "of": {"type": "NULLABLE", "of": block()}},
        ])
    ])


def test_fixed_store_vector():
    raw = bytes.fromhex("0c02510804020201")
    assert read_schema_store(raw) == {"Q": {"type": "STRING"}}
    assert write_schema_store({"Q": "STRING"}) == raw


@pytest.mark.parametrize("flags", range(128))
def test_store_all_headers_and_all_types(flags):
    user_flags = (True,) if flags & 64 else None
    header = Header(**dict(zip((
        "inline_everything", "self_describing", "out_of_band_field_errors",
        "self_describing_errors", "null_terminated_strings", "no_deduplication",
    ), (bool(flags & (1 << index)) for index in range(6)))), user_flags=user_flags)
    store = {"Operation": all_wire_types(), "Other": all_wire_types()}
    raw = write_schema_store(store, header)
    assert read_schema_store(raw) == store
    assert write_schema_store(read_schema_store(raw), header) == raw


def test_no_encoded_payload_cache_and_external_schema_replacement(tmp_path):
    store = {"Q": record({"name": "x", "of": {"type": "STRING"}, "omittable": False})}
    raw = write_schema_store(store)
    result = read_schema_store(raw)
    result["Q"]["fields"][0]["name"] = "changed"
    changed = write_schema_store(result)
    assert changed != raw
    assert read_schema_store(changed) == result
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"type": "STRING"}))
    assert load_schema(path) == {"type": "STRING"}
    path.write_text(json.dumps({"type": "VARINT"}))
    assert load_schema(path) == {"type": "VARINT"}


def test_configuration_loaders(tmp_path):
    schema = record({"name": "x", "of": "STRING"})
    normalized = validate_schema(schema)
    assert normalized["fields"][0] == {"name": "x", "of": {"type": "STRING"}, "omittable": False}
    schema["fields"][0]["name"] = "mutated"
    assert normalized["fields"][0]["name"] == "x"
    official = {"type": "STORE", "types": [{"name": "Q", "type": normalized}]}
    assert validate_schema_store(official) == {"Q": normalized}
    json_path = tmp_path / "store.json"
    json_path.write_text(json.dumps(official))
    assert load_schema_store(json_path) == {"Q": normalized}
    wire_path = tmp_path / "store.argo"
    wire_path.write_bytes(write_schema_store(official))
    assert load_schema_store(wire_path) == {"Q": normalized}
    assert load_schema_store(wire_path.read_bytes()) == {"Q": normalized}


@pytest.mark.parametrize("schema", [
    {}, None, 5, {"type": "UNION"}, {"type": "STRING", "extra": 1},
    {"type": "ARRAY"}, {"type": "FIXED", "length": -1},
    {"type": "FIXED", "length": True}, {"type": "RECORD", "fields": {}},
    {"type": "RECORD", "fields": [{"name": "x", "of": "STRING", "omittable": 1}]},
    record({"name": "x", "of": "STRING"}, {"name": "x", "of": "BOOLEAN"}),
    {"type": "BLOCK", "of": {"type": "ARRAY", "of": "STRING"}, "key": "B", "dedupe": False},
    {"type": "BLOCK", "of": "STRING", "key": b"B", "dedupe": True},
    {"type": "BLOCK", "of": "STRING", "key": "B", "dedupe": 1},
    record({"name": "\ud800", "of": "STRING"}),
])
def test_invalid_schema_fails(schema):
    with pytest.raises(ArgoError):
        validate_schema(schema)


def test_schema_cycles_depth_size_and_nodes():
    cyclic = {"type": "ARRAY"}
    cyclic["of"] = cyclic
    with pytest.raises(ArgoError, match="cyclic"):
        validate_schema(cyclic)
    nested = {"type": "ARRAY", "of": {"type": "ARRAY", "of": "STRING"}}
    with pytest.raises(ArgoError, match="depth"):
        validate_schema(nested, Limits(max_depth=1))
    with pytest.raises(ArgoError, match="node"):
        validate_schema(nested, Limits(max_total_values=1))
    with pytest.raises(ArgoError):
        validate_schema(record({"name": "long", "of": "STRING"}), Limits(max_string_bytes=1))
    with pytest.raises(ArgoError):
        write_schema_store({"Q": "STRING"}, limits=Limits(max_container_items=0))


def test_json_duplicate_properties_are_rejected(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text('{"type":"STRING","type":"VARINT"}')
    with pytest.raises(ArgoError, match="duplicate JSON"):
        load_schema(path)
    path.write_text('{"type":')
    with pytest.raises(ArgoError, match="invalid schema JSON"):
        load_schema(path)


def test_store_truncations_and_suffixes_rejected():
    raw = write_schema_store({"Q": all_wire_types()})
    for cut in range(len(raw)):
        with pytest.raises(ArgoError):
            read_schema_store(raw[:cut])
    for suffix in (b"\0", b"x", b"\0\0"):
        with pytest.raises(ArgoError):
            read_schema_store(raw + suffix)


@pytest.mark.parametrize("raw", [
    "0c020001",  # invalid store marker
    "0d040301",  # negative operation count
    "0d040202ff",  # truncated type marker
    "0d0402025163",  # unknown type marker -50
    "0d0404025101025101",  # duplicate Q operation
    "0d0402025113",  # RECORD has no field count
    "0d040202510d01",  # negative fixed length
])
def test_invalid_store_vectors(raw):
    with pytest.raises(ArgoError):
        read_schema_store(bytes.fromhex(raw))
