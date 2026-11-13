"""ConsoleLink as a macOS menu-bar app (no Dock icon): the same service the command line runs
(app.Service), driven from a menu instead of flags.

    pip install 'consolelink[menubar]'    (or pipx install, see the README)
    consolelink-menubar        (or: python -m consolelink.menubar)

The menu shows whether the console is connected, opens the page, shows (and copies) the address
for another device, and holds the settings: network access, control from the page, the control password
(kept in the Keychain), the Art-Net destination and universe, the port, and the SmartSoft bridge
(SmartSoft for Windows under CrossOver drives the console through ConsoleLink; it offers to install
the bridge DLL in SmartSoft's bottle when that is missing or out of date). A change is saved and
applied by stopping and restarting the service. Quit releases the USB interface first.

Only the console can be claimed by one process at a time: don't run this and `consolelink` together.
The decisions live in menubar_logic.py and settings_store.py (testable anywhere); this file is the
thin rumps/Cocoa layer, and the only one that needs macOS.
"""
import atexit
import os
import signal
import sys
import webbrowser

from . import app, menubar_logic as logic
from .settings_store import SettingsStore

try:
    import rumps
    from . import menubar_qr  # Core Image via PyObjC: macOS only, like rumps
except ImportError:  # not macOS, or the [menubar] extra isn't installed: main() explains
    rumps = None
    menubar_qr = None


def _icon_path(connected):
    """The menu-bar icon file for the connection state (see scripts/make_menubar_icons.py)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "menubar_icons", logic.icon_name(connected) + ".png")


def _copy_to_clipboard(text):
    from AppKit import NSPasteboard, NSPasteboardTypeString
    board = NSPasteboard.generalPasteboard()
    board.clearContents()
    board.setString_forType_(text, NSPasteboardTypeString)


def _ask(title, message, default="", secure=False):
    """A one-line input dialog: the typed text, or None if cancelled."""
    window = rumps.Window(message=message, title=title, default_text=default, ok="OK",
                          cancel="Cancel", dimensions=(260, 24), secure=secure)
    response = window.run()
    return response.text if response.clicked else None


class ConsoleLinkApp(rumps.App if rumps else object):
    def __init__(self):
        # A template image: macOS tints it for the light/dark menu bar.
        super().__init__("ConsoleLink", icon=_icon_path(False), template=True, quit_button=None)
        self._shown_connected = False
        self.store = SettingsStore()
        self.settings = self.store.load()
        self.service = None
        self.status_item = rumps.MenuItem("Waiting for console…")  # rumps keys items by title: keep distinct
        self.server_item = rumps.MenuItem("Server address")
        self.qr_item = rumps.MenuItem("Show QR code…", callback=self.show_qr)
        self.qr_window = None  # menubar_qr.QRWindow, made on first use
        self._server_url = None
        try:
            menubar_qr.warm_up()  # before the USB thread starts: see warm_up()
        except menubar_qr.QRUnavailable:
            pass  # "Show QR code…" will say so when it is used
        self._start_service_or_alert()
        self.rebuild_menu()
        if self.settings.smartsoft_bridge is not None:
            self.check_bridge_setup()
        self.timer = rumps.Timer(self.refresh, 1)
        self.timer.start()
        self._refresh_while_menu_open()
        # Cocoa ends the process with exit(), skipping Python's own shutdown (so atexit never
        # runs and the poll thread is never joined) on Ctrl+C, logout and "Quit" from the Dock or
        # Activity Monitor -- and the console would be left claimed, with a stalled pipe for the
        # next start. before_quit fires first in all of those cases.
        rumps.events.before_quit.register(self.stop_service)
        atexit.register(self.stop_service)  # in case the loop ends some other way
        # Python only sees the signal when it gets control back, which the 1 s timer guarantees.
        signal.signal(signal.SIGTERM, lambda *_: self.quit(None))

    def _refresh_while_menu_open(self):
        """rumps schedules its timer in the default run-loop mode only, which doesn't run while a
        menu is open (the loop is in event-tracking mode): the status line would freeze until the
        menu closes. Also schedule it in the common modes, which include tracking."""
        from Foundation import NSRunLoop, NSRunLoopCommonModes
        nstimer = getattr(self.timer, "_nstimer", None)  # rumps' private handle
        if nstimer is not None:
            NSRunLoop.currentRunLoop().addTimer_forMode_(nstimer, NSRunLoopCommonModes)

    # --- the service ---------------------------------------------------------------------

    def start_service(self):
        self.service = app.Service(self.settings).start()

    def stop_service(self):
        if self.service is not None:
            self.service.stop()
            self.service = None

    def _start_service_or_alert(self):
        try:
            self.start_service()
        except (ValueError, OSError) as e:
            rumps.alert("ConsoleLink can't start", str(e))

    def apply(self, **changes):
        """Save and apply a settings change; False (with an alert) if it can't be done, in
        which case the previous settings keep running."""
        try:
            new = logic.changed(self.settings, **changes)
        except ValueError as e:
            rumps.alert("ConsoleLink", str(e))
            return False
        old = self.settings
        # Names only: the control password must not end up in a terminal or a log.
        print(f"[consolelink] settings changed ({', '.join(sorted(changes))}): restarting",
              file=sys.stderr)
        self.stop_service()
        self.settings = new
        try:
            self.start_service()
        except (ValueError, OSError) as e:
            rumps.alert("ConsoleLink", f"Can't apply that: {e}")
            self.settings = old
            self._start_service_or_alert()
            self.rebuild_menu()
            return False
        self.store.save(new)
        self.rebuild_menu()
        return True

    # --- the menu ------------------------------------------------------------------------

    def rebuild_menu(self):
        s = self.settings
        toggle = lambda title, callback, on: self._checked(rumps.MenuItem(title, callback=callback), on)
        art = s.artnet_dest or "off"
        settings_menu = rumps.MenuItem("Settings")
        for item in (
            rumps.MenuItem("Remote access"),   # header: no callback, so greyed out
            toggle("Allow other devices on the network", self.toggle_network, s.listen == "network"),
            # Greyed out (no callback) while SmartSoft drives the console: the two exclude each other.
            toggle("Allow control from the page",
                   None if s.smartsoft_bridge is not None else self.toggle_write, s.allow_write),
            rumps.MenuItem("Control password…", callback=self.set_password),
            rumps.MenuItem(f"Port… ({s.port})", callback=self.set_port),
            None,                              # separator
            rumps.MenuItem("Art-Net"),         # header
            rumps.MenuItem(f"Destination… ({art})", callback=self.set_artnet),
            rumps.MenuItem(f"Universe… ({s.artnet_universe})", callback=self.set_universe),
            None,
            rumps.MenuItem("SmartSoft (CrossOver)"),   # header
            toggle("Relay SmartSoft to the console", self.toggle_bridge,
                   s.smartsoft_bridge is not None),
        ):
            settings_menu.add(item)
        items = [self.status_item, rumps.MenuItem("Open ConsoleLink", callback=self.open_page)]
        if s.listen == "network":
            items += [self.server_item, self.qr_item]
        items += [None, settings_menu, None, rumps.MenuItem("Quit ConsoleLink", callback=self.quit)]
        self.menu.clear()
        self.menu = items
        self.refresh(None)

    @staticmethod
    def _checked(item, on):
        item.state = 1 if on else 0
        return item

    def refresh(self, _):
        with app.state_lock:
            connected, device = app.state["connected"], app.state["device"]
            busy = app.state["console_busy"]
        if connected != self._shown_connected:  # re-reading the icon file only on a change
            self._shown_connected = connected
            self.icon = _icon_path(connected)
        self.status_item.title = logic.status_text(connected, device, busy)
        if self.service is not None and self.service.port:
            url = logic.server_url(self.settings, self.service.port, logic.lan_ip())
            self.server_item.title = (f"Server address: {url} (click to copy)" if url
                                      else "Server address: no network address")
            self.server_item.set_callback(self.copy_server_url if url else None)
            self.qr_item.set_callback(self.show_qr if url else None)
            self._server_url = url
        if self.qr_window is not None and self.qr_window.is_open():
            if self._server_url:
                self.qr_window.update(self._server_url)  # the address changed: a new code
            else:
                self.qr_window.close()  # not served to the network any more

    # --- actions -------------------------------------------------------------------------

    def open_page(self, _):
        if self.service is not None and self.service.port:
            webbrowser.open(logic.local_url(self.service.port))

    def copy_server_url(self, _):
        if self._server_url:
            _copy_to_clipboard(self._server_url)

    def show_qr(self, _):
        if not self._server_url:
            return
        try:
            if self.qr_window is None:
                self.qr_window = menubar_qr.QRWindow()
            self.qr_window.show(self._server_url)
        except menubar_qr.QRUnavailable as e:
            rumps.alert("ConsoleLink", f"Can't draw the QR code: {e}\n\nThe address is "
                                       f"{self._server_url}")

    def _ask_password(self):
        return _ask("Control password",
                    "Devices on the network need this password to control the console.",
                    secure=True)

    def toggle_network(self, _):
        turning_on = self.settings.listen != "network"
        changes = {"listen": "network" if turning_on else "localhost"}
        if turning_on and self.settings.allow_write and not self.settings.password:
            password = self._ask_password()
            if not password:
                return self.rebuild_menu()
            changes["password"] = password
        self.apply(**changes) or self.rebuild_menu()

    def toggle_write(self, _):
        turning_on = not self.settings.allow_write
        changes = {"allow_write": turning_on}
        if turning_on and self.settings.listen == "network" and not self.settings.password:
            password = self._ask_password()
            if not password:
                return self.rebuild_menu()
            changes["password"] = password
        self.apply(**changes) or self.rebuild_menu()

    def toggle_bridge(self, _):
        turning_on = self.settings.smartsoft_bridge is None
        if self.apply(**logic.bridge_changes(self.settings, turning_on)):
            if turning_on:
                self.check_bridge_setup()
        else:
            self.rebuild_menu()

    def check_bridge_setup(self):
        """Offer to install the bridge DLL when SmartSoft's bottle lacks it, or has an old one."""
        from . import smartsoft_install
        problem = smartsoft_install.setup_problem()
        if problem is None:
            return
        if rumps.alert("SmartSoft bridge", logic.bridge_setup_message(problem),
                       ok="Install", cancel="Not now") != 1:
            return
        error = logic.install_bridge()
        if error:
            rumps.alert("SmartSoft bridge", f"Can't install the bridge DLL: {error}")
        else:
            rumps.alert("SmartSoft bridge", "Installed. Start SmartSoft in CrossOver: it finds "
                                            "the console through ConsoleLink.")

    def set_password(self, _):
        password = _ask("Control password",
                        "Leave empty for none (only possible while the page is served to this "
                        "machine alone).", secure=True)
        if password is not None:
            self.apply(password=password)

    def set_artnet(self, _):
        dest = _ask("Art-Net destination",
                    "An IP address, or 'broadcast'. Leave empty to turn Art-Net output off.",
                    default=self.settings.artnet_dest or "")
        if dest is not None:
            self.apply(artnet_dest=dest)

    def set_universe(self, _):
        text = _ask("Art-Net universe",
                    "Universe for DMX 1 (DMX 2 goes to the next one).",
                    default=str(self.settings.artnet_universe))
        if text is not None:
            try:
                self.apply(artnet_universe=logic.parse_universe(text))
            except ValueError as e:
                rumps.alert("ConsoleLink", str(e))

    def set_port(self, _):
        text = _ask("Port", "Port of the web page.", default=str(self.settings.port))
        if text is not None:
            try:
                self.apply(port=logic.parse_port(text))
            except ValueError as e:
                rumps.alert("ConsoleLink", str(e))

    def quit(self, _):
        self.stop_service()
        rumps.quit_application()


def main():
    if sys.platform != "darwin":
        sys.exit("The menu-bar app is for macOS. On this system run `consolelink` instead.")
    if rumps is None:
        sys.exit("The menu-bar app needs rumps and keyring: pip install 'consolelink[menubar]'")
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    ConsoleLinkApp().run()


if __name__ == "__main__":
    main()
