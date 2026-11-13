"""`consolelink smartsoft install|uninstall|status` (smartsoft_install.py), on fake bottles: no
CrossOver, Wine or SmartSoft needed. Wine and lsof are replaced by a recording `run`."""
import subprocess
import sys

import pytest

from consolelink import app
from consolelink import smartsoft_install as si

BRIDGE_DLL = b"MZ...---- SmartSoft bridge loaded (backend %s)...v2"
OLD_BRIDGE_DLL = b"MZ...---- SmartSoft bridge loaded (backend %s)...v1"
OTHER_DLL = b"MZ...some other hid.dll"

# The override section as Wine writes it in user.reg (backslashes doubled, a timestamp after the
# key, then a #time line).
USER_REG_WITH_OVERRIDE = (
    "WINE REGISTRY Version 2\n"
    "\n"
    "[Software\\\\Wine\\\\AppDefaults\\\\SmartSoft.exe\\\\DllOverrides] 1794529876\n"
    "#time=1dd73064bcb5fc2\n"
    "\"hid\"=\"native,builtin\"\n"
    "\n"
    "[Software\\\\Wine\\\\Drivers] 1794529876\n"
    "\"hid\"=\"something else\"\n")


def make_bottle(bottles, name, with_smartsoft=True):
    bottle = bottles / name
    binaries = bottle / "drive_c" / "Program Files (x86)" / "ETC" / "SmartSoft" / "Binaries"
    binaries.mkdir(parents=True)
    if with_smartsoft:
        (binaries / "SmartSoft.exe").write_bytes(b"MZ")
    (bottle / "user.reg").write_text("WINE REGISTRY Version 2\n")
    return bottle


class FakeSystem:
    """Records commands; answers lsof with `pids`, and applies `reg add/delete` to user.reg
    the way Wine would once it has saved the registry."""

    def __init__(self, target):
        self.target = target
        self.pids = []
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        reg = self.target.prefix / "user.reg"
        if "-t" in cmd:   # lsof -t SmartSoft.exe
            return subprocess.CompletedProcess(cmd, 0 if self.pids else 1,
                                               "".join(f"{p}\n" for p in self.pids), "")
        if "reg" in cmd and "add" in cmd:
            reg.write_text(USER_REG_WITH_OVERRIDE)
        elif "reg" in cmd and "delete" in cmd:
            reg.write_text("WINE REGISTRY Version 2\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def bottles(tmp_path, monkeypatch):
    monkeypatch.setattr(si.shutil, "which", lambda name: f"/usr/bin/{name}")
    return tmp_path / "Bottles"


@pytest.fixture
def target(bottles):
    make_bottle(bottles, "SmartSoft")
    wine = bottles.parent / "bin" / "wine"
    wine.parent.mkdir()
    wine.write_text("")
    (wine.parent / "wineserver").write_text("")
    return si.find_target(bottles_dir=bottles, wine_candidates=[wine])


def quiet(*args):
    pass


# ---- finding SmartSoft

def test_finds_the_one_bottle_with_smartsoft(bottles, target):
    make_bottle(bottles, "Steam", with_smartsoft=False)
    found = si.find_target(bottles_dir=bottles, wine_candidates=[])
    assert found.prefix.name == "SmartSoft"
    assert found.exe.name == "SmartSoft.exe"
    assert found.wine is None   # no CrossOver found: install will say so
    assert target.wine[1:] == ["--bottle", "SmartSoft"]
    assert target.wineserver.endswith("wineserver")


def test_several_bottles_need_a_choice(bottles):
    make_bottle(bottles, "A")
    make_bottle(bottles, "B")
    with pytest.raises(si.SetupError, match="several bottles \\(A, B\\).*--bottle"):
        si.find_target(bottles_dir=bottles, wine_candidates=[])
    assert si.find_target(bottle="B", bottles_dir=bottles, wine_candidates=[]).prefix.name == "B"


def test_reports_what_is_missing(bottles):
    with pytest.raises(si.SetupError, match="no CrossOver bottle with SmartSoft"):
        si.find_target(bottles_dir=bottles, wine_candidates=[])
    make_bottle(bottles, "Steam", with_smartsoft=False)
    with pytest.raises(si.SetupError, match='no CrossOver bottle "Nope"'):
        si.find_target(bottle="Nope", bottles_dir=bottles, wine_candidates=[])
    with pytest.raises(si.SetupError, match='bottle "Steam" has no SmartSoft'):
        si.find_target(bottle="Steam", bottles_dir=bottles, wine_candidates=[])


def test_plain_wine_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(si.shutil, "which", lambda name: f"/usr/bin/{name}")
    prefix = make_bottle(tmp_path, "wineprefix")
    found = si.find_target(prefix=prefix)
    assert found.wine == ["/usr/bin/wine"]
    assert found.env == {"WINEPREFIX": str(prefix)}


# ---- reading the current state

def test_dll_state(target):
    assert si.dll_state(target, BRIDGE_DLL) == "absent"
    target.dll.write_bytes(BRIDGE_DLL)
    assert si.dll_state(target, BRIDGE_DLL) == "current"
    target.dll.write_bytes(OLD_BRIDGE_DLL)
    assert si.dll_state(target, BRIDGE_DLL) == "outdated"
    target.dll.write_bytes(OTHER_DLL)
    assert si.dll_state(target, BRIDGE_DLL) == "foreign"


def test_override_read_from_user_reg():
    assert si.parse_user_reg(USER_REG_WITH_OVERRIDE) == "native,builtin"
    assert si.parse_user_reg("WINE REGISTRY Version 2\n") is None
    # only the SmartSoft.exe section counts
    assert si.parse_user_reg(USER_REG_WITH_OVERRIDE.split("[Software\\\\Wine\\\\Drivers]")[0]
                             .replace("SmartSoft.exe", "Other.exe")) is None


# ---- install / uninstall / status

def test_install_then_uninstall(target):
    fake = FakeSystem(target)
    si.install(target, BRIDGE_DLL, run=fake.run, out=quiet)
    assert target.dll.read_bytes() == BRIDGE_DLL
    assert si.override_value(target) == "native,builtin"
    reg_add = next(c for c in fake.calls if "add" in c)
    assert reg_add[-7:] == ["add", si.OVERRIDE_KEY, "/v", "hid", "/d", "native,builtin", "/f"]
    assert fake.calls[-1][-1] == "-w"   # waited for Wine to save the registry

    fake.calls.clear()
    si.install(target, BRIDGE_DLL, run=fake.run, out=quiet)   # again: nothing to change
    assert not any("reg" in c for c in fake.calls)

    si.uninstall(target, run=fake.run, out=quiet)
    assert not target.dll.exists()
    assert si.override_value(target) is None


def test_install_updates_an_older_bridge(target):
    target.dll.write_bytes(OLD_BRIDGE_DLL)
    si.install(target, BRIDGE_DLL, run=FakeSystem(target).run, out=quiet)
    assert target.dll.read_bytes() == BRIDGE_DLL


def test_refused_while_smartsoft_runs(target):
    fake = FakeSystem(target)
    fake.pids = [25084, 25098]
    with pytest.raises(si.SetupError, match=r"running .*pid 25084, 25098.*quit it first"):
        si.install(target, BRIDGE_DLL, run=fake.run, out=quiet)
    with pytest.raises(si.SetupError, match="quit it first"):
        si.uninstall(target, run=fake.run, out=quiet)
    assert not target.dll.exists()


def test_a_foreign_hid_dll_is_kept(target):
    target.dll.write_bytes(OTHER_DLL)
    fake = FakeSystem(target)
    with pytest.raises(si.SetupError, match="isn't the bridge's.*--force"):
        si.install(target, BRIDGE_DLL, run=fake.run, out=quiet)
    assert target.dll.read_bytes() == OTHER_DLL

    si.install(target, BRIDGE_DLL, force=True, run=fake.run, out=quiet)
    assert target.dll.read_bytes() == BRIDGE_DLL
    assert target.backup.read_bytes() == OTHER_DLL
    si.uninstall(target, run=fake.run, out=quiet)
    assert target.dll.read_bytes() == OTHER_DLL   # restored
    assert not target.backup.exists()


def test_uninstall_leaves_a_foreign_hid_dll(target):
    target.dll.write_bytes(OTHER_DLL)
    si.uninstall(target, run=FakeSystem(target).run, out=quiet)
    assert target.dll.read_bytes() == OTHER_DLL


def test_install_without_a_built_dll(target):
    with pytest.raises(si.SetupError, match="make -C bridge/windows package"):
        si.install(target, None, run=FakeSystem(target).run, out=quiet)


def test_install_without_wine(bottles):
    make_bottle(bottles, "SmartSoft")
    target = si.find_target(bottles_dir=bottles, wine_candidates=[])
    with pytest.raises(si.SetupError, match="can't find Wine/CrossOver.*--wine"):
        si.install(target, BRIDGE_DLL, run=FakeSystem(target).run, out=quiet)


def test_status(target):
    fake = FakeSystem(target)
    lines = []
    si.status(target, BRIDGE_DLL, run=fake.run, out=lines.append)
    assert lines[1:] == ["Bridge DLL:   not installed", "DLL override: not set",
                         "SmartSoft is: not running"]
    si.install(target, BRIDGE_DLL, run=fake.run, out=quiet)
    fake.pids = [42]
    lines.clear()
    si.status(target, BRIDGE_DLL, run=fake.run, out=lines.append)
    assert lines[1:] == ["Bridge DLL:   installed, up to date", "DLL override: set",
                         "SmartSoft is: running (pid 42)"]


# ---- command line

def test_refused_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SystemExit) as exc:
        si.main(["status"])
    assert exc.value.code == 2


def test_consolelink_smartsoft_dispatches(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "argv", ["consolelink", "smartsoft", "status", "--bottle", "X"])
    monkeypatch.setattr(si, "main", lambda argv: seen.append(argv) or 0)
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 0
    assert seen == [["status", "--bottle", "X"]]


def test_reports_errors_with_exit_code_1(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    def no_target(**kwargs):
        raise si.SetupError("no CrossOver bottle with SmartSoft in /x")
    monkeypatch.setattr(si, "find_target", no_target)
    assert si.main(["install"]) == 1
    assert "consolelink smartsoft: no CrossOver bottle" in capsys.readouterr().err


# ---- the reminder printed when `consolelink --smartsoft-bridge` starts

def test_startup_warning(target):
    def find():
        return target
    assert "isn't installed" in si.startup_warning(BRIDGE_DLL, find)
    si.install(target, BRIDGE_DLL, run=FakeSystem(target).run, out=quiet)
    assert si.startup_warning(BRIDGE_DLL, find) is None
    newer = BRIDGE_DLL + b"v3"
    assert "is out of date" in si.startup_warning(newer, find)
    assert "consolelink smartsoft install" in si.startup_warning(newer, find)
    (target.prefix / "user.reg").write_text("WINE REGISTRY Version 2\n")
    assert "missing its DLL override" in si.startup_warning(BRIDGE_DLL, find)


def test_startup_warning_stays_quiet_when_it_cannot_tell(target, monkeypatch):
    def no_bottle():
        raise si.SetupError("no CrossOver bottle with SmartSoft")
    def broken():
        raise OSError("unreadable")
    assert si.startup_warning(BRIDGE_DLL, no_bottle) is None   # none, or several: no guessing
    assert si.startup_warning(BRIDGE_DLL, broken) is None      # never stops the bridge
    monkeypatch.setattr(si, "packaged_dll", lambda: None)
    assert si.startup_warning(None, lambda: target) is None    # no DLL in this package
