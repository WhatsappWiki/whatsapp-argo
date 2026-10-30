"""Bounded Argo 1.2 labels, headers, and message framing.

The protocol behavior follows the Argo specification and WhatsApp's MIT-licensed
erlang-argo implementation (Meta Platforms, Inc. and affiliates; WhatsApp LLC).
See the project's third-party notice for attribution.
"""
from __future__ import annotations

from dataclasses import dataclass


class ArgoError(ValueError):
    """Invalid schema, value, wire data, or an exceeded configured limit."""


@dataclass(frozen=True)
class Limits:
    max_message_bytes: int = 16 * 1024 * 1024
    max_string_bytes: int = 8 * 1024 * 1024
    max_container_items: int = 100_000
    max_depth: int = 128
    max_blocks: int = 4096
    max_user_flag_bits: int = 4096
    max_total_values: int = 1_000_000

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < 0:
                raise ArgoError(f"{name} must be a nonnegative integer")


DEFAULT_LIMITS = Limits()


def label_bytes(value: int) -> bytes:
    """Encode one signed 64-bit zig-zag label using minimal varint bytes."""
    if type(value) is not int or not -(1 << 63) <= value < (1 << 63):
        raise ArgoError("label must be a signed 64-bit integer")
    unsigned = (value << 1) if value >= 0 else ((-value << 1) - 1)
    out = bytearray()
    while unsigned >= 128:
        out.append((unsigned & 127) | 128)
        unsigned >>= 7
    out.append(unsigned)
    return bytes(out)


def _binary(data, description="data") -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ArgoError(f"{description} must be bytes-like")
    return bytes(data)


class Reader:
    """One bounded cursor; each label must use the shortest representation."""
    def __init__(self, data, name="", limits=DEFAULT_LIMITS):
        self.data = _binary(data)
        self.pos = 0
        self.name = name
        self.limits = limits

    @property
    def remaining(self):
        return len(self.data) - self.pos

    def take(self, size):
        if type(size) is not int or size < 0 or size > self.remaining:
            raise ArgoError(f"invalid length or truncated {self.name}")
        start = self.pos
        self.pos += size
        return self.data[start:self.pos]

    def label(self):
        start = self.pos
        value = 0
        for shift in range(0, 70, 7):
            byte = self.take(1)[0]
            value |= (byte & 127) << shift
            if byte < 128:
                if value >= 1 << 64:
                    raise ArgoError("label exceeds uint64")
                decoded = (value >> 1) ^ -(value & 1)
                if self.data[start:self.pos] != label_bytes(decoded):
                    raise ArgoError("nonminimal label")
                return decoded
        raise ArgoError("label exceeds 10 bytes")

    def peek_label(self):
        position = self.pos
        try:
            return self.label()
        finally:
            self.pos = position

    def eof(self):
        if self.remaining:
            raise ArgoError(f"unconsumed bytes in {self.name}")


@dataclass(frozen=True)
class Header:
    """Argo mode bits and optional user bits.

    User bits are encoded most-significant-bit first in groups of seven, with
    the low bit of each byte indicating continuation. They are normalized to
    whole seven-bit groups because the wire does not carry the unpadded length.
    ``None`` means HasUserFlags is unset; ``()`` sets it with seven zero bits.
    """
    inline_everything: bool = False
    self_describing: bool = False
    out_of_band_field_errors: bool = True
    self_describing_errors: bool = True
    null_terminated_strings: bool = False
    no_deduplication: bool = False
    user_flags: tuple[bool, ...] | None = None

    def __post_init__(self):
        for name in (
            "inline_everything", "self_describing", "out_of_band_field_errors",
            "self_describing_errors", "null_terminated_strings", "no_deduplication",
        ):
            if type(getattr(self, name)) is not bool:
                raise ArgoError(f"{name} must be a boolean")
        if self.user_flags is not None:
            if not isinstance(self.user_flags, (tuple, list)):
                raise ArgoError("user_flags must be a sequence of booleans or None")
            flags = tuple(self.user_flags)
            if any(type(flag) is not bool for flag in flags):
                raise ArgoError("user flags must be booleans")
            padded_length = max(7, ((len(flags) + 6) // 7) * 7)
            flags += (False,) * (padded_length - len(flags))
            object.__setattr__(self, "user_flags", flags)

    @property
    def has_user_flags(self):
        return self.user_flags is not None

    @property
    def flags(self):
        return sum(int(value) << index for index, value in enumerate((
            self.inline_everything, self.self_describing,
            self.out_of_band_field_errors, self.self_describing_errors,
            self.null_terminated_strings, self.no_deduplication, self.has_user_flags,
        )))

    @classmethod
    def from_reader(cls, reader):
        first = reader.take(1)[0]
        if first & 0x80:
            raise ArgoError("reserved header bit is set")
        user_flags = None
        if first & 0x40:
            user_flags = []
            while True:
                byte = reader.take(1)[0]
                if len(user_flags) + 7 > reader.limits.max_user_flag_bits:
                    raise ArgoError("user flags exceed limit")
                user_flags.extend(bool(byte & (1 << bit)) for bit in range(7, 0, -1))
                if byte & 1 == 0:
                    break
        return cls(**dict(zip((
            "inline_everything", "self_describing", "out_of_band_field_errors",
            "self_describing_errors", "null_terminated_strings", "no_deduplication",
        ), (bool(first & (1 << bit)) for bit in range(6)))),
            user_flags=None if user_flags is None else tuple(user_flags))

    def to_bytes(self):
        out = bytearray((self.flags,))
        if self.user_flags is not None:
            for offset in range(0, len(self.user_flags), 7):
                chunk = self.user_flags[offset:offset + 7]
                byte = sum(int(bit) << (7 - index) for index, bit in enumerate(chunk))
                if offset + 7 < len(self.user_flags):
                    byte |= 1
                out.append(byte)
        return bytes(out)


class MessageReader:
    """Parse framing without accepting ignored suffixes or unused blocks."""
    def __init__(self, raw, limits=DEFAULT_LIMITS):
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise ArgoError("message must be bytes-like")
        raw_length = raw.nbytes if isinstance(raw, memoryview) else len(raw)
        if raw_length > limits.max_message_bytes:
            raise ArgoError("message exceeds limit")
        self.limits = limits
        framing = Reader(raw, name="message", limits=limits)
        self.header = Header.from_reader(framing)
        self._assigned = {}
        self._next = 0
        if self.header.inline_everything:
            self._blocks = []
            self.core = Reader(framing.take(framing.remaining), name="core", limits=limits)
        else:
            blocks = []
            while framing.remaining:
                if len(blocks) >= limits.max_blocks + 1:
                    raise ArgoError("too many message blocks")
                length = framing.label()
                if length < 0:
                    raise ArgoError("negative block length")
                blocks.append(framing.take(length))
            if not blocks:
                raise ArgoError("missing core")
            self._blocks = blocks[:-1]
            self.core = Reader(blocks[-1], name="core", limits=limits)

    def block(self, key):
        if self.header.inline_everything:
            return self.core
        if key not in self._assigned:
            if self._next >= len(self._blocks):
                raise ArgoError(f"missing scalar block {key!r}")
            self._assigned[key] = Reader(self._blocks[self._next], name=str(key), limits=self.limits)
            self._next += 1
        return self._assigned[key]

    def finish(self):
        self.core.eof()
        if self._next != len(self._blocks):
            raise ArgoError("unassigned scalar block")
        for block in self._assigned.values():
            block.eof()


class _WriteBudget:
    def __init__(self, initial_size, limit):
        self.size, self.limit = initial_size, limit
        self.claim(0)

    def claim(self, size):
        if self.size + size > self.limit:
            raise ArgoError("message exceeds limit")
        self.size += size


class _BoundedBuffer(bytearray):
    """Account for payload growth before any bytearray allocation occurs."""
    def __init__(self, budget):
        super().__init__()
        self._budget = budget

    def extend(self, value):
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ArgoError("internal message buffer requires bytes-like data")
        size = value.nbytes if isinstance(value, memoryview) else len(value)
        self._budget.claim(size)
        try:
            super().extend(value)
        except BaseException:
            self._budget.claim(-size)
            raise

    def append(self, value):
        self._budget.claim(1)
        try:
            super().append(value)
        except BaseException:
            self._budget.claim(-1)
            raise

    def __iadd__(self, value):
        self.extend(value)
        return self

    def __setitem__(self, key, value):
        if not isinstance(key, slice):
            return super().__setitem__(key, value)
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ArgoError("internal message buffer requires bytes-like data")
        start, stop, step = key.indices(len(self))
        removed = len(range(start, stop, step))
        added = value.nbytes if isinstance(value, memoryview) else len(value)
        growth = added - removed
        self._budget.claim(growth)
        try:
            super().__setitem__(key, value)
        except BaseException:
            self._budget.claim(-growth)
            raise

    def __delitem__(self, key):
        old_length = len(self)
        super().__delitem__(key)
        self._budget.claim(len(self) - old_length)

    def __imul__(self, count):
        if type(count) is not int:
            raise ArgoError("message buffer multiplier must be integer")
        growth = len(self) * max(0, count) - len(self)
        self._budget.claim(growth)
        try:
            super().__imul__(count)
        except BaseException:
            self._budget.claim(-growth)
            raise
        return self

    def clear(self):
        size = len(self)
        super().clear()
        self._budget.claim(-size)

    def pop(self, index=-1):
        result = super().pop(index)
        self._budget.claim(-1)
        return result

    def remove(self, value):
        super().remove(value)
        self._budget.claim(-1)

    def insert(self, index, value):
        self._budget.claim(1)
        try:
            super().insert(index, value)
        except BaseException:
            self._budget.claim(-1)
            raise


class MessageWriter:
    """Construct fresh scalar blocks and core; no original message is cached."""
    def __init__(self, header=Header(), limits=DEFAULT_LIMITS):
        if not isinstance(header, Header):
            raise ArgoError("header must be Header")
        if header.user_flags is not None and len(header.user_flags) > limits.max_user_flag_bits:
            raise ArgoError("user flags exceed limit")
        header_size = 1 + (0 if header.user_flags is None else len(header.user_flags) // 7)
        self._budget = _WriteBudget(header_size, limits.max_message_bytes)
        self.header, self.limits = header, limits
        self.core = _BoundedBuffer(self._budget)
        self._blocks = {}

    def block(self, key):
        if self.header.inline_everything:
            return self.core
        if key not in self._blocks:
            if len(self._blocks) >= self.limits.max_blocks:
                raise ArgoError("too many scalar blocks")
            self._blocks[key] = _BoundedBuffer(self._budget)
        return self._blocks[key]

    def finish(self):
        blocks = list(self._blocks.values()) + [self.core]
        size = len(self.header.to_bytes()) + sum(len(block) for block in blocks)
        if not self.header.inline_everything:
            size += sum(len(label_bytes(len(block))) for block in blocks)
        if size > self.limits.max_message_bytes:
            raise ArgoError("message exceeds limit")
        out = bytearray(self.header.to_bytes())
        if self.header.inline_everything:
            out.extend(self.core)
        else:
            for block in blocks:
                out.extend(label_bytes(len(block)))
                out.extend(block)
        return bytes(out)
