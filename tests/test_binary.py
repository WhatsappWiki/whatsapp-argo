from dataclasses import replace

import pytest

from whatsapp_argo.binary import ArgoError, Header, Limits, MessageReader, MessageWriter, Reader, label_bytes


@pytest.mark.parametrize("number,encoded", [
    (0, "00"), (-1, "01"), (1, "02"), (-2, "03"), (63, "7e"),
    (-64, "7f"), (64, "8001"), (-65, "8101"),
    ((1 << 63) - 1, "feffffffffffffffff01"), (-(1 << 63), "ffffffffffffffffff01"),
])
def test_label_fixed_vectors(number, encoded):
    raw = bytes.fromhex(encoded)
    assert label_bytes(number) == raw
    reader = Reader(raw)
    assert reader.peek_label() == number
    assert reader.pos == 0
    assert reader.label() == number
    reader.eof()


@pytest.mark.parametrize("number", [True, False, 1.5, "1", None, 1 << 63, -(1 << 63) - 1])
def test_label_rejects_non_int64(number):
    with pytest.raises(ArgoError):
        label_bytes(number)


@pytest.mark.parametrize("raw", [b"", b"\x80", b"\x80\x00", b"\x82\x00", b"\xff" * 10, b"\xff" * 9 + b"\x02"])
def test_label_truncation_overlong_nonminimal(raw):
    with pytest.raises(ArgoError):
        Reader(raw).label()


def test_reader_checks_lengths_and_eof():
    reader = Reader(b"ab")
    for invalid in (-1, 3, True, 0.5):
        with pytest.raises(ArgoError):
            reader.take(invalid)
    with pytest.raises(ArgoError):
        reader.eof()
    assert reader.take(2) == b"ab"
    reader.eof()
    with pytest.raises(ArgoError):
        Reader(10)


@pytest.mark.parametrize("flags", range(128))
def test_every_header_bit_combination(flags):
    raw = bytes([flags]) + (b"\xaa" if flags & 64 else b"")
    reader = Reader(raw)
    header = Header.from_reader(reader)
    reader.eof()
    assert header.flags == flags
    assert header.to_bytes() == raw


def test_user_flags_varbit_continuation_in_low_bit():
    header = Header(user_flags=(True, False, True, False, True, False, True, True))
    assert header.to_bytes() == b"\x4c\xab\x80"
    assert len(header.user_flags) == 14
    assert Header.from_reader(Reader(header.to_bytes())) == header
    assert Header(user_flags=()).to_bytes() == b"\x4c\0"
    assert Header(user_flags=None).to_bytes() == b"\x0c"


@pytest.mark.parametrize("raw", [b"", b"\x80", b"\x40", b"\x40\x01"])
def test_invalid_headers(raw):
    with pytest.raises(ArgoError):
        Header.from_reader(Reader(raw))


def test_header_type_and_limit_validation():
    for kwargs in ({"inline_everything": 1}, {"user_flags": (1,)}, {"user_flags": "1"}):
        with pytest.raises(ArgoError):
            Header(**kwargs)
    with pytest.raises(ArgoError):
        Header.from_reader(Reader(b"\x40\x01\x00", limits=Limits(max_user_flag_bits=7)))
    with pytest.raises(ArgoError):
        MessageWriter(Header(user_flags=(True,) * 8), Limits(max_user_flag_bits=7))
    with pytest.raises(ArgoError):
        Limits(max_depth=-1)


@pytest.mark.parametrize("inline", [False, True])
def test_message_framing_and_consumption(inline):
    header = Header(inline_everything=inline)
    writer = MessageWriter(header)
    writer.core.extend(label_bytes(2))
    writer.block("Bytes").extend(b"ab")
    writer.core.extend(label_bytes(0))
    raw = writer.finish()
    assert raw == (b"\x0d\x04ab\0" if inline else b"\x0c\x04ab\x04\x04\0")
    reader = MessageReader(raw)
    assert reader.header == header
    assert reader.core.label() == 2
    assert reader.block("Bytes").take(2) == b"ab"
    assert reader.core.label() == 0
    reader.finish()


def test_empty_inline_core_and_empty_split_core():
    MessageReader(b"\x0d").finish()
    MessageReader(b"\x0c\0").finish()


@pytest.mark.parametrize("raw", [b"", b"\x0c", b"\x0c\x01", b"\x0c\x04a", b"\x0c\x80\0", b"\x0c\0x"])
def test_framing_rejects_missing_truncated_negative_and_nonminimal(raw):
    with pytest.raises(ArgoError):
        MessageReader(raw)


def test_no_ignored_blocks_or_suffixes():
    reader = MessageReader(b"\x0c\x02a\0")
    with pytest.raises(ArgoError, match="unassigned"):
        reader.finish()
    reader.block("String")
    with pytest.raises(ArgoError, match="unconsumed"):
        reader.finish()
    reader.block("String").take(1)
    reader.finish()
    with pytest.raises(ArgoError, match="missing scalar"):
        reader.block("Other")
    with pytest.raises(ArgoError, match="unconsumed"):
        MessageReader(b"\x0d\0").finish()


def test_message_and_block_limits():
    with pytest.raises(ArgoError, match="message exceeds"):
        MessageReader(b"\x0dabcdef", Limits(max_message_bytes=2))
    with pytest.raises(ArgoError, match="too many"):
        MessageReader(b"\x0c\0\0", Limits(max_blocks=0))
    writer = MessageWriter(limits=Limits(max_blocks=0))
    with pytest.raises(ArgoError, match="too many"):
        writer.block("B")
    writer = MessageWriter(limits=Limits(max_message_bytes=2))
    with pytest.raises(ArgoError, match="message exceeds"):
        writer.core.extend(b"abc")
    writer.core.extend(b"a")
    with pytest.raises(ArgoError, match="message exceeds"):
        writer.finish()
