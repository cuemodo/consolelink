"""Persistent settings for the menu-bar app (the command line has its own flags).

Everything but the control password goes into a JSON file in the user's application-support
folder. The password goes into the macOS Keychain instead (via the `keyring` package, loaded only
when needed), so it never sits in a file or on a command line. Pure Python apart from that, so the
rules can be tested on any OS with a fake password store.
"""
import dataclasses
import json
import os
from pathlib import Path

from . import app

KEYCHAIN_SERVICE = "ConsoleLink"
KEYCHAIN_ACCOUNT = "control-password"

# The Settings fields worth remembering between runs: the rest (debug, capture, Art-Net rate...)
# are for the command line.
PERSISTED = ("listen", "port", "allow_write", "artnet_dest", "artnet_universe", "smartsoft_bridge")


def default_path():
    return Path.home() / "Library" / "Application Support" / "ConsoleLink" / "settings.json"


class KeychainPassword:
    """The control password in the macOS Keychain."""

    def get(self):
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT) or ""

    def set(self, password):
        import keyring
        if password:
            keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, password)
        else:
            try:
                keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            except keyring.errors.PasswordDeleteError:
                pass  # there was none


class SettingsStore:
    def __init__(self, path=None, password_store=None):
        self.path = Path(path) if path else default_path()
        self.password_store = password_store or KeychainPassword()

    def load(self):
        """The saved settings; defaults for a missing or unreadable file, and for any value of
        the wrong type. Never raises: a broken preferences file must not stop the app."""
        defaults = app.Settings()
        try:
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        values = {}
        for name in PERSISTED:
            value = saved.get(name, getattr(defaults, name))
            wanted = type(getattr(defaults, name))
            if name == "artnet_dest":
                ok = value is None or isinstance(value, str)
            elif name == "smartsoft_bridge":   # a port, or None for off
                ok = value is None or type(value) is int
            else:
                ok = type(value) is wanted
            values[name] = value if ok else getattr(defaults, name)
        try:
            password = self.password_store.get()
        except Exception:  # keychain locked/denied/unavailable: carry on without a password
            password = ""
        settings = dataclasses.replace(defaults, password=password, **values)
        try:
            settings.validate()
        except ValueError:
            # e.g. a saved network + control without a password (the keychain entry is gone), or
            # control together with the SmartSoft bridge: come up read-only rather than refusing
            # to start.
            settings = dataclasses.replace(settings, allow_write=False)
            try:
                settings.validate()
            except ValueError:   # still wrong (a bridge port out of range): bridge off
                settings = dataclasses.replace(settings, smartsoft_bridge=None)
        return settings

    def save(self, settings):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {name: getattr(settings, name) for name in PERSISTED}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file
        self.password_store.set(settings.password)
