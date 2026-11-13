"""The menu-bar app's decisions, independent of rumps/macOS."""
import socket

import pytest

from consolelink import app, menubar_logic as logic, smartsoft_bridge


def test_status_and_title():
    assert logic.status_text(True, "SmartFade ML") == "● Connected · SmartFade ML"
    assert logic.status_text(True, None) == "● Connected"
    assert logic.status_text(False, None) == "○ Waiting for console…"
    assert logic.status_text(False, None, busy=True) == "○ Console in use by another program"
    assert logic.icon_name(True) != logic.icon_name(False)


def test_icon_files_exist():
    from pathlib import Path
    folder = Path(logic.__file__).parent / "menubar_icons"
    for name in (logic.ICON_CONNECTED, logic.ICON_WAITING):
        assert (folder / f"{name}.png").is_file()


def test_server_url_only_when_served_to_the_network():
    local = app.Settings()
    network = app.Settings(listen="network")
    assert logic.server_url(local, 8765, "192.168.1.110") is None
    assert logic.server_url(network, 8765, "192.168.1.110") == "http://192.168.1.110:8765"
    assert logic.server_url(network, 8765, None) is None


def test_needs_password():
    assert logic.needs_password(app.Settings(listen="network", allow_write=True))
    assert not logic.needs_password(app.Settings(listen="network", allow_write=True, password="x"))
    assert not logic.needs_password(app.Settings(listen="localhost", allow_write=True))
    assert not logic.needs_password(app.Settings(listen="network"))


def test_changed_validates_and_normalizes():
    s = app.Settings()
    assert logic.changed(s, port=9000).port == 9000
    assert logic.changed(s, artnet_dest="  ").artnet_dest is None
    assert logic.changed(s, artnet_dest=" 10.0.0.2 ").artnet_dest == "10.0.0.2"
    with pytest.raises(ValueError):
        logic.changed(app.Settings(allow_write=True), listen="network")  # needs a password
    assert logic.changed(app.Settings(allow_write=True), listen="network", password="pw").listen == "network"


@pytest.mark.parametrize("text,ok", [("8765", True), (" 80 ", True), ("0", False), ("65536", False),
                                       ("abc", False), ("", False), ("-1", False)])
def test_parse_port(text, ok):
    if ok:
        assert logic.parse_port(text) == int(text)
    else:
        with pytest.raises(ValueError):
            logic.parse_port(text)


@pytest.mark.parametrize("text,ok", [("0", True), ("32766", True), ("32767", False), ("x", False)])
def test_parse_universe(text, ok):
    if ok:
        assert logic.parse_universe(text) == int(text)
    else:
        with pytest.raises(ValueError):
            logic.parse_universe(text)


def test_lan_ip(monkeypatch):
    class FakeSocket:
        def __init__(self, ip): self.ip = ip
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def connect(self, addr): pass
        def getsockname(self): return (self.ip, 1234)

    for ip, expected in [("192.168.1.110", "192.168.1.110"), ("127.0.0.1", None), ("0.0.0.0", None)]:
        monkeypatch.setattr(socket, "socket", lambda *a, ip=ip: FakeSocket(ip))
        assert logic.lan_ip() == expected

    def broken(*a):
        raise OSError("no network")
    monkeypatch.setattr(socket, "socket", broken)
    assert logic.lan_ip() is None


def test_menubar_module_imports_without_rumps(monkeypatch):
    """The core and the launcher stay importable where rumps isn't (Linux, Windows, Pi)."""
    import importlib
    import sys
    monkeypatch.setitem(sys.modules, "rumps", None)  # makes `import rumps` raise ImportError
    mod = importlib.reload(importlib.import_module("consolelink.menubar"))
    assert mod.rumps is None
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert "macOS" in str(exc.value)


# ---- the SmartSoft bridge toggle

def test_bridge_toggle_turns_off_control_from_the_page(bridge_platform):
    s = app.Settings(allow_write=True)
    on = logic.changed(s, **logic.bridge_changes(s, True))
    assert on.smartsoft_bridge == smartsoft_bridge.DEFAULT_PORT and not on.allow_write
    off = logic.changed(on, **logic.bridge_changes(on, False))
    assert off.smartsoft_bridge is None


def test_bridge_setup_message():
    text = logic.bridge_setup_message(('CrossOver bottle "Smartfade"', "is out of date"))
    assert text.startswith('The SmartSoft bridge DLL in CrossOver bottle "Smartfade" is out of date')
    assert "Quit SmartSoft" in text and "Install" in text


def test_install_bridge(monkeypatch):
    from consolelink import smartsoft_install as si
    calls = []
    monkeypatch.setattr(si, "find_target", lambda: "target")
    monkeypatch.setattr(si, "packaged_dll", lambda: b"dll")
    monkeypatch.setattr(si, "install", lambda target, dll, out: calls.append((target, dll)))
    assert logic.install_bridge() is None
    assert calls == [("target", b"dll")]

    def running(*a, **k):
        raise si.SetupError("SmartSoft is running in CrossOver bottle \"S\" (pid 42): quit it first")
    monkeypatch.setattr(si, "install", running)
    assert "quit it first" in logic.install_bridge()
