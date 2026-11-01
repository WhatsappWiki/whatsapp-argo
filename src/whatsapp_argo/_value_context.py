"""Shared wire policies, path validation and per-message resource accounting."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager

from whatsapp_argo.binary import ArgoError, Header, Limits

__all__ = []


DEFAULT_BLOCKS = {
    "STRING": ("String", True), "BYTES": ("Bytes", True),
    "VARINT": ("Int", False), "FLOAT64": ("Float", False),
    "BOOLEAN": ("Boolean", False),
}
SCALAR_MARKERS = {"STRING": 4, "BYTES": 5, "FIXED": 5, "VARINT": 6, "FLOAT64": 7}
SCALARS = {*DEFAULT_BLOCKS, "FIXED"}


def is_labeled(wire):
    kind = wire["type"]
    if kind == "BLOCK":
        kind = wire["of"]["type"]
    return kind in ("STRING", "BYTES", "BOOLEAN", "ARRAY", "NULLABLE")


def _unwrapped(wire):
    while wire["type"] == "NULLABLE":
        wire = wire["of"]
    return wire


def _path_root(schema, override):
    if override is not None:
        return override
    if schema["type"] == "RECORD":
        for field in schema["fields"]:
            if field["name"] == "data":
                return field["of"]
        return schema
    return None


def validate_options(inline_error_paths, error_encoding):
    if inline_error_paths not in ("relative", "root"):
        raise ArgoError("inline_error_paths must be 'relative' or 'root'")
    if error_encoding not in ("omittable", "nullable"):
        raise ArgoError("error_encoding must be 'omittable' or 'nullable'")


class ValueContext:
    """Validation policy and budgets belonging to one message operation.

    Encoders and decoders compose this context; scalar contents and layout
    decisions stay with the reader or writer that owns them.
    """

    def __init__(
        self, header: Header, schema: dict, limits: Limits,
        path_schema: dict | None, inline_error_paths: str, error_encoding: str,
    ):
        validate_options(inline_error_paths, error_encoding)
        self.header, self.limits = header, limits
        self.root = _path_root(schema, path_schema)
        self.inline_error_paths = inline_error_paths
        self.error_encoding = error_encoding
        self.values = 0
        self.block_specs = {}

    @contextmanager
    def field_error_scope(self, wire):
        """Temporarily resolve in-band error paths against their nullable field."""
        root = self.root
        try:
            if self.inline_error_paths == "relative":
                self.root = wire
            yield
        finally:
            self.root = root

    def budget(self, depth):
        self.values += 1
        if depth > self.limits.max_depth:
            raise ArgoError("value exceeds depth limit")
        if self.values > self.limits.max_total_values:
            raise ArgoError("value exceeds total item limit")

    def count(self, length):
        if type(length) is not int or not 0 <= length <= self.limits.max_container_items:
            raise ArgoError("invalid container length or item limit exceeded")
        return length

    def block_spec(self, kind, key, dedupe, fixed):
        # A key cannot silently change scalar kind or deduplication policy.
        spec = (kind, dedupe, fixed)
        if key in self.block_specs and self.block_specs[key] != spec:
            raise ArgoError("conflicting scalar block definitions for " + key)
        self.block_specs[key] = spec
        if dedupe and kind not in ("STRING", "BYTES", "BOOLEAN", "DESC") and not (
            self.header.inline_everything or self.header.no_deduplication
        ):
            raise ArgoError("scalar type does not support deduplication")

    def path_segments(self, segments, *, to_wire):
        if not isinstance(segments, (list, tuple)):
            raise ArgoError("PATH must be a list of field names and indices")
        self.count(len(segments))
        if self.root is None:
            raise ArgoError("typed PATH requires a record root or path_schema")
        wire, out = self.root, []
        for segment in segments:
            wire = _unwrapped(wire)
            if wire["type"] == "RECORD":
                fields = wire["fields"]
                if to_wire:
                    if type(segment) is not str:
                        raise ArgoError("PATH record segment must be a field name")
                    index = next((i for i, f in enumerate(fields) if f["name"] == segment), None)
                    if index is None:
                        raise ArgoError("unknown PATH field")
                    out.append(index)
                else:
                    if type(segment) is not int or not 0 <= segment < len(fields):
                        raise ArgoError("PATH field ordinal out of bounds")
                    index = segment
                    out.append(fields[index]["name"])
                wire = fields[index]["of"]
            elif wire["type"] == "ARRAY":
                if type(segment) is not int or not 0 <= segment < (1 << 63):
                    raise ArgoError("PATH array index must be nonnegative int64")
                out.append(segment)
                wire = wire["of"]
            else:
                raise ArgoError("PATH descends through a scalar")
        return out

    def checked_path(self, value):
        if not isinstance(value, (list, tuple)):
            raise ArgoError("PATH must be a list")
        self.count(len(value))
        for item in value:
            if not ((type(item) is str and item != "") or (type(item) is int and 0 <= item < (1 << 63))):
                raise ArgoError("invalid PATH segment")
        return list(value)

    def checked_error(self, value, allow_extra=False):
        known = {"message", "locations", "path", "extensions"}
        if not isinstance(value, Mapping) or any(type(k) is not str for k in value) or (
            not allow_extra and not set(value) <= known
        ):
            raise ArgoError("unknown ERROR field or invalid ERROR object")
        if "message" not in value or type(value["message"]) is not str:
            raise ArgoError("ERROR.message is required and must be a string")
        out = {"message": value["message"]}
        if "locations" in value:
            locations = value["locations"]
            if locations is None and self.error_encoding == "nullable":
                locations = []
                out["locations"] = None
            if not isinstance(locations, (list, tuple)):
                raise ArgoError("ERROR.locations must be a list")
            self.count(len(locations))
            for item in locations:
                if not isinstance(item, Mapping) or set(item) != {"line", "column"}:
                    raise ArgoError("invalid ERROR location fields")
                for name in ("line", "column"):
                    if type(item[name]) is not int or not -(1 << 63) <= item[name] < (1 << 63):
                        raise ArgoError("ERROR location must contain int64 line and column")
            if value["locations"] is not None:
                out["locations"] = [{"line": item["line"], "column": item["column"]} for item in locations]
        if "path" in value:
            out["path"] = None if value["path"] is None and self.error_encoding == "nullable" else self.checked_path(value["path"])
        if "extensions" in value:
            if value["extensions"] is None and self.error_encoding == "nullable":
                out["extensions"] = None
            elif not isinstance(value["extensions"], Mapping):
                raise ArgoError("ERROR.extensions must be an object")
            else:
                out["extensions"] = dict(value["extensions"])
        if allow_extra:
            out.update((k, v) for k, v in value.items() if k not in known)
        return out
