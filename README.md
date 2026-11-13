<img src="https://raw.githubusercontent.com/cuemodo/consolelink/main/docs/images/consolelink-icon.png" alt="ConsoleLink app icon: three lighting faders on a dark rounded square, two blue and the raised center fader glowing warm white." width="128">

# ConsoleLink

by [Cuemodo](https://cuemodo.io)

ConsoleLink is an original software tool designed to communicate with existing lighting desks
and add new features:

- Web interface that mirrors and controls the console from any browser
- Art-Net output

It keeps these consoles usable when the manufacturer's software is not updated or supported on current operating systems.

ConsoleLink was tested with SmartFade ML, by Electronic Theatre Controls, Inc. (ETC).
Support for more lighting desks may be added in later releases.

**Disclaimer**: ConsoleLink is an independent project and is not affiliated with, endorsed by, or sponsored by ETC.
SmartFade and SmartSoft are trademarks of ETC and are used here only to identify compatible products.
Use at your own risk; no warranty is provided.

## Contents

- [Screenshots](#screenshots)
- [Requirements](#requirements)
- [Install](#install)
  - [macOS](#macos)
  - [Linux and Raspberry Pi](#linux-and-raspberry-pi)
  - [Windows](#windows)
  - [Other ways: pip, conda](#other-ways-pip-conda)
- [Usage](#usage)
  - [macOS menu-bar app](#macos-menu-bar-app)
  - [Art-Net output](#art-net-output)
  - [SmartSoft under CrossOver (macOS)](#smartsoft-under-crossover-macos)
- [Contributing](#contributing)
- [License](#license)

## Screenshots

The web app (`consolelink`) in a desktop browser:

![consolelink web app on a desktop browser: Master, Bumps, Independents, Crossfader and both LCDs on top, then the INT A, INT B and INT DEV intensity rows and the 24 physical faders](https://raw.githubusercontent.com/cuemodo/consolelink/main/docs/screenshots/desktop_v0.1.5.png)

And on a phone, where each row wraps to 6 columns:

<img src="https://raw.githubusercontent.com/cuemodo/consolelink/main/docs/screenshots/mobile_v0.1.5.png" width="300" alt="consolelink web app on a phone: the same controls with rows wrapped to 6 columns">

## Requirements

- Python 3.13 or newer
- The console connected over USB and powered on

## Install

The recommended way is [pipx](https://pipx.pypa.io): it installs a command-line app into its own
private environment and puts the command on your PATH, so there is no virtual environment to
create or activate. It installs the `consolelink` command and all dependencies. To update later: `pipx upgrade consolelink`.

### macOS

1. Install Python 3.13 or newer and pipx. macOS's built-in `python3` is older (3.9), so use
   [Homebrew](https://brew.sh): `brew install python pipx`, then `pipx ensurepath`.
2. Install ConsoleLink with the menu-bar app ([below](#macos-menu-bar-app)):

   ```
   pipx install 'consolelink[menubar]'
   ```

   For the command line only: `pipx install consolelink`. The `menubar` extra adds `rumps` and
   `keyring`, which the menu-bar app needs.

USB access works out of the box on macOS.

### Linux and Raspberry Pi

1. Check that `python3 --version` reports 3.13 or newer, then install pipx:
   `sudo apt install pipx`, then `pipx ensurepath`.
2. Install ConsoleLink:

   ```
   pipx install consolelink
   ```

3. On 32-bit Raspberry Pi OS only, install libusb (often already there):
   `sudo apt install libusb-1.0-0`.
4. Allow your user to access the console over USB, see [USB permissions](#usb-permissions) below.

#### USB permissions

On Linux, regular users can't access raw USB devices by default, so consolelink
will report `Access denied (insufficient permissions)`. Instead of running it
with `sudo`, add a udev rule.

First, with the console plugged in, check its vendor and product IDs:

```bash
lsusb | grep 14d5
```

You should see a line containing `ID 14d5:0201` (SmartFade ML). Then create
the rule, adjusting the product ID if yours differs:

```bash
sudo tee /etc/udev/rules.d/99-consolelink.rules <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="14d5", ATTRS{idProduct}=="0201", MODE="0660", GROUP="plugdev", TAG+="uaccess"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug and replug the console. If you're connecting over SSH or on a
headless system, make sure your user is in the `plugdev` group
(`sudo usermod -aG plugdev $USER`, then log out and back in).

### Windows

1. Install Python 3.13 or newer from [python.org](https://www.python.org/downloads/), then pipx:
   `py -m pip install --user pipx`, then `py -m pipx ensurepath` and open a new terminal.
2. Install ConsoleLink:

   ```
   pipx install consolelink
   ```

3. Switch the console to the generic WinUSB driver, once: libusb can't open it through the ETC
   driver installed with SmartSoft. Use [Zadig](https://zadig.akeo.ie): Options → List All
   Devices. The console shows up as two ETC SmartFade entries, interface 0 and interface 1:
   select each in turn, choose WinUSB and click Replace Driver. Then replug the console; no reboot
   needed.

   SmartSoft won't see the console while it uses WinUSB. To switch back, uninstall the device in
   Device Manager (tick "delete the driver software") and replug, or reinstall SmartSoft.

### Other ways: pip, conda

The same packages with plain `pip`, inside a virtual environment created with Python 3.13 or newer
(on macOS, Homebrew's versioned packages such as `python@3.13` only provide `python3.13`, not
`python3`: use that command instead):

```
python3 -m venv consolelink-env
source consolelink-env/bin/activate      # Windows: consolelink-env\Scripts\activate
pip install 'consolelink[menubar]'       # macOS; everywhere else: pip install consolelink
```

Activate the environment again in any new terminal before running `consolelink`. The per-system
steps above (USB permissions on Linux, WinUSB on Windows) still apply.

Using conda? `conda install pipx` and follow the pipx steps, or run the pip command inside a conda
environment.

## Usage

```
consolelink [--debug] [--capture PATH] [--no-web] [--artnet [DEST]]
            [--listen localhost|network] [--port N] [--allow-write [PASSWORD]]
```

Then open http://localhost:8765. The page updates live as
controls move; it also auto-reconnects if the console is unplugged and replugged.

For terminal use only, without the web server: `consolelink --no-web --debug` prints
every control change (and any message type that isn't decoded yet) until Ctrl+C.

By default only the machine running `consolelink` can open the page. With `--listen network`
the server listens on all network interfaces, so a phone or another computer on the same
network can open it too, at `http://<IP>:8765`. To get the IP of the machine running
`consolelink`:

```
ipconfig getifaddr en0   # macOS (en0 is usually Wi-Fi; try en1 for Ethernet)
hostname -I              # Linux
ipconfig                 # Windows (look for "IPv4 Address" under your Wi-Fi or Ethernet adapter)
```

**Command-line options** (`consolelink --help` lists them too):

- `--debug`: per-control change logging to stderr (including DMX changes), plus a liveness
  heartbeat, handshake diagnostics, and a line for every message type that has no decoder yet.
- `--capture PATH`: append every message (decoded or not) as a timestamped hex line to `PATH`, for
  comparing against a packet capture when something behaves unexpectedly.
- `--no-web`: don't start the web server, just poll the console. Needs at least one of `--debug`,
  `--capture`, `--artnet`.
- `--listen localhost|network`: who can open the web page. `localhost` (the default) means only
  this machine; `network` also serves it to every device on the same network. Not for `--no-web`.
- `--port N`: port of the web page. Default 8765.
- `--allow-write [PASSWORD]`: let the web page control the console: BlackOut, Solo, Ind 1/2, the bump
  buttons (press and hold the LED under a fader), the MEMS page, and the 24 faders, MASTER, BUMPS,
  LIVE and NEXT (drag a bar), and in MEMS mode the INT ONLY and GO MODE buttons. Off by default.
  With the default `--listen localhost` the password is optional: without one, the controls are
  unlocked straight away, and the server only accepts writes from the page served on localhost (it
  checks the Host and Origin headers, so another website open in your browser can't press buttons).
  With `--listen network` a `PASSWORD` is required, and every write must carry it: type it once in
  the page's settings ("Control password", kept in that browser) and the controls unlock. The
  password travels in the URL over plain HTTP, so it keeps casual users out and nothing more:
  enable writing over the network only on a network you trust. On a touch screen a fader must be
  tapped (it gets an outline) before it drags, so scrolling the page doesn't move faders.
- `--artnet [DEST]`: send both DMX universes as Art-Net to `DEST` (an IP address, or `broadcast`).
  Bare `--artnet` sends to `127.0.0.1`. Off by default.

**Advanced options** (Art-Net tuning):

- `--artnet-universe N`: Art-Net universe for console universe 1; universe 2 goes to N+1.
  Default 0.
- `--artnet-rate HZ`: maximum send rate when levels change. Default 40.
- `--artnet-keepalive SEC`: re-send the last frame this often when nothing changes. Default 1.

### macOS menu-bar app

<img src="https://raw.githubusercontent.com/cuemodo/consolelink/main/docs/screenshots/menubar_v0.1.5.png" width="400" alt="consolelink menubar: GUI for all options">

On macOS the same service can run from the menu bar instead of the command line (no Dock icon, no flags to remember).
It needs the `menubar` extra (see [Install](#install)); then start it with:

```
consolelink-menubar
```

The menu-bar icon (the favicon's three faders, faded while there is no console) and the first menu line show whether the console is connected. The menu has:

- **Open ConsoleLink**: opens the page in the browser.
- **Server address** (only when other devices are allowed): the address another device opens;
  click to copy it. **Show QR code…** opens a small window with that address as a QR code: scan it
  with a phone or tablet camera to open the page. The code holds the address only, never the
  password.
- **Settings**: allow other devices on the network (`--listen network`), allow control from the
  page (`--allow-write`), the control password, the Art-Net destination and universe, the
  port, and **Relay SmartSoft to the console** (`--smartsoft-bridge`, see
  [SmartSoft under CrossOver](#smartsoft-under-crossover-macos)). The password is kept in the
  macOS Keychain; the rest in `~/Library/Application Support/ConsoleLink/settings.json`. A change
  is saved and applied right away (the service restarts).
- **Quit ConsoleLink**: releases the console before exiting.

Only one program can use the console at a time, so don't run `consolelink` and the menu-bar app
together. The command line works as before on every system; the menu-bar app is macOS only.

### Art-Net output

```
consolelink --artnet                    # visualiser on this machine
consolelink --artnet 192.168.1.50       # an Art-Net node or another computer
consolelink --artnet broadcast          # whole local network
```

Console universe 1 and 2 are sent as Art-Net universes 0 and 1 (change with `--artnet-universe`).
Art-Net counts from 0, so some software labels these "1" and "2" — if a visualiser shows nothing,
try the neighbouring universe numbers. On the same machine, set the visualiser's Art-Net input to
listen on UDP port 6454 (some let you pick the network interface: choose loopback or "all").

If several programs on this machine listen on port 6454 (say a visualiser and an Art-Net monitor),
a unicast destination reaches only one of them. Use `--artnet broadcast` to feed all of them.

The console only reports DMX when a patched output changes, but Art-Net receivers expect a steady
stream, so the last frame is re-sent every second (`--artnet-keepalive`) while nothing moves. If the
console is unplugged, the last frame keeps being sent.

Frames go console → USB → this program → UDP with no timing guarantee, so this is fine for
visualisation and casual use, not for anything where a stall or a frozen frame would be a problem.

### SmartSoft under CrossOver (macOS)

**Experimental.** ETC's SmartSoft for Windows runs on a Mac under
[CrossOver](https://www.codeweavers.com/crossover), but CrossOver can't pass the console's USB
connection through to it. ConsoleLink can bridge the two: SmartSoft talks to a small stand-in
for the console's USB driver inside CrossOver, which relays everything to ConsoleLink on the Mac,
which talks to the console. SmartSoft then works as it does on Windows.

Once, with SmartSoft installed in a CrossOver bottle and not running:

```
consolelink smartsoft install
```

This copies the bridge's `hid.dll` next to `SmartSoft.exe` and sets a Wine DLL override for
SmartSoft.exe only; nothing of SmartSoft is modified. With several bottles holding SmartSoft, add
`--bottle NAME`. `consolelink smartsoft status` shows what is installed, and
`consolelink smartsoft uninstall` removes both changes. Neither asks for any macOS permission.

Then, each time:

```
consolelink --smartsoft-bridge
```

(or, in the menu-bar app, turn on Settings > **Relay SmartSoft to the console**; it offers to
install or update the DLL when needed) and start SmartSoft in CrossOver: it finds the console and
selects SmartFade ML by itself. Start
ConsoleLink first: if it isn't running, SmartSoft sees no console, as if it were unplugged. While
SmartSoft runs, the web page and Art-Net keep following the console, read-only. If ConsoleLink
restarts or the console is unplugged, SmartSoft picks up again once it is back.

After upgrading ConsoleLink, quit SmartSoft and run `consolelink smartsoft install` again: it
updates the DLL if the new version ships a different one. `consolelink --smartsoft-bridge` says so
at startup when the bottle's DLL is out of date, and if the two can no longer understand each
other, both refuse to connect and say what to do.

Tested with SmartSoft 3.0.2 and CrossOver 26.3 on Apple silicon. Under plain Wine (Linux), use
`consolelink smartsoft install --prefix PATH`: untested. Not on Windows, where SmartSoft reaches
the console directly. Details: [`bridge/windows/README.md`](https://github.com/cuemodo/consolelink/blob/main/bridge/windows/README.md).

## Contributing

Working on the code, running the tests or making a release? See
[CONTRIBUTING.md](https://github.com/cuemodo/consolelink/blob/main/CONTRIBUTING.md), which also
explains how the console protocol works and how it was studied.

## License

MIT -- see [`LICENSE`](https://github.com/cuemodo/consolelink/blob/main/LICENSE).

## Credits

Art-Net™ Designed by and Copyright Artistic Licence Engineering Ltd
