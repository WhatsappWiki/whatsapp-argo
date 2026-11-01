from dataclasses import replace
import copy
import math

import pytest

from whatsapp_argo import (
    ArgoError, Codec, FieldErrors, Header, Limits, NonNull, OperationRegistry,
    load_schema, read, write,
)
from whatsapp_argo.binary import MessageWriter, label_bytes


def field(name, of, omittable=False):
    return {"name": name, "of": of, "omittable": omittable}


def record(*fields):
    return {"type": "RECORD", "fields": list(fields)}


def nullable(of):
    return {"type": "NULLABLE", "of": of}


def array(of):
    return {"type": "ARRAY", "of": of}


def block(of, key="custom", dedupe=False):
    return {"type": "BLOCK", "of": of, "key": key, "dedupe": dedupe}


SCHEMA = record(
    field("data", nullable(record(
        field("text", "STRING"), field("blob", "BYTES"),
        field("integer", "VARINT"), field("float", "FLOAT64"),
        field("fixed", {"type": "FIXED", "length": 2}), field("flag", "BOOLEAN"),
        field("values", array(nullable("STRING"))),
        field("custom", block("STRING", "Names", True)), field("desc", "DESC"),
        field("missing", "STRING", True),
    ))), field("errors", array("ERROR"), True), field("extensions", "EXTENSIONS", True),
)
VALUE = {
    "data": {"text": "hello\x00世界", "blob": b"\x00\xff", "integer": -(1 << 63),
             "float": -0.25, "fixed": b"ab", "flag": True,
             "values": ["same", None, "same", ""], "custom": "hello",
             "desc": {"bytes": b"ab", "false": False, "inner": [3.5, 42, None]}},
    "errors": [{"message": "failure", "locations": [{"line": 2, "column": 4}],
                "path": ["values", 0], "extensions": {"retry": False, "raw": b"\x80"}}],
    "extensions": {"timing": 3, "nested": {"array": [None, "same", True]}},
}


@pytest.mark.parametrize("bits", range(128))
def test_all_header_modes_with_populated_values(bits):
    header = Header(**dict(zip((
        "inline_everything", "self_describing", "out_of_band_field_errors",
        "self_describing_errors", "null_terminated_strings", "no_deduplication",
    ), [bool(bits & (1 << i)) for i in range(6)])),
        user_flags=(True, False, True) if bits & 64 else None)
    raw = write(VALUE, SCHEMA, header=header)
    document = read(raw, SCHEMA, preserve_layout=True)
    assert document.value == VALUE
    assert document.header == header
    assert write(document) == raw
    assert write(document, preserve_layout=True) == raw
    changed = copy.deepcopy(VALUE)
    changed["data"]["integer"] = 17
    rewritten = write(changed, SCHEMA, header=header)
    assert rewritten != raw
    assert read(rewritten, SCHEMA).value == changed


@pytest.mark.parametrize(("schema", "value", "hex_value"), [
    ("VARINT", 42, "0c025400"),
    ("STRING", "A", "0c02410202"),
    ("BOOLEAN", True, "0c0202"),
    ({"type": "FIXED", "length": 2}, b"ab", "0c046162"),
    (nullable("VARINT"), 7, "0c020e0200"),
])
def test_fixed_spec_vectors(schema, value, hex_value):
    raw = bytes.fromhex(hex_value)
    assert read(raw, schema).value == value
    assert write(value, schema) == raw


@pytest.mark.parametrize("header", [
    Header(out_of_band_field_errors=False, self_describing_errors=False),
    Header(out_of_band_field_errors=False, self_describing_errors=True),
    Header(inline_everything=True, out_of_band_field_errors=False),
])
def test_in_band_field_errors(header):
    value = {"data": FieldErrors((VALUE["errors"][0],))}
    raw = write(value, SCHEMA, header=header)
    result = read(raw, SCHEMA)
    assert result.value == value
    assert write(result) == raw


def test_field_errors_null_absent_distinct_and_loss_rejected():
    schema = record(field("a", nullable("STRING"), True))
    values = [{}, {"a": None}, {"a": FieldErrors()}, {"a": ""}]
    packets = [write(value, schema) for value in values]
    assert len(set(packets)) == 4
    assert [read(raw, schema).value for raw in packets] == values
    with pytest.raises(ArgoError, match="out-of-band"):
        write(FieldErrors(({"message": "x"},)), nullable("STRING"))
    with pytest.raises(ArgoError, match="self-describing"):
        write(FieldErrors(), nullable("STRING"), header=Header(self_describing=True))
    assert read(write(NonNull(None), nullable("DESC")), nullable("DESC")).value == NonNull(None)
    with pytest.raises(ArgoError, match="cannot distinguish"):
        write(NonNull(None), nullable(nullable("STRING")))


@pytest.mark.parametrize("inline", [False, True])
def test_schema_free_self_describing(inline):
    value = {"a": [False, None, b"\x00", "same", "same", 7, -3.25], "b": {}}
    raw = write(value, header=Header(self_describing=True, inline_everything=inline))
    assert read(raw).value == value
    assert write(read(raw)) == raw
    with pytest.raises(ArgoError, match="schema required"):
        read(b"\x00\x00")
    with pytest.raises(ArgoError, match="schema required"):
        write(value)


def test_external_schema_replacement_and_registry(tmp_path):
    first = record(field("left", "STRING"))
    second = record(field("right", "STRING"))
    raw = write({"left": "value"}, first)
    assert read(raw, second).value == {"right": "value"}
    import json
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(first))
    assert Codec(load_schema(path)).read(raw).value == {"left": "value"}
    registry = OperationRegistry({"First": first, "Second": second}, {"1": "First", "2": "Second"})
    assert registry.codec_for(2).read(raw).value == {"right": "value"}
    assert OperationRegistry({"First": first}, {"First": {"doc_id": 123}}).operation(123) == "First"
    with pytest.raises(ArgoError, match="unknown document"):
        registry.codec_for(3)
    with pytest.raises(ArgoError):
        OperationRegistry({"First": first}, {"1": "Missing"})
    first["fields"][0]["name"] = "changed"
    assert registry.schema_for(1)["fields"][0]["name"] == "left"


def test_noncanonical_literal_layout_and_changed_values():
    # Two literal A values are valid even though the second could be a reference.
    raw = bytes.fromhex("0c04414106040202")
    schema = array("STRING")
    document = read(raw, schema, preserve_layout=True)
    assert document.value == ["A", "A"]
    assert write(document) != raw
    assert write(document, preserve_layout=True) == raw
    document.value[0] = "B"
    changed = write(document, preserve_layout=True)
    assert changed != raw and read(changed, schema).value == ["B", "A"]
    canonical = read(write(["A", "A"], schema), schema, preserve_layout=True)
    canonical.value[1] = "C"
    with pytest.raises(ArgoError, match="backreference"):
        write(canonical, preserve_layout=True)
    assert read(write(canonical), schema).value == ["A", "C"]
    with pytest.raises(ArgoError, match="requires read"):
        write(["A"], schema, preserve_layout=True)
    with pytest.raises(ArgoError, match="schema/header changed"):
        write(canonical, header=Header(no_deduplication=True), preserve_layout=True)


def test_self_describing_record_order_layout():
    schema = record(field("a", "STRING"), field("b", "STRING"))
    other = record(field("b", "STRING"), field("a", "STRING"))
    raw = write({"a": "1", "b": "2"}, other, header=Header(self_describing=True))
    document = read(raw, schema, preserve_layout=True)
    assert write(document) != raw
    assert write(document, preserve_layout=True) == raw


@pytest.mark.parametrize("schema, value", [
    ("BOOLEAN", 1), ("VARINT", True), ("VARINT", 1 << 63),
    ("STRING", b"text"), ("BYTES", "text"), ("FLOAT64", 1),
    ({"type": "FIXED", "length": 2}, b"x"), (array("STRING"), "x"),
    ("EXTENSIONS", []),
    ("ERROR", {"message": 1}), ("ERROR", {"message": "x", "path": [-1]}),
    ("ERROR", {"message": "x", "locations": [{"line": True, "column": 1}]}),
    (record(field("x", "STRING")), {}), (record(), {"x": 1}),
])
def test_invalid_values_fail(schema, value):
    with pytest.raises(ArgoError):
        write(value, schema)


def test_paths_and_root_override():
    root = record(field("objects", array(record(field("name", "STRING")))))
    value = ["objects", 2, "name"]
    raw = write(value, "PATH", path_schema=root)
    assert read(raw, "PATH", path_schema=root).value == value
    with pytest.raises(ArgoError, match="record root"):
        write(value, "PATH")
    for path in [["missing"], ["objects", "wrong"], ["objects", 2, "name", "extra"]]:
        with pytest.raises(ArgoError):
            write(path, "PATH", path_schema=root)


def test_empty_errors_and_extensions_preserve_presence():
    for error in [{"message": "x"}, {"message": "x", "locations": [], "path": [], "extensions": {}}]:
        for header in [Header(), Header(self_describing_errors=False)]:
            raw = write(error, "ERROR", header=header, path_schema=record())
            assert read(raw, "ERROR", path_schema=record()).value == error


def test_field_error_full_paths_preserve_missing_and_null():
    errors = FieldErrors([{"message": "x"}, {"message": "y", "path": None},
                          {"message": "z", "path": ["name"]}])
    assert errors.full_paths(["users", 2]) == (
        {"message": "x"}, {"message": "y", "path": None},
        {"message": "z", "path": ["users", 2, "name"]},
    )
    assert errors.errors[-1]["path"] == ["name"]
    with pytest.raises(ArgoError):
        FieldErrors(None)


def test_truncations_suffixes_and_invalid_core():
    raw = write(VALUE, SCHEMA)
    for i in range(len(raw)):
        with pytest.raises(ArgoError):
            read(raw[:i], SCHEMA)
    for suffix in [b"\x00", b"\x00\x00", b"\xff", b"extra"]:
        with pytest.raises(ArgoError):
            read(raw + suffix, SCHEMA)
    for raw, schema in [
        (b"\x01\x04", "BOOLEAN"), (b"\x01\x03", "STRING"),
        (b"\x01\x02\xff", "STRING"), (b"\x01\x07", "STRING"),
        (b"\x01\x03", array("STRING")), (b"\x03\x10", "DESC"),
    ]:
        with pytest.raises(ArgoError):
            read(raw, schema)


def test_limits_cyclic_values_and_block_conflicts():
    with pytest.raises(ArgoError):
        write("abcd", "STRING", limits=Limits(max_string_bytes=3))
    with pytest.raises(ArgoError):
        read(write("abcd", "STRING"), "STRING", limits=Limits(max_string_bytes=3))
    for limit in [Limits(max_container_items=1), Limits(max_total_values=2), Limits(max_depth=2)]:
        with pytest.raises(ArgoError):
            write([[[1, 2]]], "DESC", limits=limit)
    value = []
    value.append(value)
    with pytest.raises(ArgoError, match="depth"):
        write(value, "DESC")
    conflict = record(field("a", block("STRING", "X", True)), field("b", block("BYTES", "X", True)))
    with pytest.raises(ArgoError, match="conflicting"):
        write({"a": "hello", "b": b"x"}, conflict)
    with pytest.raises(ArgoError):
        write([1], array(block("VARINT", dedupe=True)))


def test_float_special_values_and_layout():
    for value in [float("inf"), -float("inf"), float("nan"), 0.0, -0.0]:
        raw = write(value, "FLOAT64")
        result = read(raw, "FLOAT64", preserve_layout=True)
        assert math.isnan(result.value) if math.isnan(value) else result.value == value
        assert write(result, preserve_layout=True) == raw


def test_reusable_codec_has_no_shared_dedup_state():
    codec = Codec(array("STRING"))
    expected = codec.write(["x", "x"])
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(lambda _: codec.write(["x", "x"]), range(20))) == [expected] * 20


@pytest.mark.parametrize("path_mode", ["relative", "root"])
def test_inline_error_path_scope_restored_between_fields(path_mode):
    schema = record(field("data", record(
        field("left", nullable(record(field("name", "STRING")))),
        field("right", nullable(record(field("code", "STRING")))),
        field("path", "PATH"),
    )))
    left_path = ["name"] if path_mode == "relative" else ["left", "name"]
    right_path = ["code"] if path_mode == "relative" else ["right", "code"]
    value = {"data": {
        "left": FieldErrors([{"message": "left", "path": left_path}]),
        "right": FieldErrors([{"message": "right", "path": right_path}]),
        "path": ["right", "code"],
    }}
    header = Header(inline_everything=True, out_of_band_field_errors=False, self_describing_errors=False)
    # Literal inline labels: FieldErrors (-3), count 1, message, absent
    # locations (-2), PATH ordinals, absent extensions (-2), then root PATH.
    left_wire_path = "0200" if path_mode == "relative" else "040000"
    right_wire_path = "0200" if path_mode == "relative" else "040200"
    raw = bytes.fromhex(
        "010502086c65667403" + left_wire_path + "03"
        "05020a726967687403" + right_wire_path + "03040200"
    )
    codec = Codec(schema, inline_error_paths=path_mode)
    assert codec.write(value, header=header) == raw
    document = codec.read(raw, preserve_layout=True)
    assert document.value == value
    assert codec.write(document, preserve_layout=True) == raw


def test_codec_context_is_discarded_after_failed_read_or_write():
    schema = array("STRING")
    codec = Codec(schema, limits=Limits(max_total_values=3))
    payload = codec.write(["same", "same"])
    with pytest.raises(ArgoError, match="total item limit"):
        codec.write(["same", "same", "same"])
    oversized = write(["same", "same", "same"], schema)
    with pytest.raises(ArgoError, match="total item limit"):
        codec.read(oversized)

    from concurrent.futures import ThreadPoolExecutor

    def rewrite(_):
        document = codec.read(payload, preserve_layout=True)
        assert document.value == ["same", "same"]
        return codec.write(document, preserve_layout=True)

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(rewrite, range(20))) == [payload] * 20


@pytest.mark.parametrize("kind", ["RECORD", "NULLABLE"])
def test_raised_depth_limit_retains_original_recursive_capacity(kind):
    import sys

    schema, value = "STRING", "x"
    for _ in range(600):
        if kind == "RECORD":
            schema = record(field("x", schema))
            value = {"x": value}
        else:
            schema = nullable(schema)

    # Both shapes encode only String("x"): required RECORD fields and labeled
    # non-null values add no bytes. Do not recursively compare the deep dicts.
    expected = bytes.fromhex("0c02780202")
    previous = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        limits = Limits(max_depth=650)
        assert write(value, schema, limits=limits) == expected
        decoded = read(expected, schema, limits=limits).value
        if kind == "RECORD":
            for _ in range(600):
                decoded = decoded["x"]
        assert decoded == "x"
    finally:
        sys.setrecursionlimit(previous)
