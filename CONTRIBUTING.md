# Contributing to ConsoleLink

Notes for working on the code. For installing and using ConsoleLink, see the
[README](https://github.com/cuemodo/consolelink/blob/main/README.md).

## Contents

- [How it works](#how-it-works)
- [Protocol study process](#protocol-study-process)
- [Source files](#source-files)
- [Install from source](#install-from-source)
- [Tests](#tests)
- [Releasing](#releasing)

## How it works

The console speaks a request/reply protocol over two USB bulk endpoints: a
12-byte header (msgType, payloadLen, 4×uint16 state) is polled OUT
repeatedly, and the console replies with its own header, followed by a
payload when it has one. When a control changes, the console first sends an
"announce" (payload type `0x28`) naming which object type(s) are ready; the
host acks by naming that type back, then the real payload follows.
ConsoleLink also proactively requests the types it needs at connect time
(`ConsoleLink.request_type()`) rather than waiting for an announce, since
the console's own unprompted announce loses a race against the OS's USB
probing on connect.

See [`protocol.py`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/protocol.py)'s module docstring for the full message-type breakdown
(which types are decoded, which are known-but-not-yet decoded, and why).

## Protocol study process

The general process to establish the protocol details was to capture USB traces using the original software and consoles,
and study the traces to understand the wire protocol.

For the SmartFade ML case in particular, the traces were obtained running the original SmartSoft software
under Windows 10 against an actual SmartFade ML console. USB traffic was captured with Wireshark.
Each capture isolated a single step (initial connection, moving one fader, pressing one button, etc.) so the resulting
traces could be matched to a specific action.

## Source files

| File | What it is |
|---|---|
| [`src/consolelink/protocol.py`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/protocol.py) | Shared library: USB framing, the idle-poll/announce/ack handshake, and decoders for every known message type. Everything else imports this rather than re-deriving the protocol. |
| [`src/consolelink/app.py`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/app.py) | The program (the `consolelink` command): polls the console in a background thread and serves [`index.html`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/static/index.html) over Server-Sent Events, so a browser tab shows live values. With `--no-web` it only polls (logging changes with `--debug`, or sending Art-Net). |
| [`src/consolelink/menubar.py`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/menubar.py), [`menubar_logic.py`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/menubar_logic.py), [`menubar_qr.py`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/menubar_qr.py), [`settings_store.py`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/settings_store.py) | The macOS menu-bar app (`consolelink-menubar`): `menubar.py` is the thin rumps layer, `menubar_logic.py` and `settings_store.py` hold the testable decisions and the saved settings, and `menubar_qr.py` draws the server address as a QR code (Core Image, no extra dependency). It drives `app.Service`, the same start/stop API `consolelink` uses. |
| [`scripts/make_menubar_icons.py`](https://github.com/cuemodo/consolelink/blob/main/scripts/make_menubar_icons.py) | Draws the menu-bar icons (macOS, PyObjC) into `src/consolelink/menubar_icons/`; run again after changing the drawing. |
| [`src/consolelink/artnet.py`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/artnet.py) | Optional Art-Net output of the two DMX universes, enabled with `--artnet`. |
| [`src/consolelink/smartsoft_bridge.py`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/smartsoft_bridge.py), [`bridge/windows/`](https://github.com/cuemodo/consolelink/tree/main/bridge/windows/) | Experimental: lets SmartSoft for Windows, run under CrossOver on the Mac, drive the console through ConsoleLink (`--smartsoft-bridge`). `bridge/windows/` is a 32-bit `hid.dll`, in C, that stands in for the console's USB driver inside CrossOver and sends each transfer over TCP; `smartsoft_bridge.py` replays them on the console. See [`bridge/windows/README.md`](https://github.com/cuemodo/consolelink/blob/main/bridge/windows/README.md). |
| [`src/consolelink/static/index.html`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/static/index.html) | Static single-page UI for `consolelink`: intensity meters (INT A / INT B / INT DEV), physical faders, most important buttons and indicators, like BlackOut and Master, and a mirror of the console's two LCDs, plus a "DMX Outputs" tab showing both DMX universes (1024 channels) at once. |
| [`src/consolelink/static/app.js`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/static/app.js) | Front-end logic for `index.html`: builds the meter grid, connects to the SSE stream, and renders each incoming state update. |
| [`src/consolelink/static/style.css`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/static/style.css) | Styling for `index.html`: colours, the meter and indicator layout, the blinking-LED animations, and the breakpoints that reflow the page for phones. |
| [`src/consolelink/static/favicon.svg`](https://github.com/cuemodo/consolelink/blob/main/src/consolelink/static/favicon.svg) | The browser tab icon. |
| [`pyproject.toml`](https://github.com/cuemodo/consolelink/blob/main/pyproject.toml) | Packaging: makes `pip install .` install the package, its web files and the `consolelink` command; lists the dependencies, including the `dev` extra (`pytest`, `bump-my-version`) and the `menubar` extra (`rumps`, `keyring`; macOS only). |
| [`tests/`](https://github.com/cuemodo/consolelink/tree/main/tests/) | Decoder and server tests, replaying recorded console traffic from [`tests/fixtures/`](https://github.com/cuemodo/consolelink/blob/main/tests/fixtures/) -- no console needed. |

## Install from source

To run an unreleased version, or to work on the code, clone the repository and install from the folder that
contains `pyproject.toml`: `pip install .` (or `pip install '.[menubar]'` on macOS).

**To work on the code**, install with the `dev` extra (adds `pytest` and `bump-my-version`) and
`-e`, so edits to the source take effect without reinstalling (new commands still need one reinstall).
On macOS add the `menubar` extra too. Create the virtual environment with an explicit Python 3.13 or
newer (`python3.13 -m venv .venv`; on macOS plain `python3` may be the old system one):

```
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # Linux, Windows, Raspberry Pi
pip install -e ".[dev,menubar]"    # macOS
```

## Tests

The tests replay real console traffic recorded from a SmartFade ML, so they
run without a console attached:

```
python -m pytest
```

(Run from this folder. `pyproject.toml` points pytest at `src/`.)

Each file in `tests/fixtures/` is a few messages in the same line format
`--capture` writes, with comments saying what the console was doing, and
`# @tag:` lines naming the messages the tests look up. The expected values in
the tests are what the console itself showed at the time -- not just whatever
the decoder currently returns -- so a decoder change that breaks one is a real
regression.

## Releasing

Release from `main`, after the work is merged from the feature branch:

```
git checkout main && git pull
git status                     # the tree must be clean
bump-my-version bump patch     # or minor / major; add --dry-run -vv to preview
git push --follow-tags         # pushes the commit and the tag together
```

Pushing the tag starts the `release` workflow (`.github/workflows/release.yml`): it runs the tests,
builds the package, and publishes it to PyPI with trusted publishing (no API token is stored). The
`pypi` GitHub environment requires a reviewer, so approve the run in the Actions tab. A PyPI version can
never be uploaded twice: if a release is bad, yank it on PyPI and release the next version.

`bump-my-version` (part of the `dev` extra) edits `VERSION` in `src/consolelink/protocol.py`,
commits it, and creates an annotated tag, so the file and the tag can't drift apart. Going from
0.1.1 to 0.1.2, for example, it commits "Bump version: 0.1.1 → 0.1.2" and tags that commit `v0.1.2`
(annotated: the tag records who made it, when, and the message "Version 0.1.2").
Don't edit `VERSION` by hand.

Bump on a feature branch is blocked.
