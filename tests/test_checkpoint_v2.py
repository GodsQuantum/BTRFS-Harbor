"""Manifest v2 contract: strict fields, ordered checkpoints and authentic index."""

import importlib
import json
from dataclasses import replace


def api():
    return importlib.import_module("btrfs_backup_ng.core.checkpoint_v2")


def identity():
    return {
        "profile_id": "profile-11",
        "source_volume": "/",
        "source_uuid": "b0cbeb30-9917-44d8-9f27-1ed967f91a2d",
        "source_path": "/snapshots/12/snapshot",
        "parent_uuid": None,
        "send_fingerprint": "protocol=2:compressed-data=off",
        "compression": "zstd",
        "compression_level": 3,
        "encryption": "none",
        "destination_type": "raw",
        "destination_fingerprint": "nfs:server:/export/backup",
        "kernel_release": "6.12.0",
        "btrfs_progs_version": "7.1",
        "harbor_version": "0.2.6",
        "engine_version": "0.9.12",
        "snapper_config": "root",
        "snapper_number": 12,
        "snapper_type": "single",
        "snapper_pre_number": None,
    }


def good_manifest():
    m = api()
    return m.ResumeManifest(
        schema_version=2,
        transfer_id="3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
        identity=identity(),
        checkpoint_size=5,
        state="paused",
        checkpoints=(
            m.Checkpoint(
                sequence=0,
                raw_offset=0,
                raw_length=5,
                compressed_offset=0,
                compressed_length=4,
                raw_sha256="a" * 64,
                compressed_sha256="b" * 64,
                committed_at="2026-10-08T20:00:00+00:00",
            ),
            m.Checkpoint(
                sequence=1,
                raw_offset=5,
                raw_length=3,
                compressed_offset=4,
                compressed_length=7,
                raw_sha256="c" * 64,
                compressed_sha256="d" * 64,
                committed_at="2026-10-08T20:01:00+00:00",
            ),
        ),
    )


def test_manifest_round_trip_has_deterministic_index_digest():
    m = api()
    encoded = m.serialize_manifest(good_manifest())
    payload = json.loads(encoded)
    assert payload["schema_version"] == 2
    assert len(payload["checkpoint_index_sha256"]) == 64
    assert payload["committed_raw_bytes"] == 8
    assert payload["committed_compressed_bytes"] == 11
    assert m.parse_manifest(encoded) == good_manifest()
    assert m.serialize_manifest(m.parse_manifest(encoded)) == encoded


def test_rejects_noncontiguous_offsets():
    m = api()
    original = good_manifest()
    broken = replace(
        original,
        checkpoints=(
            original.checkpoints[0],
            replace(original.checkpoints[1], raw_offset=6),
        ),
    )
    try:
        m.serialize_manifest(broken)
    except ValueError as exc:
        assert "offset" in str(exc).lower()
    else:
        raise AssertionError("noncontiguous checkpoint accepted")


def test_rejects_corrupted_index_digest():
    m = api()
    payload = json.loads(m.serialize_manifest(good_manifest()))
    payload["checkpoints"][0]["raw_length"] = 42
    try:
        m.parse_manifest(json.dumps(payload).encode())
    except ValueError:
        pass
    else:
        raise AssertionError("altered checkpoint index accepted")


def test_rejects_invalid_hash_even_when_serializing():
    m = api()
    original = good_manifest()
    broken = replace(
        original,
        checkpoints=(
            replace(original.checkpoints[0], raw_sha256="z" * 64),
            original.checkpoints[1],
        ),
    )
    try:
        m.serialize_manifest(broken)
    except ValueError:
        pass
    else:
        raise AssertionError("non-hex checksum accepted")


def test_rejects_unsupported_version():
    m = api()
    payload = json.loads(m.serialize_manifest(good_manifest()))
    payload["schema_version"] = 3
    try:
        m.parse_manifest(json.dumps(payload).encode())
    except ValueError:
        pass
    else:
        raise AssertionError("future schema accepted")


def test_rejects_untrusted_source_path():
    m = api()
    original = good_manifest()
    broken = replace(
        original, identity={**original.identity, "source_path": "../../escape"}
    )
    try:
        m.serialize_manifest(broken)
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal accepted")


def test_rejects_unknown_state_and_missing_identity():
    m = api()
    original = good_manifest()
    for broken in (
        replace(original, state="magically_complete"),
        replace(
            original,
            identity={
                key: value for key, value in identity().items() if key != "source_uuid"
            },
        ),
    ):
        try:
            m.serialize_manifest(broken)
        except ValueError:
            continue
        raise AssertionError("invalid manifest accepted")
