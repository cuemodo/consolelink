# SmartSoft bridge (Windows side)

Lets ETC SmartSoft for Windows, run on a Mac under [CrossOver](https://www.codeweavers.com/crossover),
drive a SmartFade ML connected to the Mac. CrossOver can't pass USB devices through to Windows
programs; this bridge works around that:

```
SmartSoft.exe (CrossOver) ── hid.dll (this folder) ── TCP 127.0.0.1:47561 ── consolelink --smartsoft-bridge ── USB ── console
```

**Status: experimental.** Tested with SmartSoft 3.0.2 and CrossOver 26.3 on Apple silicon, with a
SmartFade ML: SmartSoft connects, downloads the console's state and controls it.

## How it works

SmartSoft for Windows doesn't use a USB library: it talks to ETC's USB driver through ordinary
Windows calls. It finds the device with SetupDi on the driver's interface GUID, checks the
product ID with three `DeviceIoControl` codes, and then sends each bulk transfer with a
synchronous `ReadFile` or `WriteFile`. At startup it also lists HID devices to pick the
console model.

`hid.dll` is loaded by SmartSoft.exe in place of Wine's own. It shows SmartSoft a console (a HID
device 14d5:0201 and the driver's device) and sends every transfer over TCP to ConsoleLink on
the Mac, which replays it on the real console. Calls about any other device go to Wine
unchanged. Nothing of SmartSoft is modified: the DLL is one added file, and the override only
applies to SmartSoft.exe. See the comments at the top of `shim.c` and `backend_tcp.c`, and
`src/consolelink/smartsoft_bridge.py` for the TCP protocol.

## Install

Released ConsoleLink packages include the built DLL. With SmartSoft installed in a CrossOver bottle
and not running:

```
consolelink smartsoft install      # also: status, uninstall; --bottle NAME, --prefix PATH (plain Wine)
```

It copies `hid.dll` into SmartSoft's `Binaries` folder and sets the DLL override
`HKCU\Software\Wine\AppDefaults\SmartSoft.exe\DllOverrides`: `hid` = `native,builtin`, so only
SmartSoft.exe loads it. It refuses while SmartSoft runs (any process with `SmartSoft.exe` open,
from `lsof`), and won't replace a `hid.dll` that isn't the bridge's without `--force` (which keeps
it as `hid.dll.orig`; `uninstall` restores it). See `src/consolelink/smartsoft_install.py`.

The DLL and ConsoleLink must match, so after upgrading ConsoleLink run `consolelink smartsoft
install` again (SmartSoft closed). Two checks catch a forgotten update:

- At startup, `consolelink --smartsoft-bridge` compares the bottle's `hid.dll` with the one it
  ships, and prints a warning if it is missing, different, or lacks its DLL override.
- The DLL's HELLO carries its protocol version. If ConsoleLink speaks another one, it answers
  status 5 (version mismatch) with its own version, and both sides log the mismatch once,
  ConsoleLink on stderr and the DLL in its log. SmartSoft then sees no console.

### By hand

The same two changes, with `BOTTLE` the bottle's name:

```
BOTTLE=SmartSoft
SMARTSOFT="$HOME/Library/Application Support/CrossOver/Bottles/$BOTTLE/drive_c/Program Files (x86)/ETC/SmartSoft"
WINE=/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine

cp build/hid.dll "$SMARTSOFT/Binaries/hid.dll"
"$WINE" --bottle "$BOTTLE" reg add 'HKCU\Software\Wine\AppDefaults\SmartSoft.exe\DllOverrides' /v hid /d native,builtin /f
```

To undo: delete `Binaries/hid.dll`, and
`"$WINE" --bottle "$BOTTLE" reg delete 'HKCU\Software\Wine\AppDefaults\SmartSoft.exe\DllOverrides' /v hid /f`.

## Build

Needs the mingw-w64 cross compiler (macOS: `brew install mingw-w64`; Debian/Ubuntu:
`apt install gcc-mingw-w64-i686`):

```
make            # -> build/hid.dll (32-bit)
make package    # also copies it into the Python package (src/consolelink/bridge_dll/, gitignored)
```

The release workflow runs `make package` before building the Python package, so released packages
ship the DLL. From a git checkout, `consolelink smartsoft install` uses `build/hid.dll` if the
package has none.

## Use

1. On the Mac, with the console plugged in: `consolelink --smartsoft-bridge`. The web page and
   Art-Net keep working and mirror the console, read-only.
2. Start SmartSoft in CrossOver. It finds the console and selects SmartFade ML on its own.

If ConsoleLink isn't running or has no console, SmartSoft sees no console, as if it were
unplugged.

## Settings and log

The DLL reads these from the environment (CrossOver passes the Mac environment through, e.g.
`SMARTSOFT_BRIDGE=stub "$WINE" --bottle "$BOTTLE" 'C:\...\SmartSoft.exe'`):

| Variable | Meaning |
|---|---|
| `SMARTSOFT_BRIDGE` | `tcp` (default, 127.0.0.1:47561), `tcp:PORT`, `tcp:HOST:PORT`, or `stub`: a built-in fake console that only answers SmartSoft's polls, to test the DLL without hardware or ConsoleLink. |
| `SMARTSOFT_BRIDGE_LOG` | Log file, as a Windows path (default `%TEMP%\smartsoft-bridge.log`, inside the bottle under `drive_c/users/crossover/AppData/Local/Temp`). |
| `SMARTSOFT_BRIDGE_TRACE` | `1` logs every transfer in hex. |

`consolelink --smartsoft-bridge PORT` changes the Mac side's port; `--capture PATH` logs a
SmartSoft session, including what SmartSoft sends (`sent` lines).
