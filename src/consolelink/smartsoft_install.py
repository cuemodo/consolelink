"""
`consolelink smartsoft install|uninstall|status`: sets up the SmartSoft bridge in a CrossOver
bottle (or any Wine prefix), so ETC SmartSoft for Windows reaches the console through
`consolelink --smartsoft-bridge` (see smartsoft_bridge.py).

Installing makes two changes, both undone by `uninstall`; nothing of SmartSoft is modified:
  - the bridge's hid.dll (bridge/windows/ in the repository, shipped in the package) is copied
    into SmartSoft's Binaries folder, next to SmartSoft.exe;
  - a Wine DLL override, for SmartSoft.exe only, makes it load that hid.dll instead of Wine's:
    HKCU\Software\Wine\AppDefaults\SmartSoft.exe\DllOverrides, hid = native,builtin.
Both are refused while SmartSoft runs. "Running" means some process has SmartSoft.exe open
(`lsof`), which holds however SmartSoft was started (CrossOver's shortcut or `wine`).

Where SmartSoft is: in a CrossOver bottle (macOS: ~/Library/Application Support/CrossOver/Bottles;
Linux: ~/.cxoffice), picked with --bottle NAME when there are several; or in any Wine prefix
with --prefix PATH (Wine from PATH, or --wine PATH).
"""
import argparse
import dataclasses
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

OVERRIDE_KEY = r"HKCU\Software\Wine\AppDefaults\SmartSoft.exe\DllOverrides"
OVERRIDE_VALUE = "native,builtin"
# Text found in every build of the bridge DLL (its log banner, see bridge/windows/shim.c).
MARKER = b"---- SmartSoft bridge loaded"
SMARTSOFT_GLOB = "drive_c/Program Files*/ETC/SmartSoft/Binaries/SmartSoft.exe"


def crossover_locations():
    """(bottles folder, CrossOver wine candidates) for this OS."""
    home = Path.home()
    if sys.platform == "darwin":
        wine = "Contents/SharedSupport/CrossOver/bin/wine"
        return (home / "Library/Application Support/CrossOver/Bottles",
                [Path("/Applications/CrossOver.app") / wine, home / "Applications/CrossOver.app" / wine])
    return home / ".cxoffice", [Path("/opt/cxoffice/bin/wine")]


class SetupError(Exception):
    """A problem to report to the user (the message says what to do)."""


@dataclasses.dataclass
class Target:
    label: str              # for messages: 'CrossOver bottle "Smartfade"'
    prefix: Path            # the bottle / Wine prefix (holds drive_c and user.reg)
    exe: Path               # SmartSoft.exe
    wine: list | None       # command prefix to run a Windows program there; None if not found
    env: dict | None = None  # extra environment for it (WINEPREFIX for plain Wine)
    wineserver: str | None = None  # to wait until Wine has saved the registry (run with WINEPREFIX)

    @property
    def dll(self):
        return self.exe.parent / "hid.dll"

    @property
    def backup(self):
        """Where install --force keeps a hid.dll that wasn't the bridge's."""
        return self.exe.parent / "hid.dll.orig"


def find_smartsoft(prefix):
    """SmartSoft.exe inside a bottle / prefix, or None."""
    found = sorted(Path(prefix).glob(SMARTSOFT_GLOB))
    return found[0] if found else None


def _wineserver_next_to(wine_path):
    if not wine_path:
        return None
    sibling = Path(wine_path).with_name("wineserver")
    return str(sibling) if sibling.is_file() else shutil.which("wineserver")


def find_target(bottle=None, prefix=None, wine=None, bottles_dir=None, wine_candidates=None):
    """The SmartSoft install to work on, from the command-line choices."""
    if prefix is not None:
        prefix = Path(prefix).expanduser()
        exe = find_smartsoft(prefix)
        if exe is None:
            raise SetupError(f"no SmartSoft in {prefix} (looked for {SMARTSOFT_GLOB})")
        wine_path = wine or shutil.which("wine")
        return Target(f"Wine prefix {prefix}", prefix, exe, [wine_path] if wine_path else None,
                      {"WINEPREFIX": str(prefix)}, _wineserver_next_to(wine_path))

    default_dir, default_wines = crossover_locations()
    bottles_dir = Path(bottles_dir or default_dir)
    if wine:
        wine_path = wine
    else:
        candidates = default_wines if wine_candidates is None else wine_candidates
        wine_path = next((str(p) for p in candidates if p.is_file()), None)

    def target(name):
        exe = find_smartsoft(bottles_dir / name)
        return exe and Target(f'CrossOver bottle "{name}"', bottles_dir / name, exe,
                              [wine_path, "--bottle", name] if wine_path else None,
                              wineserver=_wineserver_next_to(wine_path))

    if bottle is not None:
        if not (bottles_dir / bottle).is_dir():
            raise SetupError(f'no CrossOver bottle "{bottle}" in {bottles_dir}')
        found = target(bottle)
        if not found:
            raise SetupError(f'bottle "{bottle}" has no SmartSoft (looked for {SMARTSOFT_GLOB})')
        return found
    names = sorted(p.name for p in bottles_dir.iterdir() if p.is_dir()) if bottles_dir.is_dir() else []
    found = [t for t in map(target, names) if t]
    if not found:
        raise SetupError(f"no CrossOver bottle with SmartSoft in {bottles_dir}: install SmartSoft "
                         "in a bottle first, or give a Wine prefix with --prefix PATH")
    if len(found) > 1:
        raise SetupError("SmartSoft is in several bottles ("
                         + ", ".join(t.prefix.name for t in found) + "): pick one with --bottle NAME")
    return found[0]


def packaged_dll():
    """The bridge DLL's bytes: shipped in the package, or built in a git checkout; else None."""
    shipped = resources.files("consolelink") / "bridge_dll" / "hid.dll"
    if shipped.is_file():
        return shipped.read_bytes()
    checkout = Path(__file__).resolve().parents[2] / "bridge" / "windows" / "build" / "hid.dll"
    if checkout.is_file():
        return checkout.read_bytes()
    return None


def dll_state(target, dll_bytes):
    """'absent', 'current' (same as the package's), 'outdated' (another bridge build), or
    'foreign' (a hid.dll that isn't the bridge's)."""
    if not target.dll.is_file():
        return "absent"
    installed = target.dll.read_bytes()
    if dll_bytes is not None and installed == dll_bytes:
        return "current"
    return "outdated" if MARKER in installed else "foreign"


def running_pids(exe, run=subprocess.run):
    """PIDs with SmartSoft.exe open (SmartSoft and its wineserver), [] if none, None if this
    can't be checked (no lsof)."""
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    result = run([lsof, "-t", str(exe)], capture_output=True, text=True)
    return [int(pid) for pid in result.stdout.split() if pid.isdigit()]


def parse_user_reg(text):
    """The hid override's value from a prefix's user.reg, or None."""
    section = "[software\\\\wine\\\\appdefaults\\\\smartsoft.exe\\\\dlloverrides]"
    inside = False
    for line in text.splitlines():
        if line.startswith("["):
            inside = line.lower().split("]")[0] + "]" == section
        elif inside and line.lower().startswith('"hid"='):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _wine(target, args, run):
    if not target.wine:
        raise SetupError(f"can't find Wine/CrossOver to change {target.label}'s registry: give it "
                         "with --wine PATH, or set the override by hand (see "
                         "bridge/windows/README.md in the repository)")
    env = dict(os.environ, **(target.env or {}))
    return run(target.wine + args, capture_output=True, text=True, env=env)


def override_value(target):
    """The hid override for SmartSoft.exe as saved in the prefix's user.reg, or None. Read from
    the file rather than through `wine reg query`, which starts the whole bottle (seconds); Wine
    saves the registry there when its last program exits, which install/uninstall wait for."""
    reg = target.prefix / "user.reg"
    return parse_user_reg(reg.read_text(errors="replace")) if reg.is_file() else None


def wait_registry_saved(target, run, out):
    """Let the bottle's Wine exit, so the registry change is in user.reg (status reads it)."""
    if not target.wineserver:
        return
    env = dict(os.environ, WINEPREFIX=str(target.prefix))
    try:
        run([target.wineserver, "-w"], capture_output=True, text=True, env=env, timeout=60)
    except subprocess.TimeoutExpired:
        out("(Wine is still running in that bottle; the change is saved when it exits)")


def require_closed(target, run, force):
    pids = running_pids(target.exe, run)
    if pids:
        raise SetupError(f"SmartSoft is running in {target.label} (pid "
                         f"{', '.join(map(str, pids))}): quit it first")
    if pids is None and not force:
        raise SetupError("can't check whether SmartSoft is running (lsof not found): quit it, "
                         "then run again with --force")


def install(target, dll_bytes, force=False, run=subprocess.run, out=print):
    if dll_bytes is None:
        raise SetupError("this ConsoleLink has no bridge DLL. Released packages include it; from "
                         "a git checkout, build it first: make -C bridge/windows package "
                         "(needs mingw-w64)")
    require_closed(target, run, force)
    state = dll_state(target, dll_bytes)
    if state == "foreign":
        if not force:
            raise SetupError(f"{target.dll} exists and isn't the bridge's: run again with --force "
                             f"to replace it (it is kept as {target.backup.name})")
        os.replace(target.dll, target.backup)
        out(f"Kept the previous hid.dll as {target.backup}")
    if state != "current":
        tmp = target.dll.with_name("hid.dll.tmp")
        tmp.write_bytes(dll_bytes)
        os.replace(tmp, target.dll)
        out(f"{'Updated' if state == 'outdated' else 'Copied'} the bridge DLL to {target.dll}")
    else:
        out(f"The bridge DLL is already up to date in {target.dll}")
    if override_value(target) != OVERRIDE_VALUE:
        result = _wine(target, ["reg", "add", OVERRIDE_KEY, "/v", "hid", "/d", OVERRIDE_VALUE,
                                "/f"], run)
        if result.returncode != 0:
            raise SetupError(f"setting the DLL override failed: {result.stderr.strip() or result.stdout.strip()}")
        wait_registry_saved(target, run, out)
        out(f"Set the DLL override for SmartSoft.exe ({OVERRIDE_KEY}: hid = {OVERRIDE_VALUE})")
    else:
        out("The DLL override for SmartSoft.exe is already set")
    out(f"\nDone. Next: run `consolelink --smartsoft-bridge` with the console plugged in, then "
        f"start SmartSoft ({target.label}).")


def uninstall(target, force=False, run=subprocess.run, out=print):
    require_closed(target, run, force)
    state = dll_state(target, None)
    if state == "foreign":
        out(f"Left {target.dll} alone: it isn't the bridge's")
    elif state != "absent":
        target.dll.unlink()
        out(f"Removed {target.dll}")
        if target.backup.is_file():
            os.replace(target.backup, target.dll)
            out(f"Restored the previous hid.dll from {target.backup.name}")
    else:
        out("The bridge DLL isn't installed")
    if override_value(target) is not None:
        result = _wine(target, ["reg", "delete", OVERRIDE_KEY, "/v", "hid", "/f"], run)
        if result.returncode != 0:
            raise SetupError(f"removing the DLL override failed: {result.stderr.strip() or result.stdout.strip()}")
        wait_registry_saved(target, run, out)
        out("Removed the DLL override for SmartSoft.exe")
    else:
        out("No DLL override to remove")


def status(target, dll_bytes, run=subprocess.run, out=print):
    out(f"SmartSoft:    {target.exe.parent.parent} ({target.label})")
    state = dll_state(target, dll_bytes)
    out("Bridge DLL:   " + {
        "absent": "not installed",
        "current": "installed, up to date",
        "outdated": "installed, another version (`consolelink smartsoft install` updates it)"
                    if dll_bytes is not None else "installed",
        "foreign": "a hid.dll that isn't the bridge's is there",
    }[state])
    value = override_value(target)
    out("DLL override: " + ("set" if value == OVERRIDE_VALUE
                            else "not set" if value is None else f"set to {value!r}, not {OVERRIDE_VALUE!r}"))
    pids = running_pids(target.exe, run)
    out("SmartSoft is: " + ("unknown (lsof not found)" if pids is None
                            else f"running (pid {', '.join(map(str, pids))})" if pids else "not running"))
    if dll_bytes is None:
        out("(this ConsoleLink has no bridge DLL to install: see `consolelink smartsoft install`)")


def setup_problem(dll_bytes=None, find=find_target):
    """What keeps the bridge from working in SmartSoft's bottle, as (bottle label, problem), or
    None when it is set up -- or when that can't be told (no bottle with SmartSoft, several, no
    DLL in this package). Never raises: a check that fails must not stop the bridge."""
    try:
        dll_bytes = packaged_dll() if dll_bytes is None else dll_bytes
        if dll_bytes is None:
            return None
        target = find()
        state = dll_state(target, dll_bytes)
        problem = {"absent": "isn't installed",
                   "outdated": "is out of date",
                   "foreign": "isn't installed (another hid.dll is there)"}.get(state)
        if problem is None and override_value(target) != OVERRIDE_VALUE:
            problem = "is missing its DLL override"
        return (target.label, problem) if problem else None
    except Exception:   # noqa: BLE001 -- see the docstring
        return None


def startup_warning(dll_bytes=None, find=find_target):
    """For `consolelink --smartsoft-bridge`: a line saying the bottle's bridge needs (re)installing,
    or None (see setup_problem)."""
    found = setup_problem(dll_bytes, find)
    if found is None:
        return None
    label, problem = found
    return f"the bridge DLL in {label} {problem}: quit SmartSoft, then run `consolelink smartsoft install`"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="consolelink smartsoft",
        description="Set up the SmartSoft bridge: lets ETC SmartSoft for Windows, run under "
                    "CrossOver (or Wine), drive the console through `consolelink "
                    "--smartsoft-bridge`.")
    actions = parser.add_subparsers(dest="action", required=True, metavar="ACTION")
    for name, text in (("status", "show what is installed, and whether SmartSoft is running"),
                       ("install", "install or update the bridge DLL and its DLL override"),
                       ("uninstall", "remove both")):
        p = actions.add_parser(name, help=text, description=text)
        p.add_argument("--bottle", metavar="NAME",
                       help="the CrossOver bottle with SmartSoft (needed only if several have it)")
        p.add_argument("--prefix", metavar="PATH",
                       help="a Wine prefix instead of a CrossOver bottle (uses `wine` from PATH)")
        p.add_argument("--wine", metavar="PATH", help="the wine command to use")
        if name != "status":
            p.add_argument("--force", action="store_true",
                           help="replace a hid.dll that isn't the bridge's (keeping it as "
                                "hid.dll.orig), or go ahead when lsof is missing")
    return parser


def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        parser.exit(2, "consolelink smartsoft: the bridge is for SmartSoft under CrossOver or "
                       "Wine; on Windows, run SmartSoft directly\n")
    if args.bottle and args.prefix:
        parser.error("give --bottle or --prefix, not both")
    try:
        target = find_target(bottle=args.bottle, prefix=args.prefix, wine=args.wine)
        if args.action == "status":
            status(target, packaged_dll())
        elif args.action == "install":
            install(target, packaged_dll(), force=args.force)
        else:
            uninstall(target, force=args.force)
    except SetupError as e:
        print(f"consolelink smartsoft: {e}", file=sys.stderr)
        return 1
    return 0
