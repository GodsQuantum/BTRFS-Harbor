"""Contract: installed Ubuntu boot qualification is not synthetic BusyBox."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_real_distro_root_is_installed_in_disposable_ci_disk() -> None:
    fixture = (
        ROOT / "tests/integration/tier3/test_installed_ubuntu_uefi_real.py"
    ).read_text()
    workflow = (ROOT / ".github/workflows/installed-ubuntu-acceptance.yml").read_text()
    assert 'assert os.environ.get("GITHUB_ACTIONS") == "true"' in fixture
    assert "isolated mount namespace required" in fixture
    assert '"debootstrap"' in fixture
    assert '"--variant=minbase"' in fixture
    assert '"noble"' in fixture
    assert "str(src_root)" in fixture
    assert 'src_root / "var/lib/dpkg/status"' in fixture
    assert 'src_root / "sbin/init"' in fixture
    assert "HARBOR_INSTALLED_UBUNTU_RESTORED_OK" in fixture
    assert "for index in (1, 2)" in fixture
    assert "test_installed_ubuntu_uefi_real.py" in workflow
    assert "debootstrap" in workflow
    assert "preview/checkpoint-resume-v026" in workflow
