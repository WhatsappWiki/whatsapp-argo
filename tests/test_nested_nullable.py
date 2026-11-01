"""Nullable layers around a non-null DESC null must survive fresh encoding."""
import sys

import pytest

from whatsapp_argo import ArgoError, FieldErrors, Header, Limits, NonNull, read, write


def nullable_layers(leaf, depth):
    schema, value = leaf, None
    for _ in range(depth):
        schema = {"type": "NULLABLE", "of": schema}
        value = NonNull(value)
    return schema, value


def header_from_flags(flags):
    return Header(**dict(zip((
        "inline_everything", "self_describing", "out_of_band_field_errors",
        "self_describing_errors", "null_terminated_strings", "no_deduplication",
    ), (bool(flags & (1 << bit)) for bit in range(6)))),
        user_flags=(True,) if flags & 64 else None)


def frame(header, core):
    # These cases need no scalar blocks and their Core lengths fit one Label.
    return header.to_bytes() + (b"" if header.inline_everything else bytes([len(core) * 2])) + core


@pytest.mark.parametrize("depth", [1, 2, 3, 8])
@pytest.mark.parametrize("leaf", ["DESC", {"type": "BLOCK", "key": "Custom", "dedupe": False, "of": "DESC"}])
def test_explicit_nullable_layers_roundtrip_hand_derived_bytes(depth, leaf):
    schema, expected = nullable_layers(leaf, depth)
    for flags in range(128):
        if flags & 2:
            continue
        header = header_from_flags(flags)
        # All outer nullable children share the innermost non-null Label 0;
        # the DESC itself is Null (-1). The number of layers is in the schema.
        raw = frame(header, b"\x00\x01")
        document = read(raw, schema, preserve_layout=True)
        assert document.value == expected
        assert write(expected, schema, header=header) == raw
        assert write(document) == raw
        assert write(document, preserve_layout=True) == raw
        document.value = None
        changed = write(document, preserve_layout=True)
        assert changed == frame(header, b"\x01")
        assert read(changed, schema).value is None


@pytest.mark.parametrize("leaf", ["STRING", "DESC"])
def test_ambiguous_nullable_layers_still_reject_inner_null_and_field_errors(leaf):
    for depth in (2, 3, 8):
        schema, _ = nullable_layers(leaf, depth)
        for terminal in (None, FieldErrors(), FieldErrors([{"message": "bad"}])):
            value = terminal
            for _ in range(depth - 1):
                value = NonNull(value)
                for header in (Header(), Header(inline_everything=True), Header(self_describing=True)):
                    with pytest.raises(ArgoError, match="cannot distinguish"):
                        write(value, schema, header=header)


def test_self_describing_mode_still_rejects_nonnull_desc_null_layers():
    for depth in (1, 2, 3, 8):
        schema, value = nullable_layers("DESC", depth)
        for inline in (False, True):
            with pytest.raises(ArgoError, match="cannot distinguish"):
                write(value, schema, header=Header(self_describing=True, inline_everything=inline))


@pytest.mark.parametrize("inline", [False, True])
def test_nullable_layers_inside_optional_field_and_array(inline):
    child, value = nullable_layers("DESC", 3)
    header = Header(inline_everything=inline)
    record = {"type": "RECORD", "fields": [{"name": "x", "of": child, "omittable": True}]}
    array = {"type": "ARRAY", "of": child}
    for schema, expected, core in (
        (record, {"x": value}, b"\x00\x01"),
        (record, {}, b"\x03"),
        (array, [value, None, FieldErrors()], b"\x06\x00\x01\x01\x05"),
    ):
        raw = frame(header, core)
        document = read(raw, schema, preserve_layout=True)
        assert document.value == expected
        assert write(expected, schema, header=header) == raw
        assert write(document, preserve_layout=True) == raw


def test_nullable_layers_keep_depth_and_total_value_limits():
    schema, value = nullable_layers("DESC", 2)
    header = Header(inline_everything=True)
    raw = frame(header, b"\x00\x01")
    limits = Limits(max_depth=3, max_total_values=4)
    assert write(value, schema, header=header, limits=limits) == raw
    assert read(raw, schema, limits=limits).value == value
    for limits in (Limits(max_depth=2), Limits(max_total_values=3)):
        with pytest.raises(ArgoError, match="value exceeds"):
            write(value, schema, header=header, limits=limits)
        with pytest.raises(ArgoError, match="value exceeds"):
            read(raw, schema, limits=limits)


def test_deep_explicit_nullable_layers_do_not_add_recursive_helper_frames():
    schema, value = nullable_layers("DESC", 600)
    previous = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        limits = Limits(max_depth=650)
        raw = write(value, schema, header=Header(inline_everything=True), limits=limits)
        assert raw == b"\x0d\x00\x01"
        decoded = read(raw, schema, limits=limits).value
        for _ in range(600):
            assert isinstance(decoded, NonNull)
            decoded = decoded.value
        assert decoded is None
    finally:
        sys.setrecursionlimit(previous)
