"""Malformed caller-supplied layout decisions must fail without fallback."""
from dataclasses import replace

import pytest

from whatsapp_argo import ArgoError, Header, read, write


def string_document():
    schema = {"type": "ARRAY", "of": "STRING"}
    return read(write(["same", "same"], schema), schema, preserve_layout=True)


@pytest.mark.parametrize("layout", [{}, 1, "invalid"])
def test_preserved_layout_requires_a_layout_object(layout):
    document = replace(string_document(), layout=layout)
    with pytest.raises(ArgoError, match="layout"):
        write(document, preserve_layout=True)


@pytest.mark.parametrize("events", [None, 7, iter(())])
def test_preserved_layout_requires_an_event_sequence(events):
    document = string_document()
    document.layout = replace(document.layout, events=events)
    with pytest.raises(ArgoError, match="layout"):
        write(document, preserve_layout=True)


@pytest.mark.parametrize("event", [
    None, 7, (), ("scalar", "STRING", "String"),
    ("scalar", "STRING", "String", "extra", None),
])
def test_layout_event_requires_the_exact_shape(event):
    document = string_document()
    document.layout = replace(document.layout, events=(event,) + document.layout.events[1:])
    with pytest.raises(ArgoError, match="layout"):
        write(document, preserve_layout=True)


@pytest.mark.parametrize("reference", [0.0, True, False, "0", [], -1, 100])
def test_layout_backreference_requires_an_existing_integer_index(reference):
    document = string_document()
    event = ("scalar", "STRING", "String", reference)
    document.layout = replace(document.layout, events=(document.layout.events[0], event))
    with pytest.raises(ArgoError, match="layout|backreference"):
        write(document, preserve_layout=True)


@pytest.mark.parametrize("schema", [
    "DESC",
    {"type": "RECORD", "fields": [{"name": "a", "of": "VARINT"}]},
])
@pytest.mark.parametrize("order", [None, 1, "a", ([],), ("a", "a"), ()])
def test_layout_object_order_cannot_fall_back_or_use_invalid_keys(schema, order):
    document = read(write({"a": 1}, schema, header=Header(self_describing=True)),
                    schema, preserve_layout=True)
    first = document.layout.events[0]
    document.layout = replace(document.layout, events=((first[0], order),) + document.layout.events[1:])
    with pytest.raises(ArgoError, match="layout"):
        write(document, preserve_layout=True)


def test_unused_layout_is_ignored_when_preservation_is_not_requested():
    document = string_document()
    expected = write(document)
    document.layout = {"invalid": "unused"}
    assert write(document) == expected


def test_valid_list_containers_for_layout_events_remain_supported():
    document = read(write({"a": 1}, "DESC"), "DESC", preserve_layout=True)
    expected = write(document, preserve_layout=True)
    events = list(document.layout.events)
    events[0] = ("object", ["a"])
    document.layout = replace(document.layout, events=events)
    assert write(document, preserve_layout=True) == expected
