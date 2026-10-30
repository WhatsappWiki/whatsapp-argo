"""Argo 1.2 values, rebuilt from schema and values on every write.

Protocol behavior is informed by the MIT-licensed erlang-argo implementation;
see THIRD_PARTY_NOTICES.md. No original message is retained as write output.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import struct
from typing import Any

from .binary import ArgoError, Header, Limits, MessageReader, MessageWriter, label_bytes
from .schema import validate_schema


@dataclass(frozen=True)
class FieldErrors:
    """The nullable ERROR marker, distinct from null. Errors may be in-band."""

    errors: tuple[dict[str, Any], ...] = ()

    def __post_init__(self):
        if not isinstance(self.errors, (tuple, list)):
            raise ArgoError("FieldErrors requires a list or tuple of ERROR objects")
        object.__setattr__(self, "errors", tuple(self.errors))

    def full_paths(self, prefix):
        """Return errors with their relative paths prefixed by the field path."""
        return tuple({**error, **({"path": list(prefix) + error["path"]} if error.get("path") is not None else {})}
                     for error in self.errors)


@dataclass(frozen=True)
class NonNull:
    """Explicit non-null wrapper when the inner DESC value itself is null."""

    value: Any


@dataclass(frozen=True)
class Layout:
    """Encoding decisions only; never cached scalar contents or a message."""

    schema_digest: str
    path_digest: str
    header: Header
    events: tuple[tuple, ...]


@dataclass
class Document:
    value: Any
    header: Header
    schema: dict
    layout: Layout | None = None
    path_schema: dict | None = None
    inline_error_paths: str = "relative"
    error_encoding: str = "omittable"


_DEFAULTS = {
    "STRING": ("String", True), "BYTES": ("Bytes", True),
    "VARINT": ("Int", False), "FLOAT64": ("Float", False),
    "BOOLEAN": ("Boolean", False),
}
_MARKERS = {"STRING": 4, "BYTES": 5, "FIXED": 5, "VARINT": 6, "FLOAT64": 7}
_SCALARS = {*_DEFAULTS, "FIXED"}


def _digest(schema):
    return hashlib.sha256(json.dumps(schema, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _labeled(wire):
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


class _Base:
    def __init__(self, message, schema, limits, path_schema, inline_error_paths, error_encoding):
        if inline_error_paths not in ("relative", "root"):
            raise ArgoError("inline_error_paths must be 'relative' or 'root'")
        self.msg, self.schema, self.limits = message, schema, limits
        self.header, self.root = message.header, _path_root(schema, path_schema)
        self.inline_error_paths = inline_error_paths
        if error_encoding not in ("omittable", "nullable"):
            raise ArgoError("error_encoding must be 'omittable' or 'nullable'")
        self.error_encoding = error_encoding
        self.values = 0
        self.seen = {}
        self.seen_index = {}
        self.block_specs = {}

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


class _Decoder(_Base):
    def __init__(self, message, schema, limits, path_schema, inline_error_paths, error_encoding):
        super().__init__(message, schema, limits, path_schema, inline_error_paths, error_encoding)
        self.events = []

    def event(self, *event):
        self.events.append(tuple(event))

    def length(self):
        return self.count(self.msg.core.label())

    def expect(self, expected):
        if self.msg.core.label() != expected:
            raise ArgoError("unexpected core label")

    def scalar(self, kind, key=None, dedupe=None, fixed=None, *, marker=False):
        if marker and self.header.self_describing and kind != "BOOLEAN":
            self.expect(_MARKERS[kind])
        if kind == "BOOLEAN":
            self.block_spec(kind, "Boolean" if key is None else key, bool(dedupe), fixed)
            value = self.msg.core.label()
            if value not in (0, 1):
                raise ArgoError("invalid BOOLEAN label")
            return bool(value)
        direct_fixed = kind == "FIXED" and key is None
        if not direct_fixed:
            if key is None:
                key, dedupe = _DEFAULTS[kind]
            self.block_spec(kind, key, bool(dedupe), fixed)
        inline = self.header.inline_everything or direct_fixed
        active_dedupe = bool(dedupe) and not (inline or self.header.no_deduplication)
        block = self.msg.core if inline else self.msg.block(key)
        if kind in ("STRING", "BYTES"):
            length = self.msg.core.label()
            if length <= -4:
                index = -4 - length
                seen = self.seen.setdefault(key, [])
                if not active_dedupe or index >= len(seen):
                    raise ArgoError("invalid or disabled scalar backreference")
                self.event("scalar", kind, key, index)
                return seen[index]
            if not 0 <= length <= self.limits.max_string_bytes:
                raise ArgoError("invalid scalar length or size limit exceeded")
            data = block.take(length)
            if kind == "STRING":
                if self.header.null_terminated_strings and block.take(1) != b"\x00":
                    raise ArgoError("STRING missing null terminator")
                try:
                    value = data.decode("utf-8", "strict")
                except UnicodeError as exc:
                    raise ArgoError("invalid UTF-8 STRING") from exc
            else:
                value = data
            if active_dedupe:
                self.seen.setdefault(key, []).append(value)
            self.event("scalar", kind, key, None)
            return value
        if kind == "VARINT":
            return block.label()
        if kind == "FLOAT64":
            data = block.take(8)
            return struct.unpack("<d", data)[0]
        if kind == "FIXED":
            if fixed > self.limits.max_string_bytes:
                raise ArgoError("FIXED exceeds size limit")
            return block.take(fixed)
        raise ArgoError("unsupported scalar")

    def optional(self, labeled):
        marker = self.msg.core.peek_label()
        if marker == -2:
            self.expect(-2)
            return False
        if not labeled:
            self.expect(0)
        return True

    def value(self, wire, depth=0):
        self.budget(depth)
        kind = wire["type"]
        if kind == "NULLABLE":
            marker = self.msg.core.peek_label()
            if marker == -1:
                self.expect(-1)
                return None
            if marker == -3:
                self.expect(-3)
                if self.header.self_describing:
                    raise ArgoError("field error marker is invalid in self-describing mode")
                root = self.root
                try:
                    if self.inline_error_paths == "relative":
                        self.root = wire["of"]
                    errors = () if self.header.out_of_band_field_errors else tuple(
                        self.error(depth + 1) for _ in range(self.length())
                    )
                finally:
                    self.root = root
                return FieldErrors(errors)
            if not self.header.self_describing and not _labeled(wire["of"]):
                self.expect(0)
            result = self.value(wire["of"], depth + 1)
            return NonNull(result) if result is None or isinstance(result, (FieldErrors, NonNull)) else result
        if kind == "RECORD":
            fields = wire["fields"]
            if self.header.self_describing:
                self.expect(2)
                out, definitions = {}, {f["name"]: f for f in fields}
                n = self.length()
                # Order event is before child events, so encoders can follow it.
                event_index = len(self.events)
                self.events.append(())
                for _ in range(n):
                    name = self.scalar("STRING")
                    if name in out or name not in definitions:
                        raise ArgoError("duplicate or unknown RECORD field")
                    out[name] = self.value(definitions[name]["of"], depth + 1)
                if any(not f["omittable"] and f["name"] not in out for f in fields):
                    raise ArgoError("missing required RECORD field")
                self.events[event_index] = ("record", tuple(out))
                return out
            out = {}
            for field in fields:
                if field["omittable"] and not self.optional(_labeled(field["of"])):
                    continue
                out[field["name"]] = self.value(field["of"], depth + 1)
            return out
        if kind == "ARRAY":
            if self.header.self_describing:
                self.expect(3)
            return [self.value(wire["of"], depth + 1) for _ in range(self.length())]
        if kind == "BLOCK":
            scalar = wire["of"]
            if scalar["type"] == "DESC":
                return self.desc(depth + 1)
            return self.scalar(scalar["type"], wire["key"], wire["dedupe"], scalar.get("length"), marker=True)
        if kind in _SCALARS:
            return self.scalar(kind, fixed=wire.get("length"), marker=True)
        if kind == "DESC":
            return self.desc(depth + 1)
        if kind == "EXTENSIONS":
            result = self.desc(depth + 1)
            if not isinstance(result, dict):
                raise ArgoError("EXTENSIONS must be an object")
            return result
        if kind == "ERROR":
            return self.error(depth + 1)
        if kind == "PATH":
            return self.path(depth + 1)
        raise ArgoError("unsupported wire type")

    def desc(self, depth):
        self.budget(depth)
        marker = self.msg.core.label()
        if marker == -1:
            return None
        if marker in (0, 1):
            return bool(marker)
        if marker == 2:
            n, out = self.length(), {}
            index = len(self.events)
            self.events.append(())
            for _ in range(n):
                key = self.scalar("STRING")
                if key in out:
                    raise ArgoError("duplicate DESC object key")
                out[key] = self.desc(depth + 1)
            self.events[index] = ("object", tuple(out))
            return out
        if marker == 3:
            return [self.desc(depth + 1) for _ in range(self.length())]
        kinds = {4: "STRING", 5: "BYTES", 6: "VARINT", 7: "FLOAT64"}
        if marker in kinds:
            return self.scalar(kinds[marker])
        raise ArgoError("invalid DESC type label")

    def path(self, depth, self_describing=False):
        if self.header.self_describing or self_describing:
            return self.checked_path(self.desc(depth + 1))
        segments = []
        for _ in range(self.length()):
            self.budget(depth + 1)
            segments.append(self.scalar("VARINT"))
        return self.checked_path(self.path_segments(segments, to_wire=False))

    def error(self, depth):
        self.budget(depth)
        if self.header.self_describing or self.header.self_describing_errors:
            return self.checked_error(self.desc(depth + 1), allow_extra=True)
        out = {"message": self.scalar("STRING")}
        if self.error_encoding == "nullable":
            out.update(locations=None, path=None, extensions=None)
            if self.msg.core.peek_label() == -1:
                self.expect(-1)
            else:
                out["locations"] = []
                for _ in range(self.length()):
                    self.budget(depth + 1)
                    out["locations"].append({"line": self.scalar("VARINT"), "column": self.scalar("VARINT")})
            if self.msg.core.peek_label() == -1:
                self.expect(-1)
            else:
                out["path"] = self.path(depth + 1)
            out["extensions"] = self.desc(depth + 1)
            return self.checked_error(out)
        if self.optional(True):
            out["locations"] = []
            for _ in range(self.length()):
                self.budget(depth + 1)
                out["locations"].append({"line": self.scalar("VARINT"), "column": self.scalar("VARINT")})
        if self.optional(True):
            out["path"] = self.path(depth + 1)
        if self.optional(False):
            extension = self.desc(depth + 1)
            if not isinstance(extension, dict):
                raise ArgoError("ERROR.extensions must be object")
            out["extensions"] = extension
        return self.checked_error(out)


class _Encoder(_Base):
    def __init__(self, message, schema, limits, path_schema, inline_error_paths, error_encoding, layout):
        super().__init__(message, schema, limits, path_schema, inline_error_paths, error_encoding)
        self.layout = layout
        self.event_index = 0

    def event(self, kind, *prefix):
        if self.layout is None:
            return None
        if self.event_index >= len(self.layout.events):
            raise ArgoError("layout has too few encoding decisions")
        event = self.layout.events[self.event_index]
        self.event_index += 1
        if event[:len(prefix) + 1] != (kind, *prefix):
            raise ArgoError("layout no longer matches value structure")
        return event[-1]

    def label(self, value):
        self.msg.core.extend(label_bytes(value))

    def length(self, value):
        self.label(self.count(value))

    def scalar(self, kind, value, key=None, dedupe=None, fixed=None, *, marker=False):
        if marker and self.header.self_describing and kind != "BOOLEAN":
            self.label(_MARKERS[kind])
        if kind == "BOOLEAN":
            self.block_spec(kind, "Boolean" if key is None else key, bool(dedupe), fixed)
            if type(value) is not bool:
                raise ArgoError("BOOLEAN requires bool")
            self.label(int(value))
            return
        direct_fixed = kind == "FIXED" and key is None
        if not direct_fixed:
            if key is None:
                key, dedupe = _DEFAULTS[kind]
            self.block_spec(kind, key, bool(dedupe), fixed)
        inline = self.header.inline_everything or direct_fixed
        active_dedupe = bool(dedupe) and not (inline or self.header.no_deduplication)
        block = self.msg.core if inline else self.msg.block(key)
        if kind in ("STRING", "BYTES"):
            if kind == "STRING":
                if type(value) is not str:
                    raise ArgoError("STRING requires str")
                try:
                    raw = value.encode("utf-8", "strict")
                except UnicodeError as exc:
                    raise ArgoError("invalid UTF-8 STRING") from exc
            else:
                if not isinstance(value, (bytes, bytearray, memoryview)):
                    raise ArgoError("BYTES requires bytes-like value")
                if (value.nbytes if isinstance(value, memoryview) else len(value)) > self.limits.max_string_bytes:
                    raise ArgoError("scalar exceeds size limit")
                raw = bytes(value)
                value = raw
            if len(raw) > self.limits.max_string_bytes:
                raise ArgoError("scalar exceeds size limit")
            seen = self.seen.setdefault(key, [])
            reference = self.event("scalar", kind, key)
            if self.layout is None and active_dedupe:
                reference = self.seen_index.setdefault(key, {}).get(value)
            if reference is not None:
                if not active_dedupe or not 0 <= reference < len(seen) or seen[reference] != value:
                    raise ArgoError("changed value is incompatible with preserved backreference")
                self.label(-4 - reference)
                return
            self.label(len(raw))
            block.extend(raw)
            if kind == "STRING" and self.header.null_terminated_strings:
                block.extend(b"\x00")
            if active_dedupe:
                self.seen_index.setdefault(key, {}).setdefault(value, len(seen))
                seen.append(value)
            return
        if kind == "VARINT":
            block.extend(label_bytes(value))
            return
        if kind == "FLOAT64":
            if type(value) is not float:
                raise ArgoError("FLOAT64 requires float")
            data = struct.pack("<d", value)
            block.extend(data)
            return
        if kind == "FIXED":
            if not isinstance(value, (bytes, bytearray, memoryview)):
                raise ArgoError("FIXED requires exact-length bytes")
            if (value.nbytes if isinstance(value, memoryview) else len(value)) != fixed:
                raise ArgoError("FIXED requires exact-length bytes")
            raw = bytes(value)
            if len(raw) != fixed:
                raise ArgoError("FIXED requires exact-length bytes")
            if fixed > self.limits.max_string_bytes:
                raise ArgoError("FIXED exceeds size limit")
            block.extend(raw)
            return
        raise ArgoError("unsupported scalar")

    def optional(self, present, labeled):
        if not present:
            self.label(-2)
        elif not labeled:
            self.label(0)

    def order(self, kind, value, default):
        order = self.event(kind)
        if order is None:
            return default
        if len(order) != len(value) or set(order) != set(value):
            raise ArgoError("record keys changed; preserved layout is incompatible")
        return order

    def value(self, wire, value, depth=0):
        self.budget(depth)
        kind = wire["type"]
        if kind == "NULLABLE":
            if value is None:
                self.label(-1)
                return
            if isinstance(value, FieldErrors):
                if self.header.self_describing:
                    raise ArgoError("self-describing mode cannot preserve nullable FieldErrors; supply null explicitly")
                if self.header.out_of_band_field_errors and value.errors:
                    raise ArgoError("out-of-band FieldErrors must be empty; put errors in response.errors")
                self.label(-3)
                if not self.header.out_of_band_field_errors:
                    self.length(len(value.errors))
                    root = self.root
                    try:
                        if self.inline_error_paths == "relative":
                            self.root = wire["of"]
                        for error in value.errors:
                            self.error(error, depth + 1)
                    finally:
                        self.root = root
                return
            if isinstance(value, NonNull):
                value = value.value
                if (value is None or isinstance(value, (FieldErrors, NonNull))) and (
                    self.header.self_describing or _labeled(wire["of"])
                ):
                    raise ArgoError("this nullable schema/header cannot distinguish non-null inner null")
            if not self.header.self_describing and not _labeled(wire["of"]):
                self.label(0)
            self.value(wire["of"], value, depth + 1)
            return
        if kind == "RECORD":
            if not isinstance(value, Mapping):
                raise ArgoError("RECORD requires a mapping")
            fields = {f["name"]: f for f in wire["fields"]}
            if not set(value) <= set(fields):
                raise ArgoError("unknown RECORD field")
            if any(not f["omittable"] and name not in value for name, f in fields.items()):
                raise ArgoError("missing required RECORD field")
            self.count(len(fields))
            if self.header.self_describing:
                self.label(2)
                self.length(len(value))
                for name in self.order("record", value, [name for name in fields if name in value]):
                    self.scalar("STRING", name)
                    self.value(fields[name]["of"], value[name], depth + 1)
            else:
                for name, field in fields.items():
                    if field["omittable"]:
                        self.optional(name in value, _labeled(field["of"]))
                    if name in value:
                        self.value(field["of"], value[name], depth + 1)
            return
        if kind == "ARRAY":
            if not isinstance(value, (list, tuple)):
                raise ArgoError("ARRAY requires list or tuple")
            if self.header.self_describing:
                self.label(3)
            self.length(len(value))
            for item in value:
                self.value(wire["of"], item, depth + 1)
            return
        if kind == "BLOCK":
            scalar = wire["of"]
            if scalar["type"] == "DESC":
                self.desc(value, depth + 1)
            else:
                self.scalar(scalar["type"], value, wire["key"], wire["dedupe"], scalar.get("length"), marker=True)
            return
        if kind in _SCALARS:
            self.scalar(kind, value, fixed=wire.get("length"), marker=True)
            return
        if kind == "DESC":
            self.desc(value, depth + 1)
            return
        if kind == "EXTENSIONS":
            if not isinstance(value, Mapping):
                raise ArgoError("EXTENSIONS requires an object")
            self.desc(value, depth + 1)
            return
        if kind == "ERROR":
            self.error(value, depth + 1)
            return
        if kind == "PATH":
            self.path(value, depth + 1)
            return
        raise ArgoError("unsupported wire type")

    def desc(self, value, depth):
        self.budget(depth)
        if value is None:
            self.label(-1)
        elif type(value) is bool:
            self.label(int(value))
        elif type(value) is int:
            self.label(6)
            self.scalar("VARINT", value)
        elif type(value) is float:
            self.label(7)
            self.scalar("FLOAT64", value)
        elif type(value) is str:
            self.label(4)
            self.scalar("STRING", value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            self.label(5)
            self.scalar("BYTES", value)
        elif isinstance(value, Mapping):
            if any(type(key) is not str for key in value):
                raise ArgoError("DESC object keys must be strings")
            self.label(2)
            self.length(len(value))
            for key in self.order("object", value, list(value)):
                self.scalar("STRING", key)
                self.desc(value[key], depth + 1)
        elif isinstance(value, (list, tuple)):
            self.label(3)
            self.length(len(value))
            for item in value:
                self.desc(item, depth + 1)
        else:
            raise ArgoError("unsupported DESC Python type")

    def path(self, value, depth, self_describing=False):
        value = self.checked_path(value)
        if self.header.self_describing or self_describing:
            self.desc(value, depth + 1)
        else:
            segments = self.path_segments(value, to_wire=True)
            self.length(len(segments))
            for segment in segments:
                self.budget(depth + 1)
                self.scalar("VARINT", segment)

    def error(self, value, depth):
        self.budget(depth)
        value = self.checked_error(value, allow_extra=self.header.self_describing or self.header.self_describing_errors)
        if self.header.self_describing or self.header.self_describing_errors:
            self.desc(value, depth + 1)
            return
        if self.error_encoding == "nullable":
            if not {"locations", "path", "extensions"} <= set(value):
                raise ArgoError("nullable ERROR encoding requires explicit locations/path/extensions; use None for null")
            self.scalar("STRING", value["message"])
            if value["locations"] is None:
                self.label(-1)
            else:
                self.length(len(value["locations"]))
                for item in value["locations"]:
                    self.budget(depth + 1)
                    self.scalar("VARINT", item["line"])
                    self.scalar("VARINT", item["column"])
            if value["path"] is None:
                self.label(-1)
            else:
                self.path(value["path"], depth + 1)
            self.desc(value["extensions"], depth + 1)
            return
        self.scalar("STRING", value["message"])
        self.optional("locations" in value, True)
        if "locations" in value:
            self.length(len(value["locations"]))
            for item in value["locations"]:
                self.budget(depth + 1)
                self.scalar("VARINT", item["line"])
                self.scalar("VARINT", item["column"])
        self.optional("path" in value, True)
        if "path" in value:
            self.path(value["path"], depth + 1)
        self.optional("extensions" in value, False)
        if "extensions" in value:
            self.desc(value["extensions"], depth + 1)


def read(data, schema=None, *, limits=Limits(), preserve_layout=False, path_schema=None,
         inline_error_paths="relative", error_encoding="omittable"):
    """Read one complete message. Non-self-describing values need a schema.

    Without a schema, self-describing input is interpreted as DESC with standard
    scalar block keys. Custom blocks and FIXED still need their external schema.
    """
    msg = MessageReader(data, limits)
    if schema is None:
        if not msg.header.self_describing:
            raise ArgoError("schema required for non-self-describing input")
        schema = {"type": "DESC"}
    schema = validate_schema(schema, limits)
    path_schema = None if path_schema is None else validate_schema(path_schema, limits)
    decoder = _Decoder(msg, schema, limits, path_schema, inline_error_paths, error_encoding)
    value = decoder.value(schema)
    msg.finish()
    layout = Layout(_digest(schema), _digest((path_schema, inline_error_paths, error_encoding)), msg.header,
                    tuple(decoder.events)) if preserve_layout else None
    return Document(value, msg.header, schema, layout, path_schema, inline_error_paths, error_encoding)


def write(value, schema=None, *, header=None, limits=Limits(), preserve_layout=False, path_schema=None,
          inline_error_paths=None, error_encoding=None):
    """Freshly encode a value or Document; optionally reuse encoding decisions."""
    document = value if isinstance(value, Document) else None
    if document is not None:
        value = document.value
        schema = document.schema if schema is None else schema
        header = document.header if header is None else header
        path_schema = document.path_schema if path_schema is None else path_schema
        inline_error_paths = document.inline_error_paths if inline_error_paths is None else inline_error_paths
        error_encoding = document.error_encoding if error_encoding is None else error_encoding
    inline_error_paths = "relative" if inline_error_paths is None else inline_error_paths
    error_encoding = "omittable" if error_encoding is None else error_encoding
    header = Header() if header is None else header
    if not isinstance(header, Header):
        raise ArgoError("header must be Header")
    if schema is None:
        if not header.self_describing:
            raise ArgoError("schema required for non-self-describing output")
        schema = {"type": "DESC"}
    schema = validate_schema(schema, limits)
    path_schema = None if path_schema is None else validate_schema(path_schema, limits)
    layout = None
    if preserve_layout:
        if document is None or document.layout is None:
            raise ArgoError("preserve_layout requires read(..., preserve_layout=True) Document")
        layout = document.layout
        if (layout.schema_digest != _digest(schema) or layout.path_digest != _digest((path_schema, inline_error_paths, error_encoding))
                or layout.header != header):
            raise ArgoError("schema/header changed; layout cannot be reused")
    encoder = _Encoder(MessageWriter(header, limits), schema, limits, path_schema, inline_error_paths, error_encoding, layout)
    encoder.value(schema, value)
    if layout is not None and encoder.event_index != len(layout.events):
        raise ArgoError("layout has unused encoding decisions")
    return encoder.msg.finish()


class Codec:
    """Reusable configuration; each call has independent message state."""

    def __init__(self, schema=None, *, limits=Limits(), path_schema=None, inline_error_paths="relative", error_encoding="omittable"):
        self.schema = None if schema is None else validate_schema(schema, limits)
        self.limits = limits
        self.path_schema = None if path_schema is None else validate_schema(path_schema, limits)
        if inline_error_paths not in ("relative", "root"):
            raise ArgoError("inline_error_paths must be 'relative' or 'root'")
        self.inline_error_paths = inline_error_paths
        if error_encoding not in ("omittable", "nullable"):
            raise ArgoError("error_encoding must be 'omittable' or 'nullable'")
        self.error_encoding = error_encoding

    def read(self, data, *, schema=None, preserve_layout=False):
        return read(data, self.schema if schema is None else schema, limits=self.limits,
                    path_schema=self.path_schema, preserve_layout=preserve_layout, inline_error_paths=self.inline_error_paths,
                    error_encoding=self.error_encoding)

    def write(self, value, *, schema=None, header=None, preserve_layout=False):
        return write(value, self.schema if schema is None else schema, header=header,
                     limits=self.limits, path_schema=self.path_schema, preserve_layout=preserve_layout,
                     inline_error_paths=self.inline_error_paths, error_encoding=self.error_encoding)
