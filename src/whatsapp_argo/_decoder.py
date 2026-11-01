"""Argo value decoder: fresh state for each message."""
from __future__ import annotations

import struct

from whatsapp_argo.binary import ArgoError, MessageReader
from whatsapp_argo.models import FieldErrors, NonNull
from whatsapp_argo._value_context import DEFAULT_BLOCKS, SCALAR_MARKERS, SCALARS, ValueContext, is_labeled

__all__ = []


class Decoder:
    def __init__(self, message: MessageReader, context: ValueContext):
        self.msg, self.context = message, context
        self.header, self.limits = message.header, context.limits
        self.seen = {}
        self.events = []

    def event(self, *event):
        self.events.append(tuple(event))

    def length(self):
        return self.context.count(self.msg.core.label())

    def expect(self, expected):
        if self.msg.core.label() != expected:
            raise ArgoError("unexpected core label")

    def scalar(self, kind, key=None, dedupe=None, fixed=None, *, marker=False):
        if marker and self.header.self_describing and kind != "BOOLEAN":
            self.expect(SCALAR_MARKERS[kind])
        if kind == "BOOLEAN":
            self.context.block_spec(kind, "Boolean" if key is None else key, bool(dedupe), fixed)
            value = self.msg.core.label()
            if value not in (0, 1):
                raise ArgoError("invalid BOOLEAN label")
            return bool(value)
        direct_fixed = kind == "FIXED" and key is None
        if not direct_fixed:
            if key is None:
                key, dedupe = DEFAULT_BLOCKS[kind]
            self.context.block_spec(kind, key, bool(dedupe), fixed)
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
        # Keep recursive branches here: helper frames would reduce the usable
        # depth when callers raise Limits.max_depth beyond its default.
        self.context.budget(depth)
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
                with self.context.field_error_scope(wire["of"]):
                    errors = () if self.header.out_of_band_field_errors else tuple(
                        self.error(depth + 1) for _ in range(self.length())
                    )
                return FieldErrors(errors)
            if not self.header.self_describing and not is_labeled(wire["of"]):
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
                if field["omittable"] and not self.optional(is_labeled(field["of"])):
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
        if kind in SCALARS:
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
        self.context.budget(depth)
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
            return self.context.checked_path(self.desc(depth + 1))
        segments = []
        for _ in range(self.length()):
            self.context.budget(depth + 1)
            segments.append(self.scalar("VARINT"))
        return self.context.checked_path(self.context.path_segments(segments, to_wire=False))

    def error(self, depth):
        self.context.budget(depth)
        if self.header.self_describing or self.header.self_describing_errors:
            return self.context.checked_error(self.desc(depth + 1), allow_extra=True)
        out = {"message": self.scalar("STRING")}
        if self.context.error_encoding == "nullable":
            out.update(locations=None, path=None, extensions=None)
            if self.msg.core.peek_label() == -1:
                self.expect(-1)
            else:
                out["locations"] = []
                for _ in range(self.length()):
                    self.context.budget(depth + 1)
                    out["locations"].append({"line": self.scalar("VARINT"), "column": self.scalar("VARINT")})
            if self.msg.core.peek_label() == -1:
                self.expect(-1)
            else:
                out["path"] = self.path(depth + 1)
            out["extensions"] = self.desc(depth + 1)
            return self.context.checked_error(out)
        if self.optional(True):
            out["locations"] = []
            for _ in range(self.length()):
                self.context.budget(depth + 1)
                out["locations"].append({"line": self.scalar("VARINT"), "column": self.scalar("VARINT")})
        if self.optional(True):
            out["path"] = self.path(depth + 1)
        if self.optional(False):
            extension = self.desc(depth + 1)
            if not isinstance(extension, dict):
                raise ArgoError("ERROR.extensions must be object")
            out["extensions"] = extension
        return self.context.checked_error(out)
