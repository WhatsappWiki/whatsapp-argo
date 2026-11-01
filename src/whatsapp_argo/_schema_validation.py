"""Pure validation and normalization of schemas and operation stores."""
from __future__ import annotations

from collections.abc import Mapping

from whatsapp_argo.binary import ArgoError, DEFAULT_LIMITS

__all__ = []


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
