"""Test-suite safety net: no real block device may ever be touched by tests."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from usbprep import constants as C, runner, safety  # noqa: E402

assert os.environ.get(C.REAL_DEVICE_ENV) is None, "the test suite must never run with real devices allowed"
safety.REAL_DEVICES_BLOCKED = True


def _refuse_dev_args(argv):
    for a in argv:
        if a == "if=/dev/zero":      # reading the zero source is harmless; the target is still checked
            continue
        if "/dev/" in str(a):
            raise PermissionError(f"test safety hook: real command referencing a device refused: {argv}")


runner.GUARD_HOOKS.append(_refuse_dev_args)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def fixture_text():
    def load(name):
        with open(os.path.join(FIX, name)) as f:
            return f.read()
    return load


@pytest.fixture
def workdir(tmp_path):
    assert str(tmp_path).startswith("/home/ubuntu/"), tmp_path
    return tmp_path


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """VM-handoff markers etc. go to a per-test directory, never the real ~/.local/state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("USBPREP_CONFIG_DIR", str(tmp_path / "config"))  # stars/backend choice never leak
    monkeypatch.delenv("USBPREP_SESSION_STARS", raising=False)
