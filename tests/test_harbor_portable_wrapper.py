from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
WRAPPER = REPO_ROOT / "packaging" / "universal" / "btrfs-backup-ng"


def test_portable_wrapper_never_writes_python_bytecode_into_extracted_appimage():
    text = WRAPPER.read_text()
    assert "PYTHONDONTWRITEBYTECODE=1" in text
