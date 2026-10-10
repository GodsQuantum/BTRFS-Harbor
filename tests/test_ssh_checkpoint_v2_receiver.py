"""Restricted SSH v2: journaled chunks, restart, no shell or path traversal."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from btrfs_backup_ng.ssh_checkpoint_v2_receiver import (
    _names,
    dispatch_receiver,
    initialize_receiver,
    safe_name,
)
from btrfs_backup_ng.ssh_checkpoint_v2_client import SSHMirror

FILE = "machine20261010T235000-deadbeef-000.btrfs.zst"


def command(op: str, payload: bytes, **kwargs: object) -> dict[str, object]:
    return {
        "version": 1,
        "op": op,
        "name": FILE,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "total": len(payload),
        **kwargs,
    }


def test_resume_after_truncated_stream_and_unjournaled_tail(tmp_path: Path) -> None:
    initialize_receiver(tmp_path, allow_local=True)
    payload = b"abc" * 23000
    first = payload[:20000]
    args = command(
        "put",
        payload,
        offset=0,
        size=len(first),
        chunk_sha256=hashlib.sha256(first).hexdigest(),
    )
    with pytest.raises(ValueError, match="truncated"):
        dispatch_receiver(tmp_path, args, io.BytesIO(first[:-1]))
    assert (
        dispatch_receiver(tmp_path, command("status", payload), io.BytesIO())[
            "committed"
        ]
        == 0
    )
    assert dispatch_receiver(tmp_path, args, io.BytesIO(first))["committed"] == len(
        first
    )
    partial, _ = _names(FILE)
    with (tmp_path / partial).open("ab") as stream:
        stream.write(b"UNCOMMITTED-TAIL")
    # An interrupted upload cannot accidentally count unfsynced bytes.
    assert dispatch_receiver(tmp_path, command("status", payload), io.BytesIO())[
        "committed"
    ] == len(first)
    assert (tmp_path / partial).stat().st_size == len(first)
    remaining = payload[len(first) :]
    args2 = command(
        "put",
        payload,
        offset=len(first),
        size=len(remaining),
        chunk_sha256=hashlib.sha256(remaining).hexdigest(),
    )
    assert dispatch_receiver(tmp_path, args2, io.BytesIO(remaining))[
        "committed"
    ] == len(payload)
    assert (
        dispatch_receiver(tmp_path, command("finish", payload), io.BytesIO())[
            "complete"
        ]
        is True
    )
    assert (tmp_path / FILE).read_bytes() == payload
    assert (
        dispatch_receiver(tmp_path, command("status", payload), io.BytesIO())[
            "complete"
        ]
        is True
    )


def test_remote_mismatched_offset_and_integrity_do_not_commit(tmp_path: Path) -> None:
    initialize_receiver(tmp_path, allow_local=True)
    payload = b"test-remote-integrity"
    bad = command("put", payload, offset=0, size=len(payload), chunk_sha256="0" * 64)
    with pytest.raises(ValueError, match="SHA256"):
        dispatch_receiver(tmp_path, bad, io.BytesIO(payload))
    assert (
        dispatch_receiver(tmp_path, command("status", payload), io.BytesIO())[
            "committed"
        ]
        == 0
    )
    wrong = command(
        "put", payload, offset=1, size=1, chunk_sha256=hashlib.sha256(b"t").hexdigest()
    )
    with pytest.raises(ValueError, match="offset"):
        dispatch_receiver(tmp_path, wrong, io.BytesIO(b"t"))
    with pytest.raises(ValueError, match="incomplete"):
        dispatch_receiver(tmp_path, command("finish", payload), io.BytesIO())


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "/tmp/file",
        "a;rm -rf /",
        ".ssh/authorized_keys",
        ".harbor-boot-../../evil.tar.gz",
        "a" * 250,
    ],
)
def test_ssh_name_allowlist_refuses_foreign_paths(name: str) -> None:
    with pytest.raises(ValueError):
        safe_name(name)


def test_key_forced_command_refuses_shell_suffix(tmp_path: Path) -> None:
    initialize_receiver(tmp_path, allow_local=True)
    payload = b"hello"
    req = command("status", payload)
    env = {**os.environ, "SSH_ORIGINAL_COMMAND": "harbor-v2;id"}
    trial = subprocess.run(
        [
            sys.executable,
            "-m",
            "btrfs_backup_ng",
            "raw",
            "checkpoint-v2",
            "ssh-receiver",
            "--root",
            str(tmp_path),
        ],
        input=(json.dumps(req) + "\n").encode(),
        env=env,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert trial.returncode != 0
    assert not (tmp_path / FILE).exists()


def test_torn_publish_link_is_repaired_after_crash(tmp_path: Path) -> None:
    initialize_receiver(tmp_path, allow_local=True)
    payload = b"durable-checkpoint"
    digest = hashlib.sha256(payload).hexdigest()
    args = command("put", payload, offset=0, size=len(payload), chunk_sha256=digest)
    dispatch_receiver(tmp_path, args, io.BytesIO(payload))
    partial, journal = _names(FILE)
    os.link(tmp_path / partial, tmp_path / FILE)
    assert (tmp_path / FILE).stat().st_nlink == 2
    result = dispatch_receiver(tmp_path, command("status", payload), io.BytesIO())
    assert result["complete"] is True
    assert (tmp_path / FILE).stat().st_nlink == 1
    assert not (tmp_path / partial).exists()
    assert not (tmp_path / journal).exists()


def test_transport_fails_closed_on_wrong_remote_identity(tmp_path: Path) -> None:
    key = tmp_path / "key"
    known = tmp_path / "known_hosts"
    key.write_text("private")
    known.write_text("localhost ssh-ed25519 TEST")
    with pytest.raises(ValueError):
        SSHMirror(host="-ProxyCommand=sh", user="backup", key=key, known_hosts=known)
    with pytest.raises(ValueError):
        SSHMirror(host="localhost", user="-oProxyCommand", key=key, known_hosts=known)
    with pytest.raises(ValueError):
        SSHMirror(host="localhost", user="backup", port=-1, key=key, known_hosts=known)


def test_client_resume_after_network_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import btrfs_backup_ng.ssh_checkpoint_v2_client as module

    recv = tmp_path / "receiver"
    recv.mkdir()
    initialize_receiver(recv, allow_local=True)
    source = tmp_path / FILE
    payload = os.urandom(3 * 64 * 1024)
    source.write_bytes(payload)
    key, known = tmp_path / "key", tmp_path / "known_hosts"
    key.write_text("k")
    known.write_text("h")
    client = SSHMirror(host="localhost", user="backup", key=key, known_hosts=known)
    monkeypatch.setattr(module, "MAX_CHUNK", 64 * 1024)
    steps = {"puts": 0, "up": True}

    def fake_request(
        request: dict[str, object], blob: bytes = b""
    ) -> dict[str, object]:
        if request["op"] == "put":
            steps["puts"] += 1
            if steps["puts"] == 2 and steps["up"]:
                raise RuntimeError("simulated real-network disconnect")
        return dispatch_receiver(recv, request, io.BytesIO(blob))

    monkeypatch.setattr(client, "request", fake_request)
    with pytest.raises(RuntimeError, match="disconnect"):
        client.mirror_file(source)
    assert (
        dispatch_receiver(recv, command("status", payload), io.BytesIO())["committed"]
        == 64 * 1024
    )
    steps["up"] = False
    assert client.mirror_file(source)["already_present"] is False
    assert (recv / FILE).read_bytes() == payload
    assert client.mirror_file(source)["already_present"] is True
