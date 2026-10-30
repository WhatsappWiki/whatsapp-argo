"""Independent boundary checks for values which compare equal in Python,
and for rejected writes/configuration that must not lose information.
"""
import json
import struct

import pytest

from whatsapp_argo import ArgoError, FieldErrors, Header, Limits, NonNull, OperationRegistry, read, write
from whatsapp_argo.binary import MessageWriter
from whatsapp_argo.schema import load_schema


def test_layout_signed_zero_mutation_is_written():
    original = write(0.0, "FLOAT64")
    document = read(original, "FLOAT64", preserve_layout=True)
    document.value = -0.0
    changed = write(document, preserve_layout=True)
    assert changed != original
    assert struct.pack("<d", read(changed, "FLOAT64").value) == struct.pack("<d", -0.0)


def test_layout_nan_payload_mutation_is_written():
    first_bits = bytes.fromhex("010000000000f87f")
    second_bits = bytes.fromhex("020000000000f87f")
    original = write(struct.unpack("<d", first_bits)[0], "FLOAT64")
    document = read(original, "FLOAT64", preserve_layout=True)
    assert write(document, preserve_layout=True) == original
    document.value = struct.unpack("<d", second_bits)[0]
    changed = write(document, preserve_layout=True)
    assert changed != original
    assert struct.pack("<d", read(changed, "FLOAT64").value) == second_bits


def test_output_budget_shared_across_blocks_and_core_before_allocation():
    writer = MessageWriter(Header(inline_everything=False), Limits(max_message_bytes=10))
    writer.core.extend(b"abc")
    writer.block("A").extend(b"12345")
    with pytest.raises(ArgoError, match="message exceeds"):
        writer.block("B").extend(b"678")
    assert writer.block("B") == b""
    assert writer.core == b"abc"
    assert writer.block("A") == b"12345"


def test_output_budget_counts_header_before_payload_allocation():
    header = Header(user_flags=(True,) * 14)
    with pytest.raises(ArgoError, match="message exceeds"):
        MessageWriter(header, Limits(max_message_bytes=2))


def test_output_budget_rejects_repetition_without_allocating_output():
    writer = MessageWriter(Header(inline_everything=True), Limits(max_message_bytes=10))
    writer.core.extend(b"abc")
    with pytest.raises(ArgoError, match="message exceeds"):
        writer.core *= 10 ** 12
    assert writer.core == b"abc"


def test_output_budget_reclaimed_when_buffer_is_shrunk():
    writer = MessageWriter(Header(inline_everything=True), Limits(max_message_bytes=8))
    writer.core.extend(b"1234567")
    writer.core[0:3] = b"x"
    writer.core.extend(b"89")
    assert writer.finish() == b"\x0d" + b"x456789"


def test_duplicate_schema_properties_cannot_change_selected_type(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text('{"type":"STRING","type":"BYTES"}')
    with pytest.raises(ArgoError, match="duplicate"):
        load_schema(path)


def test_duplicate_document_properties_cannot_change_selected_operation(tmp_path):
    schema_file = tmp_path / "schemas.json"
    schema_file.write_text(json.dumps({"A": "STRING", "B": "BYTES"}))
    document_file = tmp_path / "documents.json"
    document_file.write_text('{"42":"A","42":"B"}')
    with pytest.raises(ArgoError, match="duplicate"):
        OperationRegistry.from_files(schema_file, document_file)


def test_document_file_size_checked_before_json_is_read(tmp_path):
    schema_file = tmp_path / "schemas.json"
    schema_file.write_text('{}')
    document_file = tmp_path / "documents.json"
    document_file.write_bytes(b" " * 10_000)
    with pytest.raises(ArgoError, match="exceeds limit"):
        OperationRegistry.from_files(schema_file, document_file, limits=Limits(max_message_bytes=100))


def test_fixed_memoryview_uses_byte_count():
    data = b"abcd"
    view = memoryview(data).cast("I")
    assert len(view) == 1 and view.nbytes == 4
    schema = {"type": "FIXED", "length": 4}
    assert read(write(view, schema), schema).value == data
    with pytest.raises(ArgoError, match="exact-length"):
        write(view, {"type": "FIXED", "length": 1})


def test_boolean_key_collision_cannot_reinterpret_a_string_block():
    schema = {"type": "RECORD", "fields": [
        {"name": "text", "of": {"type": "BLOCK", "of": "STRING", "key": "Shared", "dedupe": False}},
        {"name": "flag", "of": {"type": "BLOCK", "of": "BOOLEAN", "key": "Shared", "dedupe": False}},
    ]}
    with pytest.raises(ArgoError, match="conflicting"):
        write({"text": "x", "flag": True}, schema)


def test_self_describing_path_empty_field_name_is_rejected():
    with pytest.raises(ArgoError, match="PATH"):
        write([""], "PATH", header=Header(self_describing=True))


def test_typed_path_decoding_also_rejects_empty_field_names():
    path_schema = {"type": "RECORD", "fields": [{"name": "", "of": "STRING"}]}
    # Int block is ordinal 0; Core is one path segment. No textual field name
    # is carried in the message; it must still be validated after resolution.
    with pytest.raises(ArgoError, match="PATH"):
        read(bytes.fromhex("0c02000202"), "PATH", path_schema=path_schema)


def test_schema_store_total_nodes_limit_is_shared_across_operations():
    from whatsapp_argo.schema import write_schema_store
    with pytest.raises(ArgoError, match="node limit"):
        write_schema_store({"A": "STRING", "B": "BOOLEAN"}, limits=Limits(max_total_values=1))


def test_nested_nullable_cannot_silently_drop_an_explicit_nonnull_layer():
    schema = {"type": "NULLABLE", "of": {"type": "NULLABLE", "of": "STRING"}}
    with pytest.raises(ArgoError, match="cannot distinguish|nested NULLABLE"):
        write(NonNull(FieldErrors()), schema)


def test_typed_error_locations_are_counted_in_total_value_budget():
    value = {"message": "error", "locations": [{"line": 1, "column": 2}] * 10}
    header = Header(self_describing_errors=False)
    raw = write(value, "ERROR", header=header)
    with pytest.raises(ArgoError, match="total item limit"):
        write(value, "ERROR", header=header, limits=Limits(max_total_values=5))
    with pytest.raises(ArgoError, match="total item limit"):
        read(raw, "ERROR", limits=Limits(max_total_values=5))
