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

## Build

Needs the mingw-w64 cross compiler (`brew install mingw-w64`):

```
make            # -> build/hid.dll (32-bit)
```

## Install into a CrossOver bottle

With `BOTTLE` the bottle's name and `SMARTSOFT` its SmartSoft folder:

```
BOTTLE=SmartSoft
SMARTSOFT="$HOME/Library/Application Support/CrossOver/Bottles/$BOTTLE/drive_c/Program Files (x86)/ETC/SmartSoft"
WINE=/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine

cp build/hid.dll "$SMARTSOFT/Binaries/hid.dll"
"$WINE" --bottle "$BOTTLE" reg add 'HKCU\Software\Wine\AppDefaults\SmartSoft.exe\DllOverrides' /v hid /d native,builtin /f
```

To uninstall, delete `Binaries/hid.dll` and the override:

```
"$WINE" --bottle "$BOTTLE" reg delete 'HKCU\Software\Wine\AppDefaults\SmartSoft.exe\DllOverrides' /v hid /f
```

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
