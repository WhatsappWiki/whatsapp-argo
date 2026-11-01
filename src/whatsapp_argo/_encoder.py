"""Argo value encoder: fresh state for each message."""
from __future__ import annotations

from collections.abc import Mapping
import struct

from whatsapp_argo.binary import ArgoError, MessageWriter, label_bytes
from whatsapp_argo.models import FieldErrors, Layout, NonNull
from whatsapp_argo._value_context import DEFAULT_BLOCKS, SCALAR_MARKERS, SCALARS, ValueContext, is_labeled

__all__ = []


class Encoder:
    def __init__(self, message: MessageWriter, context: ValueContext, layout: Layout | None = None):
        self.msg, self.context = message, context
        self.header, self.limits = message.header, context.limits
        self.seen = {}
        self.seen_index = {}
        self.layout = layout
        self.event_index = 0

    def event(self, kind, *prefix):
        if self.layout is None:
            return None
        if self.event_index >= len(self.layout.events):
            raise ArgoError("layout has too few encoding decisions")
        event = self.layout.events[self.event_index]
        self.event_index += 1
        if not isinstance(event, tuple) or len(event) != len(prefix) + 2:
            raise ArgoError("invalid layout encoding decision")
        if event[:len(prefix) + 1] != (kind, *prefix):
            raise ArgoError("layout no longer matches value structure")
        return event[-1]

    def label(self, value):
        self.msg.core.extend(label_bytes(value))

    def length(self, value):
        self.label(self.context.count(value))

    def scalar(self, kind, value, key=None, dedupe=None, fixed=None, *, marker=False):
        if marker and self.header.self_describing and kind != "BOOLEAN":
            self.label(SCALAR_MARKERS[kind])
        if kind == "BOOLEAN":
            self.context.block_spec(kind, "Boolean" if key is None else key, bool(dedupe), fixed)
            if type(value) is not bool:
                raise ArgoError("BOOLEAN requires bool")
            self.label(int(value))
            return
        direct_fixed = kind == "FIXED" and key is None
        if not direct_fixed:
            if key is None:
                key, dedupe = DEFAULT_BLOCKS[kind]
            self.context.block_spec(kind, key, bool(dedupe), fixed)
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
                if type(reference) is not int:
                    raise ArgoError("layout backreference must be an integer")
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
        if self.layout is None:
            return default
        if not isinstance(order, (tuple, list)) or any(type(name) is not str for name in order):
            raise ArgoError("invalid layout field order")
        if len(order) != len(value) or set(order) != set(value):
            raise ArgoError("record keys changed; preserved layout is incompatible")
        return order

    def value(self, wire, value, depth=0):
        # Keep recursive branches here: helper frames would reduce the usable
        # depth when callers raise Limits.max_depth beyond its default.
        self.context.budget(depth)
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
                    with self.context.field_error_scope(wire["of"]):
                        for error in value.errors:
                            self.error(error, depth + 1)
                return
            if isinstance(value, NonNull):
                value = value.value
                if (value is None or isinstance(value, (FieldErrors, NonNull))) and (
                    self.header.self_describing or is_labeled(wire["of"])
                ):
                    raise ArgoError("this nullable schema/header cannot distinguish non-null inner null")
            if not self.header.self_describing and not is_labeled(wire["of"]):
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
            self.context.count(len(fields))
            if self.header.self_describing:
                self.label(2)
                self.length(len(value))
                for name in self.order("record", value, [name for name in fields if name in value]):
                    self.scalar("STRING", name)
                    self.value(fields[name]["of"], value[name], depth + 1)
            else:
                for name, field in fields.items():
                    if field["omittable"]:
                        self.optional(name in value, is_labeled(field["of"]))
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
        if kind in SCALARS:
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
        self.context.budget(depth)
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
        value = self.context.checked_path(value)
        if self.header.self_describing or self_describing:
            self.desc(value, depth + 1)
        else:
            segments = self.context.path_segments(value, to_wire=True)
            self.length(len(segments))
            for segment in segments:
                self.context.budget(depth + 1)
                self.scalar("VARINT", segment)

    def error(self, value, depth):
        self.context.budget(depth)
        value = self.context.checked_error(value, allow_extra=self.header.self_describing or self.header.self_describing_errors)
        if self.header.self_describing or self.header.self_describing_errors:
            self.desc(value, depth + 1)
            return
        if self.context.error_encoding == "nullable":
            if not {"locations", "path", "extensions"} <= set(value):
                raise ArgoError("nullable ERROR encoding requires explicit locations/path/extensions; use None for null")
            self.scalar("STRING", value["message"])
            if value["locations"] is None:
                self.label(-1)
            else:
                self.length(len(value["locations"]))
                for item in value["locations"]:
                    self.context.budget(depth + 1)
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
                self.context.budget(depth + 1)
                self.scalar("VARINT", item["line"])
                self.scalar("VARINT", item["column"])
        self.optional("path" in value, True)
        if "path" in value:
            self.path(value["path"], depth + 1)
        self.optional("extensions" in value, False)
        if "extensions" in value:
            self.desc(value["extensions"], depth + 1)
