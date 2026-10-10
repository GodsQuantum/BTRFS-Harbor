"""Real OpenSSH loopback, restricted key, host identity and outage/resume.

Runs ONLY on disposable GitHub runner, no real user's device or remote host.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from btrfs_backup_ng.ssh_checkpoint_v2_client import SSHMirror

NAME = "machine20261010T235000-deadbeef-000.btrfs.zst"


def main() -> None:
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    temp = Path(os.environ["RUNNER_TEMP"])
    private = temp / "harbor-client"
    known = temp / "harbor-known-hosts"
    source = temp / NAME
    payload = bytes(range(256)) * 2048
    if not source.exists():
        source.write_bytes(payload)
    assert source.read_bytes() == payload
    ssh = SSHMirror(
        host="127.0.0.1",
        user="harborrecv",
        port=22222,
        key=private,
        known_hosts=known,
    )
    base = {
        "version": 1,
        "name": NAME,
        "total": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    phase = os.environ["HARBOR_NETWORK_PHASE"]
    if phase == "first":
        status = ssh.request({**base, "op": "status"})
        assert status["committed"] == 0 and not status["complete"]
        first = payload[:100_000]
        digest = hashlib.sha256(first).hexdigest()
        # A crashed/partially arrived SSH frame must not advance the journal.
        try:
            ssh.request(
                {
                    **base,
                    "op": "put",
                    "offset": 0,
                    "size": len(first),
                    "chunk_sha256": digest,
                },
                first[:-1],
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("truncated network frame wrongly accepted")
        assert ssh.request({**base, "op": "status"})["committed"] == 0
        reply = ssh.request(
            {
                **base,
                "op": "put",
                "offset": 0,
                "size": len(first),
                "chunk_sha256": digest,
            },
            first,
        )
        assert reply["committed"] == len(first)
        # sshd must reject a non-allowed SSH_ORIGINAL_COMMAND.
        forbidden = subprocess.run(
            [*ssh.argv[:-1], "echo unsafe"],
            input=b"",
            capture_output=True,
            timeout=25,
            check=False,
        )
        assert forbidden.returncode != 0
        print(
            "REAL SSH RECV: forced command, truncated frame, durable first chunk passed"
        )
    elif phase == "outage":
        try:
            ssh.request({**base, "op": "status"})
        except RuntimeError:
            print("REAL SSH OUTAGE: fail closed")
        else:
            raise AssertionError("server outage silently reported success")
    elif phase == "resume":
        status = ssh.request({**base, "op": "status"})
        assert status["committed"] == 100_000
        assert ssh.mirror_file(source)["already_present"] is False
        assert ssh.mirror_file(source)["already_present"] is True
        assert ssh.request({**base, "op": "status"})["complete"] is True
        print(
            "REAL SSH RECOVERY: exact committed offset, SHA-256 and final publication passed"
        )
    else:
        raise ValueError("invalid CI phase")


if __name__ == "__main__":
    main()
