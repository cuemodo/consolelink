"""The decisions behind the macOS menu-bar app, kept free of rumps/Cocoa so they run (and are
tested) on any OS: what the status line says, which address another device should open, whether a
settings change is allowed, and the SmartSoft bridge's setup."""
import dataclasses
import socket
import subprocess

from . import app, smartsoft_bridge

ICON_CONNECTED = "connected"  # file names (without .png) in menubar_icons/
ICON_WAITING = "waiting"


def status_text(connected, device, busy=False):
    if connected:
        return f"● Connected · {device}" if device else "● Connected"
    if busy:
        return "○ Console in use by another program"
    return "○ Waiting for console…"


def icon_name(connected):
    return ICON_CONNECTED if connected else ICON_WAITING


def lan_ip():
    """This machine's address on the local network, or None. A UDP "connect" only picks the
    outgoing interface; nothing is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            ip = sock.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def local_url(port):
    return f"http://localhost:{port}"


def server_url(settings, port, ip):
    """The address another device (phone, tablet) opens, shown as text and as a QR code; None
    when the page is only served to this machine (or there is no network address)."""
    if settings.listen != "network" or not ip:
        return None
    return f"http://{ip}:{port}"


def needs_password(settings):
    """Control from other devices on the network must have a password."""
    return settings.allow_write and settings.listen == "network" and not settings.password


def changed(settings, **changes):
    """`settings` with `changes` applied. Raises ValueError (message ready to show) if the result
    makes no sense; a blank Art-Net destination means "off"."""
    if "artnet_dest" in changes:
        changes["artnet_dest"] = (changes["artnet_dest"] or "").strip() or None
    new = dataclasses.replace(settings, **changes)
    new.validate()
    return new


def parse_port(text):
    """A port number from what was typed, or ValueError."""
    text = text.strip()
    if not text.isdecimal() or not 1 <= int(text) <= 65535:
        raise ValueError("The port must be a number between 1 and 65535.")
    return int(text)


def parse_universe(text):
    text = text.strip()
    if not text.isdecimal() or int(text) > 0x7FFE:
        raise ValueError("The Art-Net universe must be a number between 0 and 32766.")
    return int(text)


# ---- the SmartSoft bridge (SmartSoft for Windows under CrossOver drives the console)

def bridge_changes(settings, on):
    """The settings change for the bridge toggle. Turning it on also turns off control from the
    page: SmartSoft drives the console, and the two can't both write to it."""
    if on:
        return {"smartsoft_bridge": smartsoft_bridge.DEFAULT_PORT, "allow_write": False}
    return {"smartsoft_bridge": None}


def bridge_setup_message(problem):
    """The alert offering to install the bridge DLL, from smartsoft_install.setup_problem()."""
    label, what = problem
    return (f"The SmartSoft bridge DLL in {label} {what}, so SmartSoft won't find the console.\n\n"
            "Quit SmartSoft if it is running, then click Install.")


def install_bridge():
    """`consolelink smartsoft install`, from the menu: None when done, or why it failed (ready to
    show)."""
    from . import smartsoft_install as si
    try:
        si.install(si.find_target(), si.packaged_dll(), out=lambda *a: None)
    except (si.SetupError, OSError, subprocess.SubprocessError) as e:
        return str(e)
    return None
