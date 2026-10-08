"""V2 CLI must be explicit, opt-in and never change legacy raw run behavior."""

from btrfs_backup_ng.cli.dispatcher import create_subcommand_parser


def parse(*argv):
    return create_subcommand_parser().parse_args(list(argv))


def test_v2_help_actions_are_isolated_from_legacy_raw():
    arg = parse(
        "raw",
        "checkpoint-v2",
        "status",
        "--target",
        "/backup",
        "--transfer-id",
        "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
    )
    assert arg.command == "raw"
    assert arg.raw_action == "checkpoint-v2"
    assert arg.checkpoint_action == "status"


def test_v2_start_requires_explicit_allow_local_for_local_destination():
    arg = parse(
        "raw",
        "checkpoint-v2",
        "start",
        "--target",
        "/backup",
        "--source",
        "/snapshots/1/snapshot",
        "--name",
        "snap",
        "--profile-id",
        "profile-1",
    )
    assert arg.allow_local is False
    assert arg.checkpoint_size_mib == 128


def test_v2_resume_explicit_id():
    arg = parse(
        "raw",
        "checkpoint-v2",
        "resume",
        "--target",
        "/backup",
        "--transfer-id",
        "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
        "--name",
        "snap",
    )
    assert arg.checkpoint_action == "resume"
    assert arg.transfer_id


def test_v2_discard_requires_confirmation():
    arg = parse(
        "raw",
        "checkpoint-v2",
        "discard",
        "--target",
        "/backup",
        "--transfer-id",
        "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
        "--name",
        "snap",
    )
    assert arg.confirm is False
