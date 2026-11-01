"""Public Argo value read/write API and reusable codec configuration.

Protocol behavior is informed by the MIT-licensed erlang-argo implementation;
see THIRD_PARTY_NOTICES.md. No original message is retained as write output.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any  # Resolve model annotations under their stable public module.

from whatsapp_argo.binary import ArgoError, Header, Limits, MessageReader, MessageWriter
from whatsapp_argo.models import Document, FieldErrors, Layout, NonNull
from whatsapp_argo._decoder import Decoder
from whatsapp_argo._encoder import Encoder
from whatsapp_argo._schema_validation import validate_schema
from whatsapp_argo._value_context import ValueContext, validate_options

# Keep the original submodule imports available alongside the package API.
__all__ = ["Codec", "Document", "FieldErrors", "Layout", "NonNull", "read", "write"]


def _digest(schema):
    return hashlib.sha256(json.dumps(schema, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


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
    context = ValueContext(msg.header, schema, limits, path_schema, inline_error_paths, error_encoding)
    decoder = Decoder(msg, context)
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
    message = MessageWriter(header, limits)
    context = ValueContext(header, schema, limits, path_schema, inline_error_paths, error_encoding)
    encoder = Encoder(message, context, layout)
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
        validate_options(inline_error_paths, error_encoding)
        self.inline_error_paths = inline_error_paths
        self.error_encoding = error_encoding

    def read(self, data, *, schema=None, preserve_layout=False):
        return read(data, self.schema if schema is None else schema, limits=self.limits,
                    path_schema=self.path_schema, preserve_layout=preserve_layout, inline_error_paths=self.inline_error_paths,
                    error_encoding=self.error_encoding)

    def write(self, value, *, schema=None, header=None, preserve_layout=False):
        return write(value, self.schema if schema is None else schema, header=header,
                     limits=self.limits, path_schema=self.path_schema, preserve_layout=preserve_layout,
                     inline_error_paths=self.inline_error_paths, error_encoding=self.error_encoding)
