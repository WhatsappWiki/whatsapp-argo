"""Argo 1.2 read/write, with external schemas and optional operation selection."""
from whatsapp_argo.binary import ArgoError, Header, Limits
from whatsapp_argo.codec import Codec, read, write
from whatsapp_argo.models import Document, FieldErrors, Layout, NonNull
from whatsapp_argo.schema import (
    load_schema, load_schema_store, read_schema_store, validate_schema,
    write_schema_store,
)
from whatsapp_argo.adapters import OperationRegistry

__version__ = "0.1.0"
__all__ = [
    "ArgoError", "Header", "Limits", "Codec", "Document", "FieldErrors", "Layout",
    "NonNull", "OperationRegistry", "read", "write", "load_schema",
    "load_schema_store", "read_schema_store", "write_schema_store", "validate_schema",
]
