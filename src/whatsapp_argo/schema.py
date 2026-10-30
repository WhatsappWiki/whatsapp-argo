"""External schema validation and Argo wire-type store read/write.

Wire markers and store behavior follow WhatsApp's MIT-licensed erlang-argo
implementation. This module contains no bundled WhatsApp operation schemas.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from os import PathLike
from pathlib import Path

from .binary import ArgoError, DEFAULT_LIMITS, Header, MessageReader, MessageWriter, label_bytes


SCALAR_MARKERS = {-1: "STRING", -2: "BOOLEAN", -3: "VARINT", -4: "FLOAT64", -5: "BYTES", -11: "DESC"}
SIMPLE_MARKERS = {**SCALAR_MARKERS, -12: "ERROR", -13: "PATH", -15: "EXTENSIONS"}
MARKERS = {kind: marker for marker, kind in SIMPLE_MARKERS.items()}
SCALARS = frozenset(SCALAR_MARKERS.values()) | {"FIXED"}


def _text(value, limits, description):
    if not isinstance(value, str):
        raise ArgoError(f"{description} must be text")
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ArgoError(f"invalid UTF-8 {description}") from exc
    if len(raw) > limits.max_string_bytes:
        raise ArgoError(f"{description} exceeds limit")
    return value


def _keys(value, required, optional=()):
    if not isinstance(value, Mapping):
        raise ArgoError("schema node must be a mapping")
    if set(value) - set(required) - set(optional):
        raise ArgoError("unknown schema property")
    if set(required) - set(value):
        raise ArgoError("missing schema property")


def validate_schema(schema, limits=DEFAULT_LIMITS):
    """Return a validated independent schema mapping.

    Nodes use ``{"type": "STRING"}``, ``ARRAY/NULLABLE`` use ``of``, ``BLOCK``
    additionally has ``key`` and boolean ``dedupe``. RECORD's ordered ``fields``
    contain ``name``, ``of`` and optional boolean ``omittable`` (default false).
    Scalar names may also be supplied as shorthand strings.
    """
    return _validate_schema(schema, limits, [0])


def _validate_schema(schema, limits, budget):
    active = set()

    def visit(node, depth):
        if depth > limits.max_depth:
            raise ArgoError("schema exceeds depth limit")
        budget[0] += 1
        if budget[0] > limits.max_total_values:
            raise ArgoError("schema exceeds node limit")
        if isinstance(node, str):
            node = {"type": node}
        if not isinstance(node, Mapping):
            raise ArgoError("schema node must be a mapping")
        identity = id(node)
        if identity in active:
            raise ArgoError("cyclic schema")
        active.add(identity)
        try:
            kind = node.get("type")
            if not isinstance(kind, str):
                raise ArgoError("schema type must be text")
            if kind in MARKERS:
                _keys(node, ("type",))
                return {"type": kind}
            if kind == "FIXED":
                _keys(node, ("type", "length"))
                length = node["length"]
                if type(length) is not int or not 0 <= length <= limits.max_string_bytes:
                    raise ArgoError("invalid fixed length or length exceeds limit")
                return {"type": kind, "length": length}
            if kind in ("ARRAY", "NULLABLE"):
                _keys(node, ("type", "of"))
                return {"type": kind, "of": visit(node["of"], depth + 1)}
            if kind == "BLOCK":
                _keys(node, ("type", "of", "key", "dedupe"))
                child = visit(node["of"], depth + 1)
                if child["type"] not in SCALARS:
                    raise ArgoError("BLOCK requires a scalar type")
                key = _text(node["key"], limits, "block key")
                if type(node["dedupe"]) is not bool:
                    raise ArgoError("dedupe must be boolean")
                return {"type": kind, "of": child, "key": key, "dedupe": node["dedupe"]}
            if kind == "RECORD":
                _keys(node, ("type", "fields"))
                fields = node["fields"]
                if not isinstance(fields, (list, tuple)) or len(fields) > limits.max_container_items:
                    raise ArgoError("invalid record fields or too many fields")
                out, seen = [], set()
                for field in fields:
                    _keys(field, ("name", "of"), ("omittable",))
                    name = _text(field["name"], limits, "field name")
                    if name in seen:
                        raise ArgoError("duplicate field name")
                    seen.add(name)
                    omittable = field.get("omittable", False)
                    if type(omittable) is not bool:
                        raise ArgoError("omittable must be boolean")
                    out.append({"name": name, "of": visit(field["of"], depth + 1), "omittable": omittable})
                return {"type": kind, "fields": out}
            raise ArgoError(f"unsupported schema type {kind!r}")
        finally:
            active.remove(identity)

    try:
        return visit(schema, 0)
    except RecursionError as exc:
        raise ArgoError("schema nesting exceeds Python recursion capacity") from exc


def _json_unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ArgoError("duplicate JSON key")
        out[key] = value
    return out


def _read_file(path, limits):
    path = Path(path)
    if path.stat().st_size > limits.max_message_bytes:
        raise ArgoError("schema configuration exceeds limit")
    with path.open("rb") as stream:
        raw = stream.read(limits.max_message_bytes + 1)
    if len(raw) > limits.max_message_bytes:
        raise ArgoError("schema configuration exceeds limit")
    return raw


def _load_json(path, limits):
    try:
        return json.loads(_read_file(path, limits).decode("utf-8"), object_pairs_hook=_json_unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ArgoError("invalid schema JSON") from exc


def load_schema(source, limits=DEFAULT_LIMITS):
    """Load one schema from a mapping, scalar shorthand, or JSON file path."""
    if isinstance(source, (str, PathLike)) and not (isinstance(source, str) and source in MARKERS):
        source = _load_json(source, limits)
    return validate_schema(source, limits)


def validate_schema_store(store, limits=DEFAULT_LIMITS):
    """Accept an operation→schema mapping or official JSON STORE object."""
    if not isinstance(store, Mapping):
        raise ArgoError("schema store must be a mapping")
    if store.get("type") == "STORE":
        _keys(store, ("type", "types"))
        entries = store["types"]
        if not isinstance(entries, (list, tuple)) or len(entries) > limits.max_container_items:
            raise ArgoError("invalid schema store entries")
        normalized = {}
        for entry in entries:
            _keys(entry, ("name", "type"))
            name = _text(entry["name"], limits, "operation name")
            if name in normalized:
                raise ArgoError("duplicate operation name")
            normalized[name] = entry["type"]
        store = normalized
    if len(store) > limits.max_container_items:
        raise ArgoError("too many operations")
    budget = [0]
    return {_text(name, limits, "operation name"): _validate_schema(wire, limits, budget) for name, wire in store.items()}


def load_schema_store(source, limits=DEFAULT_LIMITS):
    """Load an external mapping/JSON file, or an Argo store bytes object."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return read_schema_store(source, limits)
    if isinstance(source, (str, PathLike)):
        path = Path(source)
        if path.stat().st_size > limits.max_message_bytes:
            raise ArgoError("schema store exceeds limit")
        if path.suffix.lower() == ".json":
            source = _load_json(path, limits)
        else:
            return read_schema_store(_read_file(path, limits), limits)
    return validate_schema_store(source, limits)


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
    for _ in range(codec.count(message.core.label())):
        name = codec.string()
        if name in result:
            raise ArgoError("duplicate operation name")
        result[name] = codec.wire()
    message.finish()
    return result


def write_schema_store(store, header=Header(), limits=DEFAULT_LIMITS):
    """Generate a store from current schemas; no encoded input is retained."""
    store = validate_schema_store(store, limits)
    message = MessageWriter(header, limits)
    codec = _StoreCodec(message, False)
    message.core.extend(label_bytes(2) + label_bytes(len(store)))
    for name, wire in store.items():
        codec.string(name)
        codec.wire(wire)
    return message.finish()
