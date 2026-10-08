"""Incremental replay must preserve the exact immutable parent *path* too."""

from dataclasses import replace

import pytest

from btrfs_backup_ng.core.checkpoint_v2 import (
    parse_manifest,
    serialize_manifest,
)
from btrfs_backup_ng.core.replay_v2 import (
    validate_resume_identity,
    DestinationFingerprint,
    SourceFingerprint,
)
from btrfs_backup_ng.core.checkpoint_v2 import ResumeManifest


def good_manifest():
    return ResumeManifest(
        schema_version=2,
        transfer_id="3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
        identity={
            "profile_id": "test",
            "source_volume": "/",
            "source_uuid": "b0cbeb30-9917-44d8-9f27-1ed967f91a2d",
            "source_path": "/snapshots/12/snapshot",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 3,
            "encryption": "none",
            "destination_type": "raw",
            "destination_fingerprint": "nfs:host:/share",
            "kernel_release": "7.0",
            "btrfs_progs_version": "7.1",
            "harbor_version": "0.2.6",
            "engine_version": "0.9.12",
        },
        checkpoint_size=16,
        state="paused",
        checkpoints=(),
    )


PARENT = "31211247-96d6-4675-ac24-79fcf9e7624f"


def test_incremental_manifest_roundtrips_parent_path():
    old = good_manifest()
    m = replace(
        old,
        identity={
            **old.identity,
            "parent_uuid": PARENT,
            "parent_path": "/snapshots/10/snapshot",
        },
    )
    decoded = parse_manifest(serialize_manifest(m))
    assert decoded.identity["parent_path"] == "/snapshots/10/snapshot"


def test_incremental_manifest_refuses_parent_uuid_without_path():
    old = good_manifest()
    m = replace(old, identity={**old.identity, "parent_uuid": PARENT})
    with pytest.raises(ValueError, match="parent"):
        serialize_manifest(m)


def test_incremental_manifest_refuses_traversal_in_parent_path():
    old = good_manifest()
    m = replace(
        old,
        identity={
            **old.identity,
            "parent_uuid": PARENT,
            "parent_path": "/snapshots/../etc",
        },
    )
    with pytest.raises(ValueError, match="parent"):
        serialize_manifest(m)


def test_parent_identity_check_remains_uuid_specific():
    old = good_manifest()
    m = replace(
        old,
        identity={
            **old.identity,
            "parent_uuid": PARENT,
            "parent_path": "/snapshots/10/snapshot",
        },
    )
    s = SourceFingerprint(
        uuid=old.identity["source_uuid"],
        parent_uuid=PARENT,
        path=old.identity["source_path"],
        send_fingerprint=old.identity["send_fingerprint"],
        readonly=True,
    )
    d = DestinationFingerprint(
        type="raw", fingerprint=old.identity["destination_fingerprint"]
    )
    validate_resume_identity(m, s, d)
    with pytest.raises(ValueError, match="identity"):
        validate_resume_identity(
            m, replace(s, parent_uuid="20e561c9-732d-49b3-ae61-13abf91cedd2"), d
        )
