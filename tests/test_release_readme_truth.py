"""Published release status must not be misrepresented in any locale."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("filename", "unpublished_word", "history_note"),
    [
        ("README.md", "unpublished", "Historical release notes"),
        ("README.fr.md", "non publiée", "Notes des versions précédentes"),
        ("README.zh-CN.md", "未发布", "历史版本说明"),
    ],
)
def test_rc11_is_published_and_old_sections_are_historical(
    filename: str, unpublished_word: str, history_note: str
) -> None:
    contents = (ROOT / filename).read_text(encoding="utf-8")
    current = contents.split("## v0.2.6-rc.11", 1)[1].split("## v0.2.6-rc.10", 1)[0]
    assert history_note in current
    assert unpublished_word not in current.split(history_note, 1)[0]
    assert (
        "https://github.com/GodsQuantum/BTRFS-Harbor/releases/tag/v0.2.6-rc.11"
        in current
    )
    assert history_note in contents.split("## v0.2.6-rc.10", 1)[0]
