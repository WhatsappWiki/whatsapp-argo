"""Independent vectors: produced and decoded by unmodified erlang-argo.

Fixture JSON tags are test representation, never an Argo wire encoding.
"""
import json
from pathlib import Path

import pytest

from whatsapp_argo import (
    ArgoError, FieldErrors, Header, read, read_schema_store, write,
    write_schema_store,
)


FIXTURE = json.loads((Path(__file__).parent / "fixtures/official-vectors.json").read_text())
for _v in FIXTURE["vectors"]:
    for _kind, _models in FIXTURE["models"].items():
        if f"{_kind}_ref" in _v:
            _v[_kind] = _models[_v[f"{_kind}_ref"]]
VALUES = [v for v in FIXTURE["vectors"] if v.get("kind", "value") == "value"]
STORES = [v for v in FIXTURE["vectors"] if v.get("kind") == "store"]


def unpack(value):
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            return bytes.fromhex(value["$bytes"])
        if set(value) == {"$field_errors"}:
            return FieldErrors(tuple(unpack(e) for e in value["$field_errors"]))
        return {k: unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unpack(v) for v in value]
    return value


def header(case):
    flags = case["flags"]
    return Header(
        inline_everything=bool(flags & 1), self_describing=bool(flags & 2),
        out_of_band_field_errors=bool(flags & 4), self_describing_errors=bool(flags & 8),
        null_terminated_strings=bool(flags & 16), no_deduplication=bool(flags & 32),
        user_flags=tuple(c == "1" for c in case["user_flags"]) if flags & 64 else None,
    )


@pytest.mark.parametrize("case", VALUES, ids=lambda v: v["id"])
def test_official_encoder_and_decoder_cross_vectors(case):
    assert case["official_decode_equal"] is True
    raw = bytes.fromhex(case["wire_hex"])
    expected = unpack(case["value"])
    document = read(raw, case["schema"])
    assert document.value == expected
    assert document.header == header(case)
    assert write(expected, case["schema"], header=header(case)) == raw
    assert write(document) == raw


@pytest.mark.parametrize("case", STORES, ids=lambda v: v["id"])
def test_official_store_vectors(case):
    expected = {v["name"]: v["type"] for v in case["store_json"]["types"]}
    raw = bytes.fromhex(case["wire_hex"])
    assert read_schema_store(raw) == expected
    assert write_schema_store(expected, header=header(case)) == raw


@pytest.mark.parametrize("case", [v for v in VALUES if v["id"].startswith("desc-") and v["flags"] & 2], ids=lambda v: v["id"])
def test_official_self_describing_without_external_schema(case):
    raw = bytes.fromhex(case["wire_hex"])
    assert read(raw).value == unpack(case["value"])
    assert write(unpack(case["value"]), header=header(case)) == raw


@pytest.mark.parametrize("flags", [0, 1, 2, 4, 8, 16, 32, 64, 127])
def test_official_values_mutate_before_fresh_write(flags):
    case = next(v for v in VALUES if v["id"] == f"header-matrix-{flags:02x}")
    raw = bytes.fromhex(case["wire_hex"])
    document = read(raw, case["schema"])
    document.value["data"]["label"] = "modified safely"
    changed = write(document)
    assert changed != raw
    assert read(changed, case["schema"]).value["data"]["label"] == "modified safely"


@pytest.mark.parametrize("name", ["header-matrix-00", "header-matrix-01", "header-matrix-7f", "desc-02", "inband-errors-00"])
def test_official_truncations_and_extra_data(name):
    case = next(v for v in VALUES if v["id"] == name)
    raw = bytes.fromhex(case["wire_hex"])
    for end in range(len(raw)):
        with pytest.raises(ArgoError):
            read(raw[:end], case["schema"])
    with pytest.raises(ArgoError):
        read(raw + b"\0", case["schema"])


def test_fixture_provenance_and_header_coverage():
    provenance = FIXTURE["provenance"]
    assert provenance["implementation"] == "WhatsApp/erlang-argo"
    assert len(provenance["revision"]) == 40
    assert len(provenance["source_tree_sha256"]) == 64
    assert {v["flags"] for v in VALUES if v["id"].startswith("header-matrix-")} == set(range(128))
