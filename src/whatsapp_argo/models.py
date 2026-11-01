"""Public value containers and encoding-layout metadata, without codec state."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from whatsapp_argo.binary import ArgoError, Header

__all__ = ["Document", "FieldErrors", "Layout", "NonNull"]


@dataclass(frozen=True)
class FieldErrors:
    """The nullable ERROR marker, distinct from null. Errors may be in-band."""

    errors: tuple[dict[str, Any], ...] = ()

    def __post_init__(self):
        if not isinstance(self.errors, (tuple, list)):
            raise ArgoError("FieldErrors requires a list or tuple of ERROR objects")
        object.__setattr__(self, "errors", tuple(self.errors))

    def full_paths(self, prefix):
        """Return copied errors with the field prefix added to relative paths.

        The prefix may be an iterable; paths may be lists or tuples. Missing
        paths and explicit null paths are preserved.
        """
        prefix_parts = None
        result = []
        for error in self.errors:
            copied = {**error}
            if error.get("path") is not None:
                if prefix_parts is None:
                    prefix_parts = list(prefix)
                copied["path"] = prefix_parts + list(error["path"])
            result.append(copied)
        return tuple(result)


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


# Keep the original public identity for pickle interoperability in both
# directions. codec.py re-exports these exact classes.
for _model in (Document, FieldErrors, Layout, NonNull):
    _model.__module__ = "whatsapp_argo.codec"
del _model
