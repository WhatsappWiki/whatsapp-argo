"""Public schema validation, external loading and wire-type store API.

Implementation modules separate pure validation from filesystem access and
binary serialization. No WhatsApp operation schemas are bundled.
"""
from whatsapp_argo._schema_io import load_schema, load_schema_store
from whatsapp_argo._schema_store import read_schema_store, write_schema_store
from whatsapp_argo._schema_validation import (
    MARKERS, SCALARS, SCALAR_MARKERS, SIMPLE_MARKERS,
    validate_schema, validate_schema_store,
)

__all__ = [
    "load_schema", "load_schema_store", "read_schema_store", "write_schema_store",
    "validate_schema", "validate_schema_store",
    "MARKERS", "SCALARS", "SCALAR_MARKERS", "SIMPLE_MARKERS",
]

# Public functions keep their original qualified names when pickled, even
# though their implementations now live in separate internal modules.
for _function in (
    load_schema, load_schema_store, read_schema_store, write_schema_store,
    validate_schema, validate_schema_store,
):
    _function.__module__ = __name__
del _function
