"""Bounded loading of external schema and document-ID configuration files."""
from __future__ import annotations

import json
from os import PathLike
from pathlib import Path

from whatsapp_argo.binary import ArgoError, DEFAULT_LIMITS
from whatsapp_argo._schema_store import read_schema_store
from whatsapp_argo._schema_validation import MARKERS, validate_schema, validate_schema_store

__all__ = []


def _json_unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ArgoError("duplicate JSON key")
        out[key] = value
    return out


def _read_file(path, limits):
    path = Path(path)
    if path.stat().st_size > limits.max_message_bytes:
        raise ArgoError("schema configuration exceeds limit")
    with path.open("rb") as stream:
        raw = stream.read(limits.max_message_bytes + 1)
    if len(raw) > limits.max_message_bytes:
        raise ArgoError("schema configuration exceeds limit")
    return raw


def load_json(path, limits):
    raw = _read_file(path, limits)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_json_unique)
    except ArgoError:
        # Preserve the specific duplicate-key error from object_pairs_hook.
        raise
    except (ValueError, RecursionError) as exc:
        raise ArgoError("invalid schema JSON") from exc


def load_schema(source, limits=DEFAULT_LIMITS):
    """Load one schema from a mapping, scalar shorthand, or JSON file path."""
    if isinstance(source, (str, PathLike)) and not (isinstance(source, str) and source in MARKERS):
        source = load_json(source, limits)
    return validate_schema(source, limits)


def load_schema_store(source, limits=DEFAULT_LIMITS):
    """Load an external mapping/JSON file, or an Argo store bytes object."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return read_schema_store(source, limits)
    if isinstance(source, (str, PathLike)):
        path = Path(source)
        if path.stat().st_size > limits.max_message_bytes:
            raise ArgoError("schema store exceeds limit")
        if path.suffix.lower() == ".json":
            source = load_json(path, limits)
        else:
            return read_schema_store(_read_file(path, limits), limits)
    return validate_schema_store(source, limits)
