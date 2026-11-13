"""Menu-bar settings: what is saved, what is not, and how a broken file is survived."""
import json

import pytest

from consolelink import app
from consolelink.settings_store import SettingsStore


class FakePasswords:
    def __init__(self, value="", fail=False):
        self.value, self.fail = value, fail

    def get(self):
        if self.fail:
            raise RuntimeError("keychain locked")
        return self.value

    def set(self, password):
        self.value = password


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "ConsoleLink" / "settings.json", FakePasswords())


def test_defaults_match_the_command_line(store):
    s = store.load()
    assert (s.listen, s.port, s.allow_write, s.password, s.artnet_dest) == \
        ("localhost", 8765, False, "", None)


def test_round_trip_keeps_the_password_out_of_the_file(store):
    wanted = app.Settings(listen="network", port=9000, allow_write=True, password="secret",
                          artnet_dest="192.168.1.50", artnet_universe=3)
    store.save(wanted)
    assert "secret" not in store.path.read_text()
    assert store.password_store.value == "secret"
    loaded = store.load()
    assert loaded == wanted


def test_blank_artnet_means_off(store):
    store.save(app.Settings(artnet_dest=None))
    assert store.load().artnet_dest is None


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", '{"port": "80", "listen": 5}'])
def test_broken_file_falls_back_to_defaults(store, content):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content)
    s = store.load()
    assert (s.listen, s.port) == ("localhost", 8765)


def test_wrong_types_are_ignored_per_field(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"port": True, "allow_write": "yes", "artnet_universe": 4}))
    s = store.load()
    assert (s.port, s.allow_write, s.artnet_universe) == (8765, False, 4)


def test_network_control_without_a_password_comes_up_read_only(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"listen": "network", "allow_write": True}))
    s = store.load()
    assert (s.listen, s.allow_write) == ("network", False)


def test_unavailable_keychain_does_not_stop_loading(tmp_path):
    s = SettingsStore(tmp_path / "s.json", FakePasswords(fail=True)).load()
    assert s.password == ""


def test_smartsoft_bridge_is_remembered(store):
    store.save(app.Settings(smartsoft_bridge=47561))
    assert store.load().smartsoft_bridge == 47561
    store.save(app.Settings())
    assert store.load().smartsoft_bridge is None


@pytest.mark.parametrize("value", ["47561", True, 1.5])
def test_smartsoft_bridge_of_the_wrong_type_is_off(store, value):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"smartsoft_bridge": value}))
    assert store.load().smartsoft_bridge is None


def test_bridge_with_page_control_comes_up_read_only(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"smartsoft_bridge": 47561, "allow_write": True}))
    s = store.load()
    assert (s.smartsoft_bridge, s.allow_write) == (47561, False)


def test_bridge_port_out_of_range_turns_it_off(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"smartsoft_bridge": 70000}))
    assert store.load().smartsoft_bridge is None
