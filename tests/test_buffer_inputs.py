"""Buffer lifetimes, byte lengths and error recovery at public entry points."""
import pytest

from whatsapp_argo import (
    ArgoError, Codec, Header, Limits, load_schema_store, read, read_schema_store, write,
)
from whatsapp_argo.binary import MessageReader, MessageWriter, Reader


@pytest.fixture
def released_view():
    view = memoryview(b"abcd")
    view.release()
    return view


@pytest.mark.parametrize("reader", [Reader, MessageReader, read, read_schema_store, load_schema_store, Codec().read])
def test_readers_reject_released_memoryview_with_argo_error(reader, released_view):
    with pytest.raises(ArgoError, match="bytes-like") as caught:
        reader(released_view)
    assert isinstance(caught.value.__cause__, ValueError)


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("schema", [
    "BYTES", "DESC", {"type": "FIXED", "length": 4},
    {"type": "BLOCK", "key": "Custom", "dedupe": True, "of": "BYTES"},
    {"type": "BLOCK", "key": "Custom", "dedupe": False, "of": {"type": "FIXED", "length": 4}},
])
def test_failed_buffer_write_does_not_poison_codec(schema, inline, released_view):
    codec = Codec(schema)
    header = Header(inline_everything=inline)
    with pytest.raises(ArgoError, match="bytes-like") as caught:
        codec.write(released_view, header=header)
    assert isinstance(caught.value.__cause__, ValueError)
    raw = codec.write(b"abcd", header=header)
    assert codec.read(raw).value == b"abcd"


@pytest.mark.parametrize("operation", ["extend", "slice", "iadd"])
def test_failed_buffer_mutation_preserves_contents_and_budget(operation, released_view):
    writer = MessageWriter(Header(inline_everything=True), Limits(max_message_bytes=5))
    writer.core.extend(b"ab")
    with pytest.raises(ArgoError, match="bytes-like"):
        if operation == "extend":
            writer.core.extend(released_view)
        elif operation == "slice":
            writer.core[:] = released_view
        else:
            writer.core += released_view
    assert writer.core == b"ab"
    writer.core.extend(b"cd")
    with pytest.raises(ArgoError, match="message exceeds limit"):
        writer.core.append(0)
    assert writer.finish() == b"\x0dabcd"


@pytest.mark.parametrize("shape", ["wide", "scalar", "matrix", "stride", "reverse", "empty"])
@pytest.mark.parametrize("inline", [False, True])
def test_live_memoryview_shapes_keep_byte_semantics(shape, inline):
    view = memoryview(bytearray(b"abcd"))
    if shape == "wide":
        view = view.cast("I")
    elif shape == "scalar":
        view = view.cast("I", shape=[])
    elif shape == "matrix":
        view = view.cast("B", shape=[2, 2])
    elif shape == "stride":
        view = view[::2]
    elif shape == "reverse":
        view = view[::-1]
    elif shape == "empty":
        view = view[:0]
    expected = bytes(view)
    header = Header(inline_everything=inline)
    for schema in ("BYTES", "DESC", {"type": "FIXED", "length": len(expected)}):
        raw = write(view, schema, header=header)
        assert raw == write(expected, schema, header=header)
        document = read(memoryview(raw), schema, preserve_layout=True)
        assert document.value == expected
        assert write(document, preserve_layout=True) == raw
    assert Reader(view).take(len(expected)) == expected


def test_failed_buffer_read_does_not_poison_codec(released_view):
    codec = Codec("BYTES")
    raw = codec.write(b"abcd")
    with pytest.raises(ArgoError, match="bytes-like"):
        codec.read(released_view)
    assert codec.read(raw).value == b"abcd"


@pytest.mark.parametrize("shape", ["wide", "scalar", "matrix", "stride"])
def test_message_view_limit_counts_bytes_instead_of_elements(shape):
    # Header, empty Bytes block, one-byte Core, then zero-length Bytes label.
    raw = b"\x0c\x00\x02\x00"
    if shape == "stride":
        view = memoryview(b"\x0cx\x00x\x02x\x00x")[::2]
    else:
        view = memoryview(raw)
        if shape == "wide":
            view = view.cast("I")
        elif shape == "scalar":
            view = view.cast("I", shape=[])
        else:
            view = view.cast("B", shape=[2, 2])
    assert read(view, "BYTES", limits=Limits(max_message_bytes=4)).value == b""
    with pytest.raises(ArgoError, match="message exceeds limit"):
        read(view, "BYTES", limits=Limits(max_message_bytes=3))
