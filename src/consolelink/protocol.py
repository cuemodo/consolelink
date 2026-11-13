#!/usr/bin/env python3
"""
Shared low-level protocol library for talking to the console over USB, reverse-engineered from
captured SmartSoft <-> console traffic. Used by app.py (the web app and terminal logger),
which doesn't re-derive any of this, it just imports it.

Opens the console over USB, claims the bulk vendor interface (interface 1), and speaks the
wire protocol reverse-engineered from captured SmartSoft <-> console traffic:

  - Idle poll: a 12-byte header, 6x uint16 LE (msgType, payloadLen, state[4]),
    sent OUT with msgType=0, payloadLen=0, state=[0,0,0,0]. NOT msgType=2 --
    a client that only sends msgType=2 gets zero replies.
  - The console replies with a 12-byte header too. If payloadLen>0, a second
    read on the same endpoint fetches exactly that many payload bytes.
  - When a control's value changes, the console first sends an "announce"
    (payload type=0x28) listing which object type(s) are ready (e.g.
    type=0x0e for faders/Bumps/Master, type=0x11 for the crossfaders). The
    host must ACK by sending one OUT header with msgType=2 and
    state[0]=<the announced type value>, followed by a plain msgType=0 poll
    -- only then does the console reply with the real data (payload type
    matching what was announced, wrapped in an IN header with msgType=3).
    This ack step is NOT optional -- skipping it means the announce repeats
    forever and the real value is never delivered.
  - `ConsoleLink.request_type()` gets the same data on demand instead of
    waiting for an announce -- works for every type both apps request at
    connect (0x0e/0x17/0x0f/0x0c/0x16).

Decodes:
  - type=0x0e (99 bytes): Fader 1-24, Bumps, Master (confirmed against real
    hardware across three separate captures). Mode-agnostic -- reports
    whatever's on a fader regardless of which fader mode is active: a
    fader's raw value means different things (an INT A level, an INT B
    level, a device intensity, ...) depending on type=0x17 below. The
    fader values are output levels, not lever positions: level x Crossfader
    Live (type=0x11) x Master, e.g. faders at full with Live and Master
    both at 60% read 36% (92) -- matching the devices' actual output.
  - type=0x0f (75 bytes): the stored intensity table -- unlike 0x0e (which
    only ever reports whichever fader mode is physically active), this holds
    all three intensity sub-mode banks (INT A/INT B/INT DEV) at once --
    `[header:1][INT A: 24][INT B: 24][INT DEV: 24]`.
  - type=0x0d (1026 bytes): the console's DMX output, both universes -- a 2-byte
    big-endian channel count (1024) then one level byte per channel. Pushed on
    every change to the output (never a timed stream); see decode_0x0d_dmx.
  - type=0x11 (209 bytes): Crossfader Live, Crossfader Next -- the two scene
    levels, not lever positions: once a crossfade completes the console
    resets them to Live=255, Next=0 wherever the levers physically are.
  - type=0x15 (81 bytes): the console's two physical LCDs, verbatim ASCII (plus
    8 bar-graph glyphs, see decode_0x15) -- 1 marker byte + 80 characters
    (2 displays x 2 lines x 20 chars). This is
    also currently the only way to see the 3 wheels' live values -- they
    don't touch type=0x0e/0x11 at all, only this LCD text and a
    correlated-but-undecoded type=0x16.
  - type=0x16 (637 bytes, mostly undecoded): the console's LEDs, each a 6-byte
    block of two RGB triples (the two blink phases). Bytes `1+6*(N-1)` onward hold
    fader N's Bump LED -- green in INT/PARAM modes, red in MEMS; see
    decode_0x16_bump_catch. Offsets 517 / 523 are Solo / BlackOut, and 373 / 361 the
    MEMS-only Int Only / Go Mode (decode_0x16_indicator_lights). light_state() reads
    on/off/blinking from the colors: idle is at or below LED_IDLE_MAX, a block whose
    halves differ blinks, anything brighter is on.
  - type=0x17 (7 bytes): the fader-mode selector, all six modes (INT A/INT
    B/INT DEV/PARAM 1/PARAM 2/MEMS) -- only fires unprompted on a mode
    CHANGE, but request_type() gets the true current mode on demand
    regardless.
  - type=0x18 (variable): the Device/Palette-Select row's resulting
    selection state as `[count][ids...]`, not a raw button code.
  - type=0x0c (85 bytes): Independent 1/2 name + on/off state.
  - Anything else: not decoded here (most buttons, wheels' raw deltas,
    curves, names/groups/cues).

Writes (host -> console; app.py only does them with --allow-write): an OUT msgType=1 header with
payloadLen = 4 + objLen, then a payload `[seq:1][objLen:2 LE][type:1][data]`; seq is a per-link
counter that continues from 2 (0 and 1 are the connect-time type=0x27 requests).
  - type=0x14, data `[1][code][pressed]`: a button press (send_button/press_button): BlackOut,
    Solo, Ind 1/2, the six fader-mode buttons, Int Only, Go Mode (a toggle flips on the press) and
    the 24 Bump buttons (held: the channel stays bumped while the button is down).
  - type=0x14, data `[0][id][value][prev]`: a fader move (send_fader). id is fader-1 for faders
    1-24, then 24 Master, 25 Bumps, 26 Crossfader Live, 27 Crossfader Next; prev is the last
    value sent for that id (0 for the first).
  - type=0x27, subtype 0x0a: pick the MEMS page (send_mems_page); other type=0x27 subtypes are
    the connect-time GUI requests (send_gui_request).
"""

import struct
import sys
import time

import usb.core
import usb.util

VERSION = "0.2.0"  # ConsoleLink release version (SemVer); change it with `bump-my-version bump`, not by hand

VENDOR_ID = 0x14D5
PRODUCT_ID = 0x0201
MODEL_NAMES = {PRODUCT_ID: "SmartFade ML"}  # USB product id -> console model, for the UI

HEADER_LEN = 12
IO_TIMEOUT_MS = 200

# Message types confirmed to exist on the wire (from pcap captures and/or live sessions) but
# not yet decoded -- not fader/crossfader data, safe to ignore for fader testing.
# 0x00: constant ~195-byte payload, unrelated to any specific control (seen live, not yet in
#       any pcap capture) -- possibly a periodic status/heartbeat block.
# 0x10: 1026 bytes, same 2-byte big-endian channel-count header (0x0400 = 1024) as type=0x0d,
#       but the 1024 entries are small flag-like values (0, 1, 3, 5, 0x0f), not levels. Not
#       streamed and not tied to output changes -- seen only alongside selection/mode changes.
#       Probably per-channel status; not decoded. (type=0x0d, the DMX output, is decoded.)
# 0x16: ~637-642 byte full-table dump, correlated with wheel moves and occasional full
#       refreshes; likely a live RGB-ish color-preview value, mostly not decoded -- type=0x15
#       already gives an exact, plain-text readout of whatever a wheel is adjusting.
#       EXCEPTIONS: the console's LEDs are 6-byte blocks (two RGB triples, the LED's two blink
#       phases): the Solo/BlackOut/Int Only/Go Mode blocks (decode_0x16_indicator_lights) and each
#       fader's Bump LED (decode_0x16_bump_catch) at `1+6*(N-1)`; light_state() turns any of them
#       into on/off/blinking.
KNOWN_UNDECODED_TYPES = {0x00, 0x10}


def find_bulk_interface(dev):
    """Find the (interface_number, alt_setting, ep_in, ep_out) with two bulk endpoints."""
    for cfg in dev:
        for intf in cfg:
            ep_in = ep_out = None
            for ep in intf:
                xfer_type = usb.util.endpoint_type(ep.bmAttributes)
                if xfer_type != usb.util.ENDPOINT_TYPE_BULK:
                    continue
                if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                    ep_in = ep
                else:
                    ep_out = ep
            if ep_in is not None and ep_out is not None:
                return intf.bInterfaceNumber, intf.bAlternateSetting, ep_in, ep_out
    return None


# Console button codes, for ConsoleLink.press_button(). A virtual press is what SmartSoft sends when
# its Live tab is clicked: the console toggles the function exactly as if its own button were
# pressed. Confirmed on the console for BlackOut (a press toggles it and the change comes back as a
# type=0x16 BlackOut indicator update); Solo, Ind 1/2 and the others below work the same way.
BUTTON_SOLO = 0x56
BUTTON_BLACKOUT = 0x57
BUTTON_IND1 = 0x5F
BUTTON_IND2 = 0x60
# MEMS-only toggles, SmartSoft's "FADERS: INT ONLY" and "BUMPS: GO MODE": a press flips each, like
# Solo/BlackOut. Their console LEDs are type=0x16 blocks (blue = on): GO MODE at offset 361, INT
# MODE at 373. In any other fader mode both LEDs read off; back in MEMS the console shows its
# remembered state again.
BUTTON_INT_ONLY = 0x3E
BUTTON_GO_MODE = 0x3C
BUTTON_RELEASE_DELAY = 0.12  # SmartSoft sends the release 0.12-0.17 s after the press
# Fader-mode buttons. Edge-triggered like the toggles: the console switches mode on the press
# and ignores the release; the mode comes back as type=0x17 about 0.1 s later.
BUTTON_MODE_INT_A = 0x41
BUTTON_MODE_INT_B = 0x42
BUTTON_MODE_INT_DEV = 0x40
BUTTON_MODE_PARAM_1 = 0x43
BUTTON_MODE_PARAM_2 = 0x44
BUTTON_MODE_MEMS = 0x3F
# A fader's Bump button is a positional code, the same in every fader mode: fader N (1-24) is
# code N-1. In INT A/B/DEV it is momentary -- the channel sits at the Bumps master's level for as
# long as the button is held and reverts on release -- and the console ignores a press shorter
# than about 0.15 s (taps of 78-109 ms did nothing; 0.235 s worked). In MEMS the press fires the
# memory (the fader sets its level) and holding does nothing extra.
BUMP_COUNT = 24
# A virtual fader move is a type=0x14 write with data [kind=0][fader-1][value][previous value]:
# SmartSoft sends fader 1 as 00 00 vv pp and fader 24 as 00 17 vv pp, one message about every
# 0.09 s while dragging, each pp being the vv it sent last for that fader (0 for the first move
# of a session). The console's type=0x0e readback then reports the written value.
FADER_COUNT = 24
# Wire ids 24-27 are the console's four other analog controls. Probed on the console (a write
# to each id moved this control): 24 Master, 25 Bumps master, 26 Crossfader Live, 27 Crossfader
# Next. send_fader() numbers them like faders 25-28. Live/Next are scene levels that renormalize
# after a completed crossfade (the previous-value byte is then stale, which isn't known to matter).
FADER_MASTER = 25
FADER_BUMPS = 26
FADER_LIVE = 27
FADER_NEXT = 28
CONTROL_COUNT = 28
# SmartSoft's MEMS page select is a type=0x27 GUI request, subtype 0x0a, [page-1][0]; the
# console answers with type=0x17 (data[2] = the 0-based page), then type=0x0e.
MEMS_PAGE_COUNT = 12
GUI_SUBTYPE_MEMS_PAGE = 0x0A


def bump_code(fader):
    """Button code of fader `fader`'s (1-24) Bump button."""
    if not 1 <= fader <= BUMP_COUNT:
        raise ValueError(f"fader must be 1-{BUMP_COUNT}, got {fader}")
    return fader - 1


def pack_header(msg_type, payload_len, state=(0, 0, 0, 0)):
    return struct.pack("<HHHHHH", msg_type, payload_len, *state)


def decode_announce(data):
    """type=0x28 payload: [0x00][count][type,0,0,0,0]*count -- returns list of announced types and selectors.

    Catalog announcements use the four bytes after the type as an item selector, echoed back
    as state[1]/state[2] of the ack header. For type 0x09 the first field is the name-table
    page number; for type 0x00 (MEMS memory names) it's (page, slot), both 0-indexed.

    The two fields have DIFFERENT byte orders on the wire: the first is big-endian, the
    second little-endian. Confirmed for the second field by 0x00's page-3/slot-5 memory,
    announced as `02 04 00` after the page's high byte, and acked by real SmartSoft as
    state[2]=4 (slots 1-23 acked the same way) -- reading it big-endian gave 1024, so the
    console answered every slot with slot 0's record.
    """
    if len(data) < 2:
        return []
    count = data[1]
    entries = []
    for i in range(count):
        off = 2 + 5 * i
        if off + 5 <= len(data):
            selector = (struct.unpack_from(">H", data, off + 1)[0],
                        struct.unpack_from("<H", data, off + 3)[0])
            entries.append((data[off], selector))
    return entries

def decode_0x0e(data):
    """type=0x0e (99 bytes): Fader 1-24 (duplicated pair), Bumps=data[97], Master=data[98]."""
    if len(data) < 99:
        return {}
    values = {}
    for n in range(1, 25):
        off = 1 + 4 * (n - 1)
        if data[off] or data[off + 1]:
            values[f"Fader{n}"] = data[off]
    if data[97]:
        values["Bumps"] = data[97]
    if data[98]:
        values["Master"] = data[98]
    return values

def decode_0x0e_full(data):
    """Full snapshot decode of type=0x0e: ALL 24 fader values (zero included) + bumps + master.

    decode_0x0e() above omits zero entries, which suits a terminal printer that only shows
    what changed. But the message is a full snapshot every time, not a delta -- a stateful
    client needs all 26 values back so it can replace its whole state each message, rather than
    merging present keys and leaving a fader that moved to 0 stuck at its last nonzero value.
    """
    if len(data) < 99:
        return None
    faders = [data[1 + 4 * n] for n in range(24)]
    return faders, data[97], data[98]

def decode_0x11(data):
    """type=0x11 (209 bytes): CrossfaderLive=data[195], CrossfaderNext=data[196]."""
    if len(data) < 197:
        return {}
    values = {}
    if data[195]:
        values["CrossfaderLive"] = data[195]
    if data[196]:
        values["CrossfaderNext"] = data[196]
    return values

def decode_0x11_full(data):
    """Full snapshot decode of type=0x11: (live, next), zero included -- same reason as
    decode_0x0e_full: every message carries both levels, so a stateful client replaces both.
    """
    if len(data) < 197:
        return None
    return data[195], data[196]


# The console's LCDs have 8 custom glyphs, sent as bytes 0x80-0x87. The only screen seen using
# them is the INT-mode one ("IntA: 1" / "13", per the manual "the current output of the selected
# fader mode, with bar graphs"): one cell per fader, faders 1-12 in line 1 and 13-24 in line 2,
# columns 8-19. A fader brought down to 0 steps 0x85, 0x84, ... 0x80 and then shows a space, so
# 0x80 + k reads as a bar k+1 eighths tall -- drawn here with the matching Unicode block elements.
LCD_BAR_GLYPHS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"  # 1/8 .. 8/8 bars


def _lcd_char(b):
    if 0x20 <= b < 0x7f:
        return chr(b)
    if 0x80 <= b <= 0x87:
        return LCD_BAR_GLYPHS[b - 0x80]
    return "\ufffd"


def decode_0x15(data):
    """type=0x15 (81 bytes): [marker byte] + 80 chars = both LCDs, 2 lines x 20 chars each.

    Returns [LCD 1 line 1, LCD 1 line 2, LCD 2 line 1, LCD 2 line 2]. Plain ASCII, except the
    bar-graph glyphs (see LCD_BAR_GLYPHS); any other non-ASCII byte becomes U+FFFD."""
    if len(data) < 81:
        return None
    text = "".join(_lcd_char(b) for b in data[1:81])
    lines = [text[i:i + 20] for i in range(0, 80, 20)]
    return lines


INTENSITY_SUBMODE_NAMES = {0: "INT A", 1: "INT B", 2: "INT DEV"}


def decode_0x17(data):
    """type=0x17 (7 bytes): the fader-mode selector. Only fires unprompted on a mode change;
    use request_type() for the current mode on demand.

    Two-field encoding:
      data[0]: family -- 0 = intensity (INT A/B/INT DEV), 1 = MEMS,
               2 = PARAM (device parameters)
      data[4]: sub-mode within the intensity family (0/1/2 = INT A/INT B/
               INT DEV) -- stale/meaningless once data[0] != 0
      data[3]: sub-mode within the PARAM family (0 = PARAM 1, 1 = PARAM 2)
               -- stale/meaningless once data[0] != 2
      data[2]: the currently selected MEMS page (see decode_0x17_mems_page below) --
               stale/meaningless once data[0] != 1
    """
    if len(data) < 5:
        return None
    family = data[0]
    if family == 0:
        return INTENSITY_SUBMODE_NAMES.get(data[4], f"unknown intensity submode({data[4]})")
    if family == 1:
        return "MEMS"
    if family == 2:
        return "PARAM 1" if data[3] == 0 else "PARAM 2"
    return f"unknown family({family})"


def decode_0x17_mems_page(data):
    """type=0x17, MEMS family only (data[0] == 1): the currently selected memory page,
    1-indexed to match the console's own LCD text ("Memory page:1" / "Bump 1-12 to change",
    type=0x15, shown only while the MEMS button is physically held down). `data[2]` is the
    0-indexed page number, and reflects the console's persisted current page, not just an
    ephemeral "while held" value.
    Meaningless/stale outside the MEMS family -- callers should only read this when
    decode_0x17(data) == "MEMS".

    Returns the 1-indexed page number, or None if data is too short.
    """
    if len(data) < 3:
        return None
    return data[2] + 1


def decode_0x18(data):
    """type=0x18 (variable): [count][id, id, ...] = resulting Device/Palette-Select selection."""
    if len(data) < 1:
        return None
    count = data[0]
    return list(data[1:1 + count])


def decode_0x09_labels(data):
    """Decode the per-item name table for INT A / INT B / INT DEV / Independents.

    The console sends a series of 39-byte records, each structured as:
      page: index 0, then tag 0x03 0x06 + 1 pad byte, then a UTF-16LE 3-line label:
      line1[12] (6 chars), line2[12] (6 chars), line3[11] (5 chars + 1 dangling byte --
      the record is a byte short of a full 6-char third line, so the console's own firmware
      silently truncates a 6-character third word to 5, e.g. "Orange" -> "Orang". This is a
      genuine one-byte-short field in the wire format, not a decode bug on this end -- matches
      what the real console's own display shows.
    The page number maps to the 74 positional entries of the console's item model:
      0-23   IntA1-24
      24-47  IntB1-24
      48-71  Device1-24
      72-73  Independent1-2
    Returns a dict keyed by item family, with values of {index: [line1, line2, line3]} --
    the label's 3 lines exactly as laid out on the wire (and on the console's own display),
    not collapsed into one string, so a caller can render them on their own 3 rows the way
    the console does rather than word-wrapping a joined string to a different width.
    """
    if len(data) < 39:
        return {"INT A": {}, "INT B": {}, "INT DEV": {}, "Independent": {}}

    result = {"INT A": {}, "INT B": {}, "INT DEV": {}, "Independent": {}}
    if len(data) % 39:
        data = data[:len(data) - (len(data) % 39)]

    for offset in range(0, len(data), 39):
        record = data[offset:offset + 39]
        if len(record) < 39:
            continue
        page = record[0]
        if page < 0 or page > 73:
            continue

        line1 = record[4:16].decode("utf-16-le", errors="ignore").replace("\uffff", "").rstrip("\x00\ufffd").strip()
        line2 = record[16:28].decode("utf-16-le", errors="ignore").replace("\uffff", "").rstrip("\x00\ufffd").strip()
        line3 = record[28:39].decode("utf-16-le", errors="ignore").replace("\uffff", "").rstrip("\x00\ufffd").strip()
        if not (line1 or line2 or line3):
            continue

        if 0 <= page <= 23:
            family = "INT A"
            index = page + 1
        elif 24 <= page <= 47:
            family = "INT B"
            index = page - 24 + 1
        elif 48 <= page <= 71:
            family = "INT DEV"
            index = page - 48 + 1
        else:
            family = "Independent"
            index = page - 72 + 1

        result[family][index] = [line1, line2, line3]
    return result


def decode_0x0f_all_modes(data):
    """type=0x0f (75 bytes): the stored intensity table. Unlike type=0x0e (mode-agnostic --
    only ever reports whichever fader mode is CURRENTLY active), this holds all three
    intensity sub-mode banks simultaneously. Layout: `[header:1][INT A: 24][INT B: 24]
    [INT DEV: 24][+2 more bytes, likely Bumps/Master -- not decoded here since type=0x0e
    already covers those]`.
    Returns {"INT A": [24 raw intensity values], "INT B": [...], "INT DEV": [...]}, or
    None if data is too short.
    """
    if len(data) < 73:
        return None
    order = ("INT A", "INT B", "INT DEV")
    return {order[m]: [data[1 + m * 24 + n] for n in range(24)] for m in range(3)}


DMX_UNIVERSE_SIZE = 512

def decode_0x0d_dmx(data):
    """type=0x0d (1026 bytes): the DMX output levels, both universes, as a full snapshot.

    Layout: `[channel count:2, big-endian][one level byte per channel]`. The console always
    reports 1024 channels (2 universes x 512); DMX address N (1-512) of universe 1 is
    `data[1 + N]`, and universe 2 follows at `data[514:]`. Sent whenever a patched output
    changes, and every message holds every channel, so a stateful client replaces its whole
    picture each time.
    Returns (universe_1, universe_2), each a list of 512 levels (index 0 = address 1), or None
    if the payload isn't a well-formed 1024-channel message.
    """
    if len(data) < 2:
        return None
    count = int.from_bytes(data[:2], "big")
    if count != 2 * DMX_UNIVERSE_SIZE or len(data) != 2 + count:
        return None
    body = data[2:]
    return list(body[:DMX_UNIVERSE_SIZE]), list(body[DMX_UNIVERSE_SIZE:])


def decode_0x00_memory_name(data):
    """type=0x00, connect-time only: one MEMS memory ("Look")'s name, plus (not decoded here)
    a variable-length body carrying that memory's own recorded content.

    `data[0]` is the 0-indexed MEMS page and `data[1]` the 0-indexed slot within it (see
    decode_0x17_mems_page) -- confirmed as a genuine two-field split, not a single combined
    index.

    The connect-time catalog announces one type=0x00 entry per recorded memory, with (page,
    slot) as the announce selector (see decode_announce), and the reply carries that same
    page/slot here. A slot never announced has no memory recorded.

    The name sits in the LAST 38 bytes of the message, tail-anchored regardless of overall
    message length (not a fixed absolute offset -- the body before it is presumably that
    memory's own scene data, scaling with complexity): `[tag: 0x03 0x06][pad: 0x00]
    [line1: 12-byte UTF-16LE][line2: 12-byte UTF-16LE][line3: 11-byte UTF-16LE]` -- the same
    tag and 3-line (6/6/5-char) shape as decode_0x09_labels, including that decoder's same
    one-byte-short third line.

    Returns (page, slot, [line1, line2, line3]) -- both 0-indexed, lines exactly as laid out
    on the wire, same shape as decode_0x09_labels/decode_0x0c so callers can render/join them
    the same way -- or None if data is too short or the expected tag isn't where it should be
    (rather than silently decoding garbage as a name).
    """
    if len(data) < 40:
        return None
    page = data[0]
    slot = data[1]
    tag_off = len(data) - 38
    if data[tag_off:tag_off + 2] != b"\x03\x06":
        return None
    name = data[tag_off + 3:tag_off + 38]
    line1 = name[0:12].decode("utf-16-le", errors="ignore").replace("￿", "").rstrip("\x00�").strip()
    line2 = name[12:24].decode("utf-16-le", errors="ignore").replace("￿", "").rstrip("\x00�").strip()
    line3 = name[24:35].decode("utf-16-le", errors="ignore").replace("￿", "").rstrip("\x00�").strip()
    return page, slot, [line1, line2, line3]


_0X0C_MARKER = bytes.fromhex("0001030600")


def decode_0x0c(data):
    """type=0x0c: Independent 1/2 name + click-state + live value table.

    Each entry is `[clicked: 0x00/0x01][value: 0-255][marker: 00 01 03 06 00][name: 3 lines,
    same 12+12+11-byte layout as type=0x09's NamedItem]` -- 42 bytes per entry, found by
    marker-scanning (degrades gracefully if there are ever more than 2 entries). `clicked` is
    a genuinely separate bit from `value` -- "is this Independent's button currently
    held/latched on" is independent from "what level is it set to". Entries appear in physical
    order (1st = IND 1, 2nd = IND 2). The name is 3 fixed-width lines, not a variable-length
    run bounded by the next marker.

    Returns e.g. {1: (False, 73, ['Work', 'light', '']), 2: (False, 196, ['', '', ''])} --
    (clicked, raw 0-255 value, [line1, line2, line3] -- same shape as decode_0x09_labels).
    """
    starts = [i for i in range(len(data) - len(_0X0C_MARKER) + 1)
              if data[i:i + len(_0X0C_MARKER)] == _0X0C_MARKER]
    result = {}
    for n, off in enumerate(starts):
        if off < 2:
            continue
        clicked = bool(data[off - 2])
        value = data[off - 1]
        name_start = off + len(_0X0C_MARKER)
        line1 = data[name_start:name_start + 12].decode("utf-16-le", errors="ignore").rstrip("\x00").strip()
        line2 = data[name_start + 12:name_start + 24].decode("utf-16-le", errors="ignore").rstrip("\x00").strip()
        line3 = data[name_start + 24:name_start + 35].decode("utf-16-le", errors="ignore").rstrip("\x00").strip()
        result[n + 1] = (clicked, value, [line1, line2, line3])
    return result


def decode_0x16_bump_catch(data, fader):
    """type=0x16, per-fader Bump LED. `fader` is 1-indexed (1-24, same convention as
    decode_0x0e's `Fader{N}`).

    Each fader has a 6-byte block at `1+6*(N-1)`: two RGB triples, the LED's two blink
    phases -- the same shape as Solo/BlackOut's blocks (decode_0x16_indicator_lights). The color
    comes from the console itself: green `(0, v, 0)` in INT A/B/DEV and PARAM 1/2, red
    `(v, 0, 0)` in MEMS.

      - At rest / caught: both triples equal, level tracking the fader's live combined
        output 1:1 (INT/PARAM). In MEMS: 0xff while the memory is up, 0x46 for a recorded
        memory at rest, 0x0a for an empty slot.
      - Unlatched after a fader-mode switch (physical position hasn't caught its stored
        value): the triples differ, e.g. green 0x46 / 0x0a -- sent once when blinking
        starts; the alternation is local to the console, not re-sent per flash.

    "Blinking" is decoded generically as "the two triples differ".

    Returns None if data is too short, else {"blinking": True, "color_a": [r, g, b],
    "color_b": [r, g, b]} or {"blinking": False, "color": [r, g, b]}.
    """
    return _rgb_pair(data, 1 + 6 * (fader - 1))


def decode_0x16_indicator_lights(data):
    """type=0x16: the LED colors of Solo (offset 517), BlackOut (523) and the MEMS-only GO MODE
    (361) and INT ONLY (373): 6-byte blocks of two RGB triples, the LED's two blink phases. Solo
    on is white `ff ff ff`, BlackOut/Int Only/Go Mode on is blue `00 00 ff`, BlackOut's blink is
    `37 37 ff` / `0a 0a 0a`, idle is `0a 0a 0a` for all. light_state() reads on/off/blinking
    from the colors, so no "on" color is hard-coded.

    Returns {"solo", "blackout", "go_mode", "int_only"}, each in decode_0x16_bump_catch's shape
    (None if data is too short).
    """
    return {"solo": _rgb_pair(data, 517), "blackout": _rgb_pair(data, 523),
            "go_mode": _rgb_pair(data, 361), "int_only": _rgb_pair(data, 373)}


def _rgb_pair(data, off):
    """A type=0x16 6-byte LED block at `off`: two RGB triples, the LED's two blink phases.
    Returns None if data is too short, else {"blinking": True, "color_a": [r, g, b],
    "color_b": [r, g, b]} if the halves differ, or {"blinking": False, "color": [r, g, b]}.
    """
    if len(data) < off + 6:
        return None
    a, b = list(data[off:off + 3]), list(data[off + 3:off + 6])
    if a != b:
        return {"blinking": True, "color_a": a, "color_b": b}
    return {"blinking": False, "color": a}


# The console's idle LED level: a Bump LED with nothing to show reads 00 in the INT modes but 0a in
# PARAM 1/2 and MEMS (a faint glow); Solo, BlackOut, Int Only and Go Mode idle at 0a. A MEMS slot
# with a recorded memory is 46, well above it.
LED_IDLE_MAX = 0x0A


def light_state(light):
    """"on", "off" or "blinking" for a light in _rgb_pair's shape (None -> None, no data yet),
    from its real colors, whatever they are: a LED whose brightest channel is at or below
    LED_IDLE_MAX is off (also when both blink phases are that dark); otherwise it is blinking if
    its two phases differ, else on."""
    if light is None:
        return None
    colors = [light["color_a"], light["color_b"]] if light["blinking"] else [light["color"]]
    if max(max(c) for c in colors) <= LED_IDLE_MAX:
        return "off"
    return "blinking" if light["blinking"] else "on"


class ConsoleLink:
    def __init__(self, dev, ep_in, ep_out):
        self.dev = dev
        self.ep_in = ep_in
        self.ep_out = ep_out
        self.write_errors = 0
        self.read_timeouts = 0
        self.next_seq = 2  # seq 0 and 1 are used by send_gui_request() at connect
        self.fader_last = [0] * CONTROL_COUNT  # last value sent per fader: the "previous" byte

    def write_header(self, msg_type, payload_len=0, state=(0, 0, 0, 0)):
        try:
            self.dev.write(self.ep_out.bEndpointAddress, pack_header(msg_type, payload_len, state),
                            timeout=IO_TIMEOUT_MS)
            return True
        except usb.core.USBError as e:
            self.write_errors += 1
            if self.write_errors <= 5:
                print(f"[write error: {e}]", file=sys.stderr)
            return False

    def read_header(self):
        try:
            header = bytes(self.dev.read(self.ep_in.bEndpointAddress, HEADER_LEN, timeout=IO_TIMEOUT_MS))
        except usb.core.USBError:
            self.read_timeouts += 1
            return None
        if len(header) < HEADER_LEN:
            return None
        msg_type, payload_len = struct.unpack_from("<HH", header, 0)
        state = struct.unpack_from("<HHHH", header, 4)
        return msg_type, payload_len, state

    def read_payload(self, payload_len):
        try:
            return bytes(self.dev.read(self.ep_in.bEndpointAddress, payload_len, timeout=1000))
        except usb.core.USBError as e:
            print(f"[payload read error: {e}]", file=sys.stderr)
            return None

    def decode_in_payload(self, raw):
        """IN payload framing: [seq:2 LE][objLen:2 LE][type:1][objLen bytes of data]."""
        if len(raw) < 5:
            return None, None, b""
        seq = struct.unpack_from("<H", raw, 0)[0]
        obj_len = struct.unpack_from("<H", raw, 2)[0]
        obj_type = raw[4]
        data = raw[5:5 + obj_len]
        return seq, obj_type, data

    def write_payload(self, seq, obj_type, data=b""):
        """OUT payload framing: [seq:1][objLen:2 LE][type:1][data] -- companion to a
        msgType=1 write_header(1, 4 + len(data)) sent just before it (confirmed from real
        SmartSoft traffic)."""
        payload = bytes([seq & 0xFF]) + struct.pack("<H", len(data)) + bytes([obj_type]) + data
        try:
            self.dev.write(self.ep_out.bEndpointAddress, payload, timeout=IO_TIMEOUT_MS)
            return True
        except usb.core.USBError as e:
            self.write_errors += 1
            if self.write_errors <= 5:
                print(f"[write error: {e}]", file=sys.stderr)
            return False

    def send_gui_request(self, seq, subtype):
        """Send a type=0x27 GUI request OUT (msgType=1) -- real SmartSoft sends subtype=0x09
        (version query) then subtype=0x07 (full show-catalog request) right after connecting.
        subtype=0x07's reply is a single huge type=0x28 announce (191 entries) covering the
        static show catalog -- names, groups, cues, curves, current show/firmware info.
        `app.py` still uses subtype=0x07 to fetch the type=0x09 name-table pages -- the rest
        of the fast connect-time state (fader/master/bumps/mode/independents/solo/blackout)
        uses request_type() instead. The reply(ies) aren't read here -- the caller's normal
        poll loop picks them up like any other announce, since the announce/ack handling is
        generic."""
        data = bytes([subtype, 0, 0])
        if not self.write_header(1, 4 + len(data)):
            return False
        return self.write_payload(seq, 0x27, data)

    def send_button(self, code, pressed):
        """Send one button event OUT: msgType=1 header, then a type=0x14 payload with data
        [kind=1 (button)][code][pressed 1/0]. The sequence byte is a per-link counter (SmartSoft
        continues from 2 after its two connect-time type=0x27 requests; this client's own
        requests use 0 and 1)."""
        seq = self.next_seq
        self.next_seq = (seq + 1) & 0xFF
        data = bytes([1, code, 1 if pressed else 0])
        if not self.write_header(1, 4 + len(data)):
            return False
        return self.write_payload(seq, 0x14, data)

    def send_fader(self, fader, value):
        """Move fader `fader` (1-24) to `value` (0-255) as if its physical fader were moved: one
        type=0x14 payload [kind=0][fader-1][value][previous value sent for this fader]."""
        if not 1 <= fader <= CONTROL_COUNT:
            raise ValueError(f"fader must be 1-{CONTROL_COUNT}, got {fader}")
        if not 0 <= value <= 255:
            raise ValueError(f"value must be 0-255, got {value}")
        seq = self.next_seq
        self.next_seq = (seq + 1) & 0xFF
        data = bytes([0, fader - 1, value, self.fader_last[fader - 1]])
        if not self.write_header(1, 4 + len(data)):
            return False
        if not self.write_payload(seq, 0x14, data):
            return False
        self.fader_last[fader - 1] = value
        return True

    def send_mems_page(self, page):
        """Select MEMS page `page` (1-12), like the PAGE dropdown in SmartSoft: one type=0x27
        request, subtype 0x0a. Works in any fader mode; the console remembers the page."""
        if not 1 <= page <= MEMS_PAGE_COUNT:
            raise ValueError(f"page must be 1-{MEMS_PAGE_COUNT}, got {page}")
        seq = self.next_seq
        self.next_seq = (seq + 1) & 0xFF
        data = bytes([GUI_SUBTYPE_MEMS_PAGE, page - 1, 0])
        if not self.write_header(1, 4 + len(data)):
            return False
        return self.write_payload(seq, 0x27, data)

    def press_button(self, code):
        """Press then release a console button, like a click in SmartSoft. Toggle buttons
        (BlackOut, Solo, Independents) flip state once per call."""
        if not self.send_button(code, True):
            return False
        time.sleep(BUTTON_RELEASE_DELAY)
        return self.send_button(code, False)

    def request_type(self, type_byte):
        """Proactively ask the console for the current value of a given object type, without
        waiting for it to announce that type first. Same ack sequence normally sent only in
        response to a real type=0x28 announce (msgType=2 with state[0]=type, then a plain
        poll) -- but the console replies with current data even when it never announced this
        type first. This is what both apps use at connect instead of
        relying on the console's own unprompted announce, which loses a race against macOS's
        automatic USB HID driver probing almost every time.
        Returns (obj_type, data), or (None, None) on any failure/timeout.
        """
        if not self.write_header(2, 0, (type_byte, 0, 0, 0)):
            return None, None
        if not self.write_header(0, 0, (type_byte, 0, 0, 0)):
            return None, None
        reply = self.read_header()
        if reply is None:
            return None, None
        _, payload_len, _ = reply
        if payload_len == 0:
            return None, None
        raw = self.read_payload(payload_len)
        if raw is None:
            return None, None
        return self.decode_in_payload(raw)[1:]
