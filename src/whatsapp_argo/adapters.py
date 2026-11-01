"""Optional doc-id selection, separate from the generic Argo codec."""
from __future__ import annotations

from collections.abc import Mapping

from whatsapp_argo.binary import ArgoError, Limits
from whatsapp_argo.codec import Codec
from whatsapp_argo._schema_io import load_json
from whatsapp_argo.schema import load_schema_store, validate_schema

__all__ = ["OperationRegistry"]


class OperationRegistry:
    """Bind an externally supplied operation store to document identifiers.

    ``documents`` is either ``{doc_id: operation_name}`` or the client asset
    shape ``{name: {"doc_id": ..., ...}}``. Unknown IDs never select a fallback.
    """

    def __init__(self, schemas, documents, *, limits=Limits()):
        if not isinstance(schemas, Mapping) or not isinstance(documents, Mapping):
            raise ArgoError("schemas and documents must be mappings")
        if len(schemas) > limits.max_container_items or len(documents) > limits.max_container_items:
            raise ArgoError("operation registry exceeds item limit")
        self.schemas = {name: validate_schema(schema, limits) for name, schema in schemas.items()}
        self.documents, self.limits = {}, limits
        for key, entry in documents.items():
            if isinstance(entry, Mapping):
                operation = entry.get("operation_name", key)
                if "doc_id" not in entry:
                    raise ArgoError("document entry is missing doc_id")
                identifier = entry["doc_id"]
            else:
                identifier, operation = key, entry
            if type(operation) is not str or operation not in self.schemas:
                raise ArgoError("document maps to unknown operation")
            identifier = self._identifier(identifier)
            if identifier in self.documents:
                raise ArgoError("duplicate document identifier")
            self.documents[identifier] = operation

    @staticmethod
    def _identifier(value):
        if type(value) not in (int, str) or (type(value) is int and value < 0):
            raise ArgoError("document identifier must be a nonnegative integer or text")
        value = str(value)
        if not value:
            raise ArgoError("document identifier cannot be empty")
        return value

    @classmethod
    def from_files(cls, schema_store, document_ids, *, limits=Limits()):
        return cls(load_schema_store(schema_store, limits),
                   load_json(document_ids, limits), limits=limits)

    def operation(self, doc_id):
        key = self._identifier(doc_id)
        if key not in self.documents:
            raise ArgoError("unknown document identifier")
        return self.documents[key]

    def schema_for(self, doc_id):
        return validate_schema(self.schemas[self.operation(doc_id)], self.limits)

    def codec_for(self, doc_id):
        return Codec(self.schema_for(doc_id), limits=self.limits)
