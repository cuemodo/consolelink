"""
Minimal local web app showing live console state: the 24 physical faders'
current output, plus the console's per-mode intensity memory.

Run:
    consolelink [--debug] [--capture PATH] [--no-web] [--artnet [DEST]]
              [--listen localhost|network] [--port N] [--allow-write [PASSWORD]]
              [--smartsoft-bridge [PORT]]
(or `python -m consolelink ...`; `pip install -e .` from the repo creates the command).

--smartsoft-bridge [PORT] (experimental): instead of polling the console, relay ETC SmartSoft for
Windows running under CrossOver (bridge/windows/ in the repository, and smartsoft_bridge.py). The
page and Art-Net follow the relayed traffic, read-only.
Then open http://localhost:8765 in a browser (--port N for another port). By default only this machine can reach the page
(--listen localhost); --listen network also serves it to other devices on the same network, at
http://<this Mac's LAN IP>:8765 (e.g. `ipconfig getifaddr en0` for the IP; macOS will prompt to
allow incoming connections for python3 the first time a LAN client connects).

--allow-write [PASSWORD] (off by default) lets the page control the console: press its buttons
(BlackOut, Solo, Ind 1/2, the fader-mode buttons, the MEMS-only Int Only / Go Mode), hold a Bump
button, drag the 24 faders, MASTER, BUMPS, LIVE and NEXT, and pick the MEMS page. The page sends
POST /api/button|bump|fader|mems_page/... (see Handler.do_POST); with a
PASSWORD each request must carry ?pw=PASSWORD (the page asks for it in its settings; POST
/api/auth only checks it). The password travels in the URL over plain HTTP, so it keeps casual
users out and nothing more. It is required with --listen network, optional with --listen
localhost: without one, writes are accepted only from a page on localhost (Host and Origin
headers are checked, so another website open in the browser cannot press buttons). Writes go to the
USB thread, the only one that touches the device, through a queue -- or, for faders, a dict that
keeps only the latest value per fader.

Terminal only, no web server: add --no-web (with --debug, --capture and/or --artnet -- there is
nothing else for it to do). --debug prints every control change to stderr, plus a line for any
message type that has no decoder yet ("known, undecoded" or "UNEXPECTED", with the first bytes),
for finding not-yet-decoded controls.

--capture PATH appends every message (known or not) to PATH as a timestamped line with the full,
untruncated hex, for reading back like a packet capture: run with --capture capture.log, press
whatever is being investigated, Ctrl+C, then read the file. It is also the format the tests'
fixtures are cut from.

Stdlib only (plus pyusb, already required by protocol.py). No build step, no
npm, no framework -- a background thread polls the console over USB and a
plain http.server serves a static page over Server-Sent Events (GET
/api/events, text/event-stream) so every update is pushed the moment it's
decoded, rather than the page polling and silently skipping whatever changed
between polls.

Reuses the USB protocol implementation from protocol.py rather than re-deriving it -- see that
module's docstring for the wire-protocol details (idle poll, announce/ack handshake,
request_type(), type=0x0e/0x17 decoding).

Shows: the per-slot Intensity values (INT A, INT B, INT DEV) for all 24
fader slots -- these are the console's stored intensity levels, not raw
physical fader positions. Only whichever mode is currently active on the
console's fader-mode selector reflects live physical fader movement
(type=0x0e); the other two rows come from the console's separately
maintained intensity memory (type=0x0f) and only change when a fader is
moved while that mode is active. Plus Master (a real physical fader) and
Bumps. Independent 1/2, per the manual, are toggle/bump buttons (not
faders) -- shown as lights, decoded from type=0x0c. Also a dedicated
Physical Faders row: 24 bar+light indicators, one per physical fader,
mode-agnostic (type=0x0e for the live value, type=0x16 for the Bump LED --
solid/color-proportional once caught, blinking while the physical fader
hasn't yet caught its stored logical value after a mode switch). type=0x16 also carries the Solo,
BlackOut and MEMS-only Int Only / Go Mode LEDs; every LED's on/off/blinking is read from its real
colors (protocol.light_state) and sent to the page. And the console's two
LCDs, mirrored as text (type=0x15), and the Crossfader Live/Next levels (type=0x11). And the
console's DMX output, both universes (type=0x0d), for the "DMX Outputs" tab.

The fader and intensity values are output levels, level x Crossfader Live x Master (both
type=0x0e and the stored type=0x0f bank): a fader at full with Live and Master at 60% reads
36%, not 100%.
"""
import argparse
import dataclasses
import hmac
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

import usb.core
import usb.util

try:
    import libusb_package  # bundles libusb; not installed where it has no wheel (see pyproject.toml)
except ImportError:
    libusb_package = None

from . import artnet
from . import protocol as sfl
from . import smartsoft_bridge

# --listen: "localhost" binds loopback only (this machine); "network" binds every interface, so
# other devices on the network (a phone, another computer) can connect.
LISTEN_HOSTS = {"localhost": "127.0.0.1", "network": "0.0.0.0"}
DEFAULT_PORT = 8765

# Set from --capture PATH by Service.start(): log every message (known or not) with full hex + a timestamp
# (see the module docstring).
capture_path = ""

INTENSITY_MODES = ("INT A", "INT B", "INT DEV")  # sub-modes with a decoded intensity bank (type=0x0e/0x0f)
ALL_FADER_MODES = INTENSITY_MODES + ("PARAM 1", "PARAM 2", "MEMS")  # every mode the physical fader-mode selector (type=0x17) can report

state = {
    "version": sfl.VERSION,
    "connected": False,
    "device": None,  # console model name (sfl.MODEL_NAMES) while connected, else None
    "fader_mode": None,  # None = unknown -- never actually seen a type=0x17; not a guess like "INT A"
    "fader_mode_confirmed": False,  # False = fader_mode is still unknown/unconfirmed
    "mems_page": None,  # 1-indexed MEMS memory page (1-4 seen so far), from type=0x17's
    # data[2] -- see decode_0x17_mems_page in protocol.py. Only meaningful while
    # fader_mode == "MEMS"; None until a MEMS type=0x17 has actually been seen.
    "intensities": {mode: [0] * 24 for mode in INTENSITY_MODES},
    "labels": dict({mode: {} for mode in INTENSITY_MODES}, MEMS={}),  # MEMS is nested one
    # level deeper: {page (1-indexed): {slot (1-indexed): [lines]}}, from type=0x00
    # (decode_0x00_memory_name in protocol.py) -- one entry per recorded memory.
    "independent_labels": {},
    "physical_faders": [0] * 24,  # live output level per fader (x Crossfader Live x Master), mode-agnostic (type=0x0e)
    "physical_fader_lights": [None] * 24,  # None until the first type=0x16 seen for that
    # fader; then {"blinking": True, "color_a": [r, g, b], "color_b": [r, g, b]} or
    # {"blinking": False, "color": [r, g, b]} -- the console's own LED color, see
    # decode_0x16_bump_catch in protocol.py
    "physical_fader_states": [None] * 24,  # each Bump LED as "on"/"off"/"blinking" (light_state)
    "bumps": 0,
    "master": 0,
    "crossfader_live": 0,  # Crossfader Live/Next scene levels, raw 0-255 (type=0x11)
    "crossfader_next": 0,
    "independent1": None,  # None until the first type=0x0c message; then a raw 0-255 value
    "independent2": None,
    "independent1_clicked": None,  # None until the first type=0x0c message; then True/False -- a
    "independent2_clicked": None,  # separate bit from the value above
    "solo": None,  # None until the first type=0x16 message; then "on"/"off"/"blinking"
    "blackout": None,
    "go_mode": None,   # the MEMS-only GO MODE / INT ONLY LEDs, same states (off outside MEMS)
    "int_only": None,
    "indicator_lights": {"solo": None, "blackout": None, "go_mode": None, "int_only": None},  # the console's own Solo/BlackOut
    # LED colors, same shape as physical_fader_lights -- see decode_0x16_indicator_lights
    "lcd": None,  # None until the first type=0x15; then the console's 4 LCD lines,
    # [LCD 1 line 1, LCD 1 line 2, LCD 2 line 1, LCD 2 line 2] -- see decode_0x15
    "dmx": None,  # None until the first type=0x0d; then [universe 1, universe 2], each a list of
    # 512 raw 0-255 levels (index 0 = address 1) -- see decode_0x0d_dmx in protocol.py
    "console_busy": False,  # True while a console is plugged in but another program holds it
    "write_enabled": False,  # True when started with --allow-write; the page enables its buttons
    "write_password_required": False,  # True when writes need the password; False: the page needs none
    "last_update": 0.0,
}
state_lock = threading.Lock()
state_condition = threading.Condition(state_lock)  # notified on every state change, for SSE

def update_state(**kwargs):
    with state_condition:
        state.update(kwargs)
        state["last_update"] = time.time()
        state_condition.notify_all()

# Off by default -- run with --debug for per-control change logging, a liveness heartbeat, and
# announce/ack handshake visibility. Distinguishes "nothing arrived on the wire" from "arrived
# but decoded/rendered wrong" far faster than guessing. Set from the settings by Service.start().
debug = False
_last_logged_master = None
_last_logged_selection = None
_last_logged_intensities = {mode: [0] * 24 for mode in INTENSITY_MODES}
_t_start = time.time()

# Console button taps requested over HTTP (POST /api/button/<name>), sent by the USB poll thread
# -- the only thread that touches the device -- between idle polls. The fader-mode buttons are
# taps too: the console acts on the press edge.
BUTTONS = {"blackout": sfl.BUTTON_BLACKOUT, "solo": sfl.BUTTON_SOLO,
           "ind1": sfl.BUTTON_IND1, "ind2": sfl.BUTTON_IND2,
           "mode-int-a": sfl.BUTTON_MODE_INT_A, "mode-int-b": sfl.BUTTON_MODE_INT_B,
           "mode-int-dev": sfl.BUTTON_MODE_INT_DEV, "mode-param-1": sfl.BUTTON_MODE_PARAM_1,
           "mode-param-2": sfl.BUTTON_MODE_PARAM_2, "mode-mems": sfl.BUTTON_MODE_MEMS,
           "int-only": sfl.BUTTON_INT_ONLY, "go-mode": sfl.BUTTON_GO_MODE}
# Set from --allow-write by Service.start(). Off by default: writing is refused (403) and the page's
# buttons stay inert.
write_enabled = False
# The password the page must send (?pw=...) with every write; set with --allow-write PASSWORD.
# Empty means no password: only possible on localhost (--listen localhost), where do_POST instead
# checks that the request comes from this machine's own page (see _from_own_page).
write_password = ""
# Events for the poll thread: ("tap", code), ("press", code), ("keepalive", code), ("release", code),
# ("page", n).
button_queue = queue.Queue()
# Fader moves requested over HTTP (POST /api/fader/<1-24|master|bumps|live|next>/<0-255>): only
# the latest value per fader matters, so a fast drag overwrites instead of queueing, like SmartSoft's own
# send queue.
fader_writes = {}
fader_writes_lock = threading.Lock()

# A Bump button is held, not tapped (INT modes: the channel stays bumped for as long as the
# button is down), so the page sends press and release separately -- and sends a keepalive
# every ~250 ms. The console never releases a button by itself (a bump stays on
# if the USB cable is pulled mid-press), so HeldButtons releases for the client when the
# keepalive stops (BUMP_LEASE: a phone off Wi-Fi, a closed tab), when a hold runs past
# BUMP_MAX_HOLD, and on shutdown.
BUMP_LEASE = 0.75
BUMP_MAX_HOLD = 30.0


class HeldButtons:
    """Buttons currently pressed on the console, owned by the poll thread. `clock` is injectable
    so the timing can be tested without sleeping."""

    def __init__(self, link, clock=time.monotonic):
        self.link = link
        self.clock = clock
        self.held = {}  # code -> {"since", "seen", "release"}

    def press(self, code):
        """A press from the client. Pressed again before the release went out, it is one
        continuous hold."""
        now = self.clock()
        h = self.held.get(code)
        if h is None:
            if self.link.send_button(code, True):
                self.held[code] = {"since": now, "seen": now, "release": False}
        else:
            h["seen"] = now
            h["release"] = False

    def keepalive(self, code):
        """The client still holds `code`. Refresh-only, never a new press: a keepalive that
        was in flight when the release arrived must not re-press the button."""
        h = self.held.get(code)
        if h is not None:
            h["seen"] = self.clock()

    def release(self, code):
        h = self.held.get(code)
        if h is not None:
            h["release"] = True

    def service(self):
        """Send the releases that are due. The press is not stretched: the console ignores one
        shorter than ~0.15 s, and how long to press is up to the user."""
        now = self.clock()
        for code, h in list(self.held.items()):
            due = h["release"] or now - h["seen"] > BUMP_LEASE or now - h["since"] > BUMP_MAX_HOLD
            if due:
                self.link.send_button(code, False)
                del self.held[code]

    def release_all(self):
        for code in list(self.held):
            self.link.send_button(code, False)
        self.held.clear()


# Set from --artnet by Service.start(): an artnet.ArtNetSender fed with every DMX snapshot, or None.
artnet_sender = None

def handle_payload(obj_type, data):
    global _last_logged_master, _last_logged_selection
    # Faders (only for active fader mode), bumps and master snapshot
    if obj_type == 0x0e:
        full = sfl.decode_0x0e_full(data)
        if full is not None:
            faders, bumps, master = full
            if debug and master != _last_logged_master:
                print(f"[master] t={time.time() - _t_start:7.3f}  {_last_logged_master} -> {master}",
                      file=sys.stderr)
                _last_logged_master = master
            with state_condition:
                mode = state["fader_mode"]
                # Gated on the 3 known INTENSITY_MODES, not just "not None": mode can also be
                # PARAM 1/PARAM 2/MEMS, but state["intensities"] only has INT A/B/DEV entries --
                # whether type=0x0e's raw fader value even means "intensity" in those other
                # modes is still an open RE question, and indexing with "PARAM 1" is a KeyError.
                if mode in INTENSITY_MODES:
                    if debug and faders != _last_logged_intensities[mode]:
                        changed = {i + 1: (_last_logged_intensities[mode][i], v)
                                   for i, v in enumerate(faders) if v != _last_logged_intensities[mode][i]}
                        print(f"[intensity:{mode}] t={time.time() - _t_start:7.3f}  "
                              f"confirmed={state['fader_mode_confirmed']}  changed={changed}",
                              file=sys.stderr)
                        _last_logged_intensities[mode] = faders
                    state["intensities"][mode] = faders
                # Unlike intensities[mode] above, this isn't gated on a known mode -- it's the
                # raw live value straight off the wire, not attributed to any particular
                # intensity bank, so there's no "assumed mode" risk in setting it unconditionally.
                state["physical_faders"] = faders
                state["bumps"] = bumps
                state["master"] = master
                state["last_update"] = time.time()
                state_condition.notify_all()
    # Fader mode
    elif obj_type == 0x17:
        mode = sfl.decode_0x17(data)
        if mode in ALL_FADER_MODES:
            if debug:
                print(f"[mode] t={time.time() - _t_start:7.3f}  "
                      f"{state['fader_mode']} -> {mode} (now confirmed)", file=sys.stderr)
            update = {"fader_mode": mode, "fader_mode_confirmed": True}
            if mode == "MEMS":
                page = sfl.decode_0x17_mems_page(data)
                if page is not None:
                    update["mems_page"] = page
                    if debug:
                        print(f"[mems page] t={time.time() - _t_start:7.3f}  page={page}",
                              file=sys.stderr)
            update_state(**update)
    # Labels pages
    elif obj_type == 0x09:
        labels = sfl.decode_0x09_labels(data)
        if labels:
            if debug:
                received = [(family, index, lines)
                            for family, entries in labels.items()
                            for index, lines in entries.items()]
                for family, index, lines in received:
                    print(f"[label] {family} {index}: {lines!r}", file=sys.stderr)
            with state_condition:
                for mode in INTENSITY_MODES:
                    state["labels"][mode].update(labels.get(mode, {}))
                # independent_labels feeds the IND 1/2 button's 3-line name block, same as the
                # intensity rows -- kept as [line1, line2, line3], not joined into one string.
                state["independent_labels"].update(labels.get("Independent", {}))
                state["last_update"] = time.time()
                state_condition.notify_all()
    # Independents (toggle/bump buttons, not faders)
    elif obj_type == 0x0c:
        entries = sfl.decode_0x0c(data)
        update = {}
        if 1 in entries:
            update["independent1_clicked"] = entries[1][0]
            update["independent1"] = entries[1][1]
        if 2 in entries:
            update["independent2_clicked"] = entries[2][0]
            update["independent2"] = entries[2][1]
        # entries[n][2] is [line1, line2, line3] (same 3-line shape decode_0x09_labels uses)
        # -- kept as-is, not joined, so the UI can show 3 lines like the intensity rows do.
        if 1 in entries and any(entries[1][2]):
            update.setdefault("independent_labels", {})
            update["independent_labels"][1] = entries[1][2]
        if 2 in entries and any(entries[2][2]):
            update.setdefault("independent_labels", {})
            update["independent_labels"][2] = entries[2][2]
        if debug and update:
            print(f"[independents] t={time.time() - _t_start:7.3f}  {update}", file=sys.stderr)
        if update:
            update_state(**{k: v for k, v in update.items() if k not in {"independent_labels"}})
            if "independent_labels" in update:
                with state_condition:
                    state["independent_labels"].update(update["independent_labels"])
                    state["last_update"] = time.time()
                    state_condition.notify_all()
    # Solo/BlackOut/Int Only/Go Mode and the 24 per-fader Bump-LED blocks, all in this payload: the
    # LED colors as the console sent them, and each LED's on/off/blinking read from them.
    elif obj_type == 0x16:
        lights = [sfl.decode_0x16_bump_catch(data, n) for n in range(1, 25)]
        indicator_lights = sfl.decode_0x16_indicator_lights(data)
        indicator_states = {k: sfl.light_state(v) for k, v in indicator_lights.items()}
        update = {k: v for k, v in indicator_states.items() if v is not None}
        if debug and update:
            print(f"[indicators] t={time.time() - _t_start:7.3f}  {update}", file=sys.stderr)
        with state_condition:
            state.update(update)
            state["physical_fader_lights"] = lights
            state["physical_fader_states"] = [sfl.light_state(light) for light in lights]
            state["indicator_lights"] = indicator_lights
            state["last_update"] = time.time()
            state_condition.notify_all()

    # Full intensity table (3x24) for all three intensity sub-modes at once -- see
    # decode_0x0f_all_modes in protocol.py -- so the two dimmed/inactive rows in the UI get
    # real data too, not just whatever they were last set to while active.
    elif obj_type == 0x0f:
        all_modes = sfl.decode_0x0f_all_modes(data)
        if all_modes is not None:
            with state_condition:
                for mode_name, intensities in all_modes.items():
                    state["intensities"][mode_name] = intensities
                state["last_update"] = time.time()
                state_condition.notify_all()
    # Crossfader Live/Next -- their own message, never part of type=0x0e
    elif obj_type == 0x11:
        full = sfl.decode_0x11_full(data)
        if full is not None:
            live, next_ = full
            if debug and (live, next_) != (state["crossfader_live"], state["crossfader_next"]):
                print(f"[crossfader] t={time.time() - _t_start:7.3f}  live={live} next={next_}",
                      file=sys.stderr)
            update_state(crossfader_live=live, crossfader_next=next_)
    # DMX output, both universes -- a full snapshot, resent whenever a patched output changes
    elif obj_type == 0x0d:
        universes = sfl.decode_0x0d_dmx(data)
        if universes is not None:
            if debug:
                old = state["dmx"] or [[0] * 512, [0] * 512]
                changes = [f"U{u + 1}.{a + 1}={universes[u][a]}" for u in range(2) for a in range(512)
                           if universes[u][a] != old[u][a]]
                print(f"[dmx] t={time.time() - _t_start:7.3f}  {' '.join(changes) or 'no change'}",
                      file=sys.stderr)
            update_state(dmx=[universes[0], universes[1]])
            if artnet_sender is not None:
                artnet_sender.update(*universes)
    # The console's two LCDs, 2 x 20 chars each, resent whenever either display changes
    elif obj_type == 0x15:
        lines = sfl.decode_0x15(data)
        if lines is not None:
            if debug and lines != state["lcd"]:
                print(f"[lcd] t={time.time() - _t_start:7.3f}  {lines!r}", file=sys.stderr)
            update_state(lcd=lines)
    # MEMS memory ("Look") names, connect-time only -- see decode_0x00_memory_name in
    # protocol.py for the confirmed record shape (page, slot, both 0-indexed).
    elif obj_type == 0x00:
        result = sfl.decode_0x00_memory_name(data)
        if result is not None:
            page, slot, lines = result
            if debug and any(lines):
                print(f"[mems label] page={page + 1} slot={slot + 1}: {lines!r}", file=sys.stderr)
            with state_condition:
                state["labels"]["MEMS"].setdefault(page + 1, {})[slot + 1] = lines
                state["last_update"] = time.time()
                state_condition.notify_all()
    # Device/Palette-Select selection: not shown in the web UI, only logged under --debug
    elif obj_type == 0x18:
        ids = sfl.decode_0x18(data)
        if debug and ids is not None and ids != _last_logged_selection:
            print(f"[selection] t={time.time() - _t_start:7.3f}  ids={ids}", file=sys.stderr)
            _last_logged_selection = ids
    # Anything without a decoder: silent, except under --debug, where it is worth seeing
    elif debug:
        if obj_type in sfl.KNOWN_UNDECODED_TYPES:
            # Seen in captures but not decoded -- not fader data, not an error.
            label = "known, undecoded"
        else:
            label = "UNEXPECTED -- new, not seen before; worth reporting back"
        print(f"[unhandled] t={time.time() - _t_start:7.3f}  type=0x{obj_type:02x} len={len(data)} "
              f"raw={data[:16].hex()}...  ({label})", file=sys.stderr)


def poll_forever(stop_event):
    """Connect, poll until something goes wrong or the device disappears, then retry."""
    # The libusb bundled with libusb-package if it loads; None makes pyusb look for a system libusb.
    backend = libusb_package.get_libusb1_backend() if libusb_package else None
    if debug:
        lib = libusb_package.get_library_path() if backend is not None else None
        print(f"[consolelink] libusb: {lib or 'system'}", file=sys.stderr)
    waiting_reported = False
    busy_reported = False
    while not stop_event.is_set():
        dev = usb.core.find(idVendor=sfl.VENDOR_ID, idProduct=sfl.PRODUCT_ID, backend=backend)
        found = sfl.find_bulk_interface(dev) if dev is not None else None
        if found is None:
            update_state(connected=False, device=None, console_busy=False)
            busy_reported = False
            if not waiting_reported:
                print("[consolelink] no console found, waiting...", file=sys.stderr)
                waiting_reported = True
            stop_event.wait(2.0)
            continue
        waiting_reported = False

        intf_num, alt, ep_in, ep_out = found

        try:
            if dev.is_kernel_driver_active(intf_num):
                dev.detach_kernel_driver(intf_num)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass
        try:
            usb.util.claim_interface(dev, intf_num)
        except usb.core.USBError as e:
            # Another program (e.g. a second ConsoleLink, or SmartSoft) holds the console: keep
            # trying until it lets go instead of ending this thread.
            update_state(connected=False, device=None, console_busy=True)
            if not busy_reported:
                print(f"[consolelink] console found but can't be claimed ({e}): in use by another "
                      "program? waiting...", file=sys.stderr)
                busy_reported = True
            usb.util.dispose_resources(dev)
            stop_event.wait(2.0)
            continue
        busy_reported = False

        link = sfl.ConsoleLink(dev, ep_in, ep_out)
        update_state(connected=True, device=sfl.MODEL_NAMES.get(dev.idProduct), console_busy=False)
        print(f"[consolelink] connected: {dev.manufacturer!r} {dev.product!r}")

        capture_f = open(capture_path, "a") if capture_path else None
        capture_t0 = time.time()
        if capture_f:
            print(f"[consolelink] raw capture logging to {capture_path}", file=sys.stderr)

        def log_capture(kind, obj_type, data):
            if capture_f:
                capture_f.write(f"t={time.time() - capture_t0:8.3f} {kind:8s} "
                                 f"type=0x{obj_type:02x} len={len(data)} raw={data.hex()}\n")
                capture_f.flush()

        # Ask for the fader MODE before fader/master/bumps state -- type=0x0e is mode-agnostic
        # on the wire, and handle_payload() drops 0x0e data entirely while state["fader_mode"]
        # is still None.
        mode_type, mode_data = link.request_type(0x17)
        if mode_type is not None:
            log_capture("requested", mode_type, mode_data)
            handle_payload(mode_type, mode_data)
        elif debug:
            print("[consolelink] proactive 0x17 request got no reply", file=sys.stderr)

        # Ask for current fader/master/bumps state directly rather than relying on the
        # console's own unprompted announce, which loses a race against macOS's automatic USB
        # HID driver probing almost every time. Sent after the mode request above, so it gets
        # bucketed correctly.
        obj_type, data = link.request_type(0x0e)
        if obj_type is not None:
            log_capture("requested", obj_type, data)
            handle_payload(obj_type, data)
        elif debug:
            print("[consolelink] proactive 0x0e request got no reply", file=sys.stderr)

        # type=0x0f holds all three intensity banks at once, so INT B/INT DEV get real data on
        # connect too, not just whichever mode happens to be active (0x0e is mode-agnostic and
        # structurally can't report a mode that isn't active).
        all_type, all_data = link.request_type(0x0f)
        if all_type is not None:
            log_capture("requested", all_type, all_data)
            handle_payload(all_type, all_data)
        elif debug:
            print("[consolelink] proactive 0x0f request got no reply", file=sys.stderr)

        # Independents, Solo/BlackOut, the LCD text, the crossfaders and the DMX output are
        # fetched directly.
        for type_byte, label in ((0x0c, "0x0c"), (0x16, "0x16"), (0x15, "0x15"), (0x11, "0x11"),
                                 (0x0d, "0x0d")):
            obj_type, data = link.request_type(type_byte)
            if obj_type is not None:
                log_capture("requested", obj_type, data)
                handle_payload(obj_type, data)
            elif debug:
                print(f"[consolelink] proactive {label} request got no reply", file=sys.stderr)

        held = HeldButtons(link)
        consecutive_write_fails = 0
        needs_reset = False
        good_headers = 0
        last_heartbeat = time.time()
        plain_poll_started = time.time()
        gui_requests_sent = False
        while not stop_event.is_set():
            if debug and time.time() - last_heartbeat >= 10.0:
                # Distinguishes "still polling fine, nothing new" from a genuine stall -- if
                # this stops appearing, the loop is stuck above, likely in a read/write that
                # isn't timing out (the announce/ack path has its own DROPPED logging instead).
                print(f"[poll] t={time.time() - _t_start:7.3f}  alive, "
                      f"good_headers={good_headers} write_fails={link.write_errors} "
                      f"read_timeouts={link.read_timeouts}", file=sys.stderr)
                last_heartbeat = time.time()

            while True:
                try:
                    kind, arg = button_queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "tap":
                    link.press_button(arg)
                elif kind == "press":
                    held.press(arg)
                elif kind == "keepalive":
                    held.keepalive(arg)
                elif kind == "release":
                    held.release(arg)
                elif kind == "page":
                    link.send_mems_page(arg)
            with fader_writes_lock:
                pending_faders = list(fader_writes.items())
                fader_writes.clear()
            for fader, value in pending_faders:
                link.send_fader(fader, value)
            held.service()

            if not link.write_header(0, 0, (0, 0, 0, 0)):
                consecutive_write_fails += 1
                if consecutive_write_fails > 20:
                    # Repeated OUT-pipe write failures (as opposed to a device that's simply
                    # gone) usually mean a previous session left the bulk pipe stalled -- a
                    # plain re-claim doesn't clear that, but a USB port reset does, without
                    # needing a physical unplug/replug.
                    print("[consolelink] too many write failures, resetting device", file=sys.stderr)
                    needs_reset = True
                    break
                time.sleep(0.05)
                continue
            consecutive_write_fails = 0

            reply = link.read_header()
            if reply is None:
                continue
            good_headers += 1
            if not gui_requests_sent and time.time() - plain_poll_started >= 2.0:
                # Request the full catalog after initial idle polling so the console announces
                # all 74 type=0x09 name pages without suppressing the startup catch-up response.
                link.send_gui_request(1, 0x07)
                gui_requests_sent = True
            _, payload_len, _ = reply
            if payload_len == 0:
                continue
            raw = link.read_payload(payload_len)
            if raw is None:
                continue
            _, obj_type, data = link.decode_in_payload(raw)
            if obj_type is None:
                continue

            if obj_type == 0x28:
                log_capture("announce", obj_type, data)
                announced_entries = sfl.decode_announce(data)
                for announced_type, selector in announced_entries:
                    if stop_event.is_set():
                        break  # the full catalogue is ~74 entries, seconds of USB round trips
                    # NOTE: every `continue` below silently drops this announced update with
                    # NO retry. Never observed firing in practice, but if a future "misses
                    # the last state" report comes back, enable debug and one of these lines
                    # should show up right when it happens.
                    ack_state = (announced_type, selector[0], selector[1], 0)
                    if not link.write_header(2, 0, ack_state):
                        if debug:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: ack write failed", file=sys.stderr)
                        continue
                    if not link.write_header(0, 0, ack_state):
                        if debug:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: follow-up poll write failed", file=sys.stderr)
                        continue
                    ack_reply = link.read_header()
                    if ack_reply is None:
                        if debug:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: ack header read timed out", file=sys.stderr)
                        continue
                    _, ack_payload_len, _ = ack_reply
                    if ack_payload_len == 0:
                        if debug:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: ack reply had payloadLen=0", file=sys.stderr)
                        continue
                    ack_raw = link.read_payload(ack_payload_len)
                    if ack_raw is None:
                        if debug:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: payload read timed out", file=sys.stderr)
                        continue
                    _, real_type, real_data = link.decode_in_payload(ack_raw)
                    if real_type is not None:
                        log_capture("acked", real_type, real_data)
                        handle_payload(real_type, real_data)
            else:
                log_capture("payload", obj_type, data)
                handle_payload(obj_type, data)

        held.release_all()
        update_state(connected=False, device=None)
        if capture_f:
            capture_f.close()
        try:
            usb.util.release_interface(dev, intf_num)
        except usb.core.USBError:
            pass
        if needs_reset:
            try:
                dev.reset()
                print("[consolelink] device reset; waiting for re-enumeration", file=sys.stderr)
                stop_event.wait(2.0)  # give the device time to fully come back before retrying
            except usb.core.USBError as e:
                print(f"[consolelink] reset failed: {e} -- may need a physical unplug/replug",
                      file=sys.stderr)
        usb.util.dispose_resources(dev)
        stop_event.wait(1.0)


def bridge_console_changed(identity):
    """smartsoft_bridge's on_console: identity is (vid, pid, bcd, product), or None when lost."""
    update_state(connected=identity is not None,
                 device=sfl.MODEL_NAMES.get(identity[1]) if identity else None, console_busy=False)


def bridge_forever(stop_event, bridge):
    """--smartsoft-bridge: relay SmartSoft (under CrossOver) to the console instead of polling it
    (see smartsoft_bridge.py). The console's messages on the way through update the page and
    Art-Net like polled ones; --capture also logs what SmartSoft sends, as "sent" lines."""
    capture_f = open(capture_path, "a") if capture_path else None
    capture_t0 = time.time()
    if capture_f:
        print(f"[consolelink] raw capture logging to {capture_path}", file=sys.stderr)

    def on_message(kind, obj_type, data):
        if capture_f:
            capture_f.write(f"t={time.time() - capture_t0:8.3f} {kind:8s} "
                            f"type=0x{obj_type:02x} len={len(data)} raw={data.hex()}\n")
            capture_f.flush()
        if kind == "payload":
            handle_payload(obj_type, data)
        elif kind == "sent" and debug:
            print(f"[smartsoft] t={time.time() - _t_start:7.3f}  sent type=0x{obj_type:02x} "
                  f"len={len(data)} raw={data[:16].hex()}", file=sys.stderr)

    bridge.on_message = on_message
    try:
        bridge.serve_forever(stop_event)
    finally:
        update_state(connected=False, device=None)
        if capture_f:
            capture_f.close()


def _int_in_range(text, lo, hi):
    """int(text) if it is a plain decimal in lo..hi, else None."""
    return int(text) if text.isdecimal() and lo <= int(text) <= hi else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout to the USB connect/disconnect prints above

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html")
        elif self.path == "/style.css":
            self._serve_file("style.css", "text/css")
        elif self.path == "/app.js":
            self._serve_file("app.js", "text/javascript")
        elif self.path == "/favicon.svg":
            self._serve_file("favicon.svg", "image/svg+xml")
        elif self.path == "/api/state":
            # One-shot snapshot, used only for the initial page load before the
            # SSE stream below takes over. Not used for the live updates.
            with state_lock:
                body = json.dumps(state).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/events":
            self._serve_events()
        else:
            self.send_response(404)
            self.end_headers()

    def _from_own_page(self):
        """With no password, a write must come from the page served by this machine: a Host
        header naming localhost (blocks DNS rebinding) and, when the browser sends one, an Origin
        on localhost too (blocks any other website's script from POSTing to localhost:<port>)."""
        def is_local(value):
            host = urlsplit(value if "//" in value else "//" + value).hostname
            return host in ("localhost", "127.0.0.1", "::1")
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return is_local(host) and (origin is None or is_local(origin))

    def do_POST(self):
        """POST /api/button/<name> (tap), /api/bump/<1-24>/press|keepalive|release (hold),
        /api/fader/<1-24|master|bumps|live|next>/<0-255>, /api/mems_page/<1-12> and
        /api/auth (password check only). All need --allow-write and, if it was given a password,
        ?pw=<password> (otherwise the request must come from this machine's own page); all but
        /api/auth also need a connected console."""
        url = urlsplit(self.path)
        event = ("auth",) if url.path == "/api/auth" else self._parse_write_request(url.path)
        if event is None:
            self.send_response(404)
            self.end_headers()
            return
        if not write_enabled:
            self.send_response(403)
            self.end_headers()
            return
        if write_password:
            given = parse_qs(url.query).get("pw", [""])[0]
            if not hmac.compare_digest(given.encode(), write_password.encode()):
                time.sleep(0.5)  # slows down password guessing
                self.send_response(401)
                self.end_headers()
                return
        elif not self._from_own_page():
            self.send_response(403)
            self.end_headers()
            return
        if event[0] == "auth":
            self.send_response(204)
            self.end_headers()
            return
        with state_lock:
            connected = state["connected"]
        if not connected:
            self.send_response(503)
            self.end_headers()
            return
        if event[0] == "fader":
            with fader_writes_lock:
                fader_writes[event[1]] = event[2]
        else:
            button_queue.put(event)
        self.send_response(204)
        self.end_headers()

    def _parse_write_request(self, path):
        """The queue event for `path` (no query), or None if it isn't a valid write request."""
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "button"] and parts[2] in BUTTONS:
            return ("tap", BUTTONS[parts[2]])
        if (len(parts) == 4 and parts[:2] == ["api", "bump"]
                and parts[3] in ("press", "keepalive", "release")):
            fader = _int_in_range(parts[2], 1, sfl.BUMP_COUNT)
            if fader is not None:
                return (parts[3], sfl.bump_code(fader))
        if len(parts) == 4 and parts[:2] == ["api", "fader"]:
            fader = {"master": sfl.FADER_MASTER, "bumps": sfl.FADER_BUMPS,
                     "live": sfl.FADER_LIVE, "next": sfl.FADER_NEXT}.get(
                parts[2], _int_in_range(parts[2], 1, sfl.FADER_COUNT))
            value = _int_in_range(parts[3], 0, 255)
            if fader is not None and value is not None:
                return ("fader", fader, value)
        if len(parts) == 3 and parts[:2] == ["api", "mems_page"]:
            page = _int_in_range(parts[2], 1, sfl.MEMS_PAGE_COUNT)
            if page is not None:
                return ("page", page)
        return None

    def _serve_events(self):
        """Server-Sent Events: push the current state every time it changes (or at
        least once a second as a heartbeat), so the client never has to poll and
        never silently skips an update that arrived between polls."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                with state_condition:
                    state_condition.wait(timeout=1.0)
                    payload = json.dumps(state)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client navigated away/closed the tab

    def _serve_file(self, name, content_type):
        try:
            body = (resources.files("consolelink") / "static" / name).read_bytes()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        # Without this, iOS Safari can keep serving a stale index.html/app.js after an upgrade.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """`handle_error` is a SERVER hook, not a request-handler one -- overriding it on Handler
    above would silently never fire. Suppresses socketserver's default full-traceback print for
    a closed/reset connection (routine for a long-lived SSE stream, e.g. a browser tab closing
    mid-request) -- not a crash, just noisy; the server and every other connection keep running."""

    # HTTPServer sets SO_REUSEADDR, which on Windows lets a second server bind a port already in
    # use instead of failing; Windows can rebind a TIME_WAIT port without it anyway.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        super().handle_error(request, client_address)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Local web app showing live console state (physical faders + per-mode intensities).")
    parser.add_argument("--debug", action="store_true",
                         help="Per-control change logging to stderr, plus a liveness "
                              "heartbeat, handshake diagnostics, and a line for every message "
                              "type without a decoder.")
    parser.add_argument("--capture", metavar="PATH", default="",
                         help="Append every message (decoded or not) as a timestamped hex "
                              "line to PATH, for comparing against a packet capture when "
                              "something behaves unexpectedly.")
    parser.add_argument("--no-web", action="store_true",
                         help="Don't start the web server; just poll the console (use with "
                              "--debug, --capture and/or --artnet).")
    parser.add_argument("--listen", choices=sorted(LISTEN_HOSTS), default=None,
                         help="Who can open the web page: 'localhost' (default) only this "
                              "machine, 'network' also other devices on the same network.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="N",
                         help=f"Port of the web page (default {DEFAULT_PORT}).")
    parser.add_argument("--allow-write", metavar="PASSWORD", nargs="?", const="", default=None,
                         help="Let the web page press console buttons (BlackOut, Solo, Ind 1/2, "
                              "the Bump buttons), pick the MEMS page and move the faders. Off by "
                              "default. With --listen network a PASSWORD is required (the page "
                              "asks for it in its settings); with --listen localhost it is "
                              "optional. The password travels in the URL over plain HTTP, so it "
                              "keeps casual users out, nothing more: use it on a network you "
                              "trust.")
    parser.add_argument("--smartsoft-bridge", metavar="PORT", type=int, nargs="?",
                         const=smartsoft_bridge.DEFAULT_PORT, default=None,
                         help="Let ETC SmartSoft for Windows, run under CrossOver on this Mac, "
                              "drive the console: ConsoleLink relays it over localhost (default "
                              f"port {smartsoft_bridge.DEFAULT_PORT}) instead of polling the "
                              "console itself. Needs the bridge DLL installed in the CrossOver "
                              "bottle. The page and Art-Net keep mirroring the console, "
                              "read-only.")
    artnet.add_arguments(parser)
    return parser


@dataclasses.dataclass
class Settings:
    """Everything that configures a running ConsoleLink, whether it comes from the command line
    (Settings.from_args) or from a GUI wrapper's own preferences."""
    web: bool = True  # False: no web server, just poll the console (--no-web)
    listen: str = "localhost"  # key of LISTEN_HOSTS
    port: int = DEFAULT_PORT
    allow_write: bool = False
    password: str = ""  # "" = none (only valid with listen="localhost")
    artnet_dest: str | None = None  # destination (IP or "broadcast"), None = off
    artnet_universe: int = 0
    artnet_rate: float = artnet.DEFAULT_RATE_HZ
    artnet_keepalive: float = artnet.DEFAULT_KEEPALIVE_S
    debug: bool = False
    capture: str = ""
    smartsoft_bridge: int | None = None  # port to relay SmartSoft on (--smartsoft-bridge), None = off

    @classmethod
    def from_args(cls, args):
        """From the parsed command line (after check_args has resolved the --listen default)."""
        return cls(web=not args.no_web, listen=args.listen, port=args.port, allow_write=args.allow_write is not None,
                   password=args.allow_write or "", artnet_dest=args.artnet,
                   artnet_universe=args.artnet_universe, artnet_rate=args.artnet_rate,
                   artnet_keepalive=args.artnet_keepalive, debug=args.debug, capture=args.capture,
                   smartsoft_bridge=args.smartsoft_bridge)

    def validate(self):
        """Raise ValueError (message ready to show to the user) for a setting that makes no sense."""
        if self.listen not in LISTEN_HOSTS:
            raise ValueError(f"listen must be one of {', '.join(sorted(LISTEN_HOSTS))}")
        if not 0 <= self.port <= 65535:
            raise ValueError("the port must be between 0 and 65535")
        if self.allow_write and not self.web:
            raise ValueError("control from the page needs the web page")
        if self.allow_write and self.listen == "network" and not self.password:
            raise ValueError("control from other devices on the network needs a password: they "
                             "could otherwise control the console")
        if self.smartsoft_bridge is not None:
            if not 0 <= self.smartsoft_bridge <= 65535:
                raise ValueError("the SmartSoft bridge port must be between 0 and 65535")
            if self.allow_write:
                raise ValueError("control from the page isn't available with the SmartSoft "
                                 "bridge: SmartSoft drives the console")


class BridgePortError(OSError):
    """Service.start() couldn't bind the SmartSoft bridge port (as opposed to the web port)."""


class Service:
    """The running ConsoleLink: USB poll thread, optional web server and Art-Net sender.

    The state lives in this module's globals (one console, one process), so only one Service at
    a time. start() raises ValueError for bad settings and OSError if the port is taken, without
    leaving anything running; stop() releases the USB interface, so a new Service can follow."""

    def __init__(self, settings):
        self.settings = settings
        self._stop_event = threading.Event()
        self._poll_thread = None
        self._server = None
        self._server_thread = None
        self._artnet = None
        self._bridge = None

    @property
    def port(self):
        """The port actually bound (differs from settings.port when that is 0), or None."""
        return self._server.server_port if self._server else None

    @property
    def bridge_port(self):
        """The SmartSoft bridge's bound port, or None when it is off."""
        return self._bridge.port if self._bridge else None

    def start(self):
        global debug, capture_path, artnet_sender, write_enabled, write_password
        s = self.settings
        s.validate()
        if s.artnet_dest is not None:
            self._artnet = artnet.start_sender(s.artnet_dest, s.artnet_universe, s.artnet_rate,
                                               s.artnet_keepalive)
        try:
            if s.web:
                self._server = QuietThreadingHTTPServer((LISTEN_HOSTS[s.listen], s.port), Handler)
            if s.smartsoft_bridge is not None:
                backend = libusb_package.get_libusb1_backend() if libusb_package else None
                try:
                    self._bridge = smartsoft_bridge.BridgeServer(
                        s.smartsoft_bridge, usb_backend=backend, on_console=bridge_console_changed)
                except OSError as e:
                    raise BridgePortError(e.errno, e.strerror) from e
        except OSError:
            self.stop()
            raise
        debug = s.debug
        capture_path = s.capture
        artnet_sender = self._artnet
        write_enabled = s.allow_write
        write_password = s.password
        while not button_queue.empty():  # leftovers from a previous run
            button_queue.get_nowait()
        with fader_writes_lock:
            fader_writes.clear()
        update_state(connected=False, device=None, write_enabled=write_enabled,
                     write_password_required=bool(write_password))
        # Not a daemon thread: those are hard-killed at interpreter exit with no chance to run
        # their `finally` cleanup, which would leave the USB interface claimed/pipes stalled for
        # the next run. stop() joins it, so release_interface() runs on a normal Ctrl+C.
        if self._bridge:
            self._poll_thread = threading.Thread(target=bridge_forever,
                                                 args=(self._stop_event, self._bridge))
        else:
            self._poll_thread = threading.Thread(target=poll_forever, args=(self._stop_event,))
        self._poll_thread.start()
        if self._server:
            self._server_thread = threading.Thread(target=self._server.serve_forever,
                                                   name="http", daemon=True)
            self._server_thread.start()
        return self

    def wait(self, timeout):
        """Block up to `timeout` s; True while the poll thread is still running."""
        self._poll_thread.join(timeout=timeout)
        return self._poll_thread.is_alive()

    def stop(self):
        global artnet_sender
        self._stop_event.set()
        if self._server is not None:
            if self._server_thread is not None:
                self._server.shutdown()  # blocks until serve_forever returns
            self._server.server_close()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5.0)
            if self._poll_thread.is_alive():
                print("[consolelink] the console is still busy after 5 s; it may need a USB "
                      "reset on the next start", file=sys.stderr)
        elif self._bridge is not None:
            self._bridge.close()  # bound, but start() failed before serving
        if self._artnet is not None:
            artnet_sender = None
            self._artnet.stop()
        self._server = self._server_thread = self._poll_thread = self._artnet = None
        self._bridge = None


def check_args(parser, args):
    """Reject option combinations that make no sense (parser.error exits), and resolve the
    --listen default ("localhost")."""
    if args.no_web and not (args.debug or args.capture or args.artnet is not None
                            or args.smartsoft_bridge is not None):
        parser.error("--no-web needs at least one of --debug, --capture, --artnet, "
                     "--smartsoft-bridge (otherwise there is nothing to do)")
    if args.allow_write is not None and args.no_web:
        parser.error("--allow-write needs the web page (drop --no-web)")
    if args.listen and args.no_web:
        parser.error("--listen needs the web page (drop --no-web)")
    args.listen = args.listen or "localhost"
    try:
        Settings.from_args(args).validate()
    except ValueError as e:
        parser.error(str(e))


def main():
    parser = build_parser()
    args = parser.parse_args()
    check_args(parser, args)
    settings = Settings.from_args(args)
    print(f"ConsoleLink v{sfl.VERSION}")
    try:
        service = Service(settings).start()
    except ValueError as e:
        parser.error(str(e))
    except BridgePortError as e:
        sys.exit(f"Can't listen for SmartSoft on port {settings.smartsoft_bridge}: {e}")
    except OSError as e:
        sys.exit(f"Can't listen on port {settings.port}: {e}")
    try:
        if service.bridge_port is not None:
            print(f"SmartSoft bridge on 127.0.0.1:{service.bridge_port}: start SmartSoft in "
                  "CrossOver.")
        if settings.web:
            print(f"Serving on http://{LISTEN_HOSTS[settings.listen]}:{service.port} "
                  "-- Ctrl+C to stop.")
        elif service.bridge_port is not None:
            print("No web server -- Ctrl+C to stop.")
        else:
            print("Polling the console, no web server -- Ctrl+C to stop.")
        while service.wait(0.5):  # a timeout keeps Ctrl+C responsive
            pass
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        service.stop()


if __name__ == "__main__":
    main()
