"""Argo wire-type store serialization; no filesystem or value-codec dependency.

Wire markers follow WhatsApp's MIT-licensed erlang-argo implementation.
See THIRD_PARTY_NOTICES.md for attribution.
"""
from __future__ import annotations

from whatsapp_argo.binary import ArgoError, DEFAULT_LIMITS, Header, MessageReader, MessageWriter, label_bytes
from whatsapp_argo._schema_validation import MARKERS, SCALARS, SIMPLE_MARKERS, _text, validate_schema_store

__all__ = []


class _StoreCodec:
    def __init__(self, message, reading):
        self.message, self.reading = message, reading
        self.core = message.core
        self.limits = message.limits
        self.seen = []
        self.index = {}
        self.nodes = 0

    def count(self, length):
        if type(length) is not int or not 0 <= length <= self.limits.max_container_items:
            raise ArgoError("invalid schema container count or count exceeds limit")
        return length

    def boolean(self, value=None):
        if self.reading:
            label = self.core.label()
            if label not in (0, 1):
                raise ArgoError("invalid schema boolean")
            return bool(label)
        self.core.extend(label_bytes(int(value)))

    def string(self, value=None):
        dedupe = not self.message.header.no_deduplication and not self.message.header.inline_everything
        terminated = self.message.header.null_terminated_strings
        if self.reading:
            label = self.core.label()
            if label <= -4:
                index = -4 - label
                if not dedupe or index >= len(self.seen):
                    raise ArgoError("invalid schema string backreference")
                return self.seen[index]
            if not 0 <= label <= self.limits.max_string_bytes:
                raise ArgoError("invalid schema string length or length exceeds limit")
            block = self.message.block("String")
            raw = block.take(label)
            if terminated and block.take(1) != b"\0":
                raise ArgoError("invalid string terminator")
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise ArgoError("invalid schema string UTF-8") from exc
            if dedupe:
                self.seen.append(text)
            return text
        _text(value, self.limits, "schema string")
        if dedupe and value in self.index:
            self.core.extend(label_bytes(-4 - self.index[value]))
            return
        raw = value.encode("utf-8")
        self.core.extend(label_bytes(len(raw)))
        block = self.message.block("String")
        block.extend(raw)
        if terminated:
            block.append(0)
        if dedupe:
            self.index[value] = len(self.seen)
            self.seen.append(value)

    def wire(self, wire=None, depth=0):
        if depth > self.limits.max_depth:
            raise ArgoError("schema exceeds depth limit")
        self.nodes += 1
        if self.nodes > self.limits.max_total_values:
            raise ArgoError("schema exceeds node limit")
        if self.reading:
            marker = self.core.label()
            if marker in SIMPLE_MARKERS:
                return {"type": SIMPLE_MARKERS[marker]}
            if marker == -6:
                length = self.core.label()
                if not 0 <= length <= self.limits.max_string_bytes:
                    raise ArgoError("invalid fixed schema length")
                return {"type": "FIXED", "length": length}
            if marker == -7:
                child = self.wire(depth=depth + 1)
                if child["type"] not in SCALARS:
                    raise ArgoError("BLOCK requires a scalar schema")
                return {"type": "BLOCK", "of": child, "key": self.string(), "dedupe": self.boolean()}
            if marker in (-8, -9):
                return {"type": "NULLABLE" if marker == -8 else "ARRAY", "of": self.wire(depth=depth + 1)}
            if marker == -10:
                fields, names = [], set()
                for _ in range(self.count(self.core.label())):
                    name = self.string()
                    if name in names:
                        raise ArgoError("duplicate field name")
                    names.add(name)
                    child = self.wire(depth=depth + 1)
                    fields.append({"name": name, "of": child, "omittable": self.boolean()})
                return {"type": "RECORD", "fields": fields}
            raise ArgoError(f"unknown wire type marker {marker}")
        kind = wire["type"]
        if kind in MARKERS:
            self.core.extend(label_bytes(MARKERS[kind]))
        elif kind == "FIXED":
            self.core.extend(label_bytes(-6) + label_bytes(wire["length"]))
        elif kind == "BLOCK":
            self.core.extend(label_bytes(-7))
            self.wire(wire["of"], depth + 1)
            self.string(wire["key"])
            self.boolean(wire["dedupe"])
        elif kind in ("NULLABLE", "ARRAY"):
            self.core.extend(label_bytes(-8 if kind == "NULLABLE" else -9))
            self.wire(wire["of"], depth + 1)
        elif kind == "RECORD":
            self.core.extend(label_bytes(-10) + label_bytes(len(wire["fields"])))
            for field in wire["fields"]:
                self.string(field["name"])
                self.wire(field["of"], depth + 1)
                self.boolean(field["omittable"])


def read_schema_store(raw, limits=DEFAULT_LIMITS):
    """Read a complete Argo wire-type store and require every block at EOF."""
    message = MessageReader(raw, limits)
    codec = _StoreCodec(message, True)
    if message.core.label() != 2:
        raise ArgoError("not a wire-type store")
    result = {}
    try:
        for _ in range(codec.count(message.core.label())):
            name = codec.string()
            if name in result:
                raise ArgoError("duplicate operation name")
            result[name] = codec.wire()
    except RecursionError as exc:
        raise ArgoError("schema nesting exceeds Python recursion capacity") from exc
    message.finish()
    return result


def write_schema_store(store, header=Header(), limits=DEFAULT_LIMITS):
    """Generate a store from current schemas; no encoded input is retained."""
    store = validate_schema_store(store, limits)
    message = MessageWriter(header, limits)
    codec = _StoreCodec(message, False)
    message.core.extend(label_bytes(2) + label_bytes(len(store)))
    try:
        for name, wire in store.items():
            codec.string(name)
            codec.wire(wire)
    except RecursionError as exc:
        raise ArgoError("schema nesting exceeds Python recursion capacity") from exc
    return message.finish()
