"""Argo 1.2 read/write, with external schemas and optional operation selection."""
from .binary import ArgoError, Header, Limits
from .codec import Codec, Document, FieldErrors, Layout, NonNull, read, write
from .schema import (
    load_schema, load_schema_store, read_schema_store, validate_schema,
    write_schema_store,
)
from .adapters import OperationRegistry

__version__ = "0.1.0"
__all__ = [
    "ArgoError", "Header", "Limits", "Codec", "Document", "FieldErrors", "Layout",
    "NonNull", "OperationRegistry", "read", "write", "load_schema",
    "load_schema_store", "read_schema_store", "write_schema_store", "validate_schema",
]
