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


# Keep the original public identity for pickle interoperability in both
# directions. codec.py re-exports these exact classes.
for _model in (Document, FieldErrors, Layout, NonNull):
    _model.__module__ = "whatsapp_argo.codec"
del _model
