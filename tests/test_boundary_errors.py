"""Public error handling at interpreter limits and path helper boundaries."""
import sys

import pytest

from whatsapp_argo import (
    ArgoError, FieldErrors, Header, Limits, OperationRegistry, load_schema, read,
    read_schema_store, write, write_schema_store,
)


@pytest.fixture
def recursion_limit():
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        yield
    finally:
        sys.setrecursionlimit(previous)


@pytest.mark.parametrize("operation", ["read", "write", "read-store", "write-store"])
def test_interpreter_recursion_limit_raises_argo_error(operation, recursion_limit):
    limits = Limits(max_depth=5000)
    with pytest.raises(ArgoError, match="recursion capacity") as caught:
        if operation == "write":
            value = 0
            for _ in range(1500):
                value = [value]
            write(value, "DESC", limits=limits)
        elif operation == "read":
            # Inline, self-describing: 1,500 singleton arrays around Int(0).
            read(b"\x03" + b"\x06\x02" * 1500 + b"\x0c\x00", limits=limits)
        elif operation == "read-store":
            # Inline store with Q: ARRAY repeated 1,500 times, then STRING.
            read_schema_store(b"\x0d\x04\x02\x02Q" + b"\x11" * 1500 + b"\x01", limits)
        else:
            schema = "STRING"
            for _ in range(1500):
                schema = {"type": "ARRAY", "of": schema}
            write_schema_store({"Q": schema}, limits=limits)
    assert isinstance(caught.value.__cause__, RecursionError)


@pytest.mark.skipif(not hasattr(sys, "set_int_max_str_digits"), reason="Python has no integer digit limit")
def test_json_integer_conversion_limit_raises_argo_error(tmp_path):
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        path = tmp_path / "schema.json"
        path.write_text('{"type":"FIXED","length":' + "1" * 641 + "}", encoding="utf-8")
        with pytest.raises(ArgoError, match="invalid schema JSON") as caught:
            load_schema(path)
        assert isinstance(caught.value.__cause__, ValueError)
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.skipif(not hasattr(sys, "set_int_max_str_digits"), reason="Python has no integer digit limit")
@pytest.mark.parametrize("operation", ["mapping", "asset", "operation", "schema_for", "codec_for"])
def test_document_identifier_conversion_limit_raises_argo_error(operation):
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        identifier = 10 ** 640
        registry = OperationRegistry({"Q": "STRING"}, {42: "Q"})
        with pytest.raises(ArgoError, match="identifier.*conversion limit") as caught:
            if operation == "mapping":
                OperationRegistry({"Q": "STRING"}, {identifier: "Q"})
            elif operation == "asset":
                OperationRegistry({"Q": "STRING"}, {"Q": {"doc_id": identifier}})
            else:
                getattr(registry, operation)(identifier)
        assert isinstance(caught.value.__cause__, ValueError)
        assert registry.operation(42) == "Q"
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.skipif(not hasattr(sys, "set_int_max_str_digits"), reason="Python has no integer digit limit")
def test_long_document_identifiers_keep_text_and_unrestricted_integer_support():
    previous = sys.get_int_max_str_digits()
    try:
        identifier = 10 ** 640
        text = "1" + "0" * 640
        sys.set_int_max_str_digits(0)
        registry = OperationRegistry({"Q": "STRING"}, {identifier: "Q"})
        assert registry.operation(identifier) == "Q"
        sys.set_int_max_str_digits(640)
        assert registry.operation(text) == "Q"
        assert OperationRegistry({"Q": "STRING"}, {text: "Q"}).operation(text) == "Q"
    finally:
        sys.set_int_max_str_digits(previous)


def test_schema_path_errors_are_not_reclassified_as_json_errors():
    with pytest.raises(ValueError, match="embedded null"):
        load_schema("invalid\0schema.json")


def test_full_paths_without_relative_paths_does_not_consume_prefix():
    errors = FieldErrors([{"message": "a"}, {"message": "b", "path": None}])
    prefix = iter(["data"])
    assert errors.full_paths(prefix) == errors.errors
    assert list(prefix) == ["data"]
    assert errors.full_paths(None) == errors.errors


def test_field_error_full_paths_accepts_tuple_paths_without_mutation():
    original = {"message": "bad", "path": ("users", 0)}
    errors = FieldErrors([original])
    expected = ({"message": "bad", "path": ["data", "users", 0]},)
    assert errors.full_paths(("data",)) == expected
    assert original["path"] == ("users", 0)
    # The same tuple path is already valid input to ERROR encoding.
    schema = {"type": "NULLABLE", "of": "DESC"}
    raw = write(errors, schema, header=Header(out_of_band_field_errors=False))
    assert read(raw, schema).value.full_paths(["data"]) == expected


def test_field_error_full_paths_materializes_prefix_once():
    errors = FieldErrors([
        {"message": "a", "path": ["first"]},
        {"message": "b", "path": ["second"]},
        {"message": "c", "path": None},
        {"message": "d"},
    ])
    assert errors.full_paths(iter(["data", 2])) == (
        {"message": "a", "path": ["data", 2, "first"]},
        {"message": "b", "path": ["data", 2, "second"]},
        {"message": "c", "path": None},
        {"message": "d"},
    )
