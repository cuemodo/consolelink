"""
SmartSoft bridge: lets ETC's SmartSoft for Windows, run on the Mac under CrossOver, drive the
console through ConsoleLink (`consolelink --smartsoft-bridge`).

CrossOver can't pass USB devices through to Windows programs. A small 32-bit DLL in the
CrossOver bottle (bridge/windows/ in the repository, loaded by SmartSoft.exe in place of Wine's
hid.dll) stands in for the console's USB driver: SmartSoft sees a console, and every bulk
transfer it makes comes here over TCP on localhost. This module replays each one on the real
console and sends back the result, so SmartSoft drives the console as it does on Windows;
ConsoleLink only relays.

The relayed traffic is also decoded on the way through (StreamTap): the web page and Art-Net
keep mirroring the console (read-only, as SmartSoft owns the session) and --capture logs a
SmartSoft session, including what SmartSoft sends ("sent" lines).

Wire protocol, all little-endian. Every frame, both ways: [op:1][status:1][reserved:2]
[length:4] then `length` bytes. The DLL sends one request at a time on a connection; each gets
one reply with op | 0x80:

  HELLO  0x01  [version:2][flags:2]       -> [version:2][vid:2][pid:2][bcd:2][product, ASCII]
  WRITE  0x02  [data]                     -> [written:4]
  READ   0x03  [max_len:4][timeout_ms:4]  -> [data]

Status: 0 ok, 1 timeout (READ: nothing arrived), 2 no console, 3 I/O error, 4 bad request.
HELLO flag 1 starts a session: replies left on the console by an earlier, interrupted session
are drained first. Without it, HELLO only asks whether the console is there. One WRITE / READ is
one bulk transfer on the console's vendor interface (OUT / IN endpoint), so transfer
boundaries -- which the protocol's header/payload framing relies on -- are kept.
"""
import select
import socket
import struct
import sys
import threading
import time

import usb.core
import usb.util

from . import protocol as sfl

PROTOCOL_VERSION = 1
DEFAULT_PORT = 47561

FRAME = struct.Struct("<BBHI")
OP_HELLO, OP_WRITE, OP_READ = 0x01, 0x02, 0x03
REPLY = 0x80
ST_OK, ST_TIMEOUT, ST_NO_DEVICE, ST_IO_ERROR, ST_BAD_REQUEST = range(5)
HELLO_SESSION_START = 0x0001

MAX_FRAME = 1 << 20      # bigger than any transfer the console makes (~2 KB)
MAX_READ_TIMEOUT_MS = 5000
WRITE_TIMEOUT_MS = 1000
DRAIN_TIMEOUT_MS = 20    # per read while draining stale replies at session start
DRAIN_MAX_READS = 64
REOPEN_INTERVAL_S = 2.0


def log(msg):
    print(f"[smartsoft-bridge] {msg}", file=sys.stderr)


def pack_frame(op, status=ST_OK, payload=b""):
    return FRAME.pack(op, status, 0, len(payload)) + payload


def recv_exact(sock, n):
    """n bytes from sock, or None if the peer closed the connection first."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    """(op, status, payload), or None when the peer has closed the connection."""
    header = recv_exact(sock, FRAME.size)
    if header is None:
        return None
    op, status, _, length = FRAME.unpack(header)
    if length > MAX_FRAME:
        raise ValueError(f"frame too long: {length} bytes")
    payload = recv_exact(sock, length) if length else b""
    if payload is None:
        return None
    return op, status, payload


class StreamTap:
    """Decodes the relayed bulk transfers into messages, for the web page and --capture.

    Calls on_message(kind, obj_type, data) for each complete payload: kind "payload" or
    "announce" (type 0x28) for the console's, "sent" for SmartSoft's. A header announces the
    length of the payload that follows on the same endpoint, so a transfer of exactly that
    length right after it is the payload, even when it is 12 bytes long like a header."""

    def __init__(self, on_message):
        self.on_message = on_message
        self.out_expected = 0
        self.in_expected = 0

    def out(self, data):
        if self.out_expected and len(data) == self.out_expected:
            self.out_expected = 0
            if len(data) >= 4:   # [seq:1][objLen:2][type:1][data]
                obj_len = struct.unpack_from("<H", data, 1)[0]
                self.on_message("sent", data[3], bytes(data[4:4 + obj_len]))
            return
        self.out_expected = 0
        if len(data) == sfl.HEADER_LEN:
            msg_type, payload_len = struct.unpack_from("<HH", data, 0)
            if msg_type == 1 and payload_len:
                self.out_expected = payload_len

    def inn(self, data):
        if self.in_expected and len(data) == self.in_expected:
            self.in_expected = 0
            if len(data) >= 5:   # [seq:2][objLen:2][type:1][data]
                obj_len = struct.unpack_from("<H", data, 2)[0]
                obj_type = data[4]
                self.on_message("announce" if obj_type == 0x28 else "payload", obj_type,
                                bytes(data[5:5 + obj_len]))
            return
        self.in_expected = 0
        if len(data) == sfl.HEADER_LEN:
            payload_len = struct.unpack_from("<H", data, 2)[0]
            self.in_expected = payload_len


def _is_timeout(e):
    return isinstance(e, usb.core.USBTimeoutError)


class UsbConsole:
    """The console's vendor interface, claimed while the bridge runs. Calls on_change(identity)
    when it connects ((vid, pid, bcd, product)) or goes away (None)."""

    def __init__(self, usb_backend=None, on_change=None):
        self.usb_backend = usb_backend
        self.on_change = on_change or (lambda identity: None)
        self.dev = None
        self.intf_num = self.ep_in = self.ep_out = None
        self.identity = None
        self._reported = None   # last state printed, to print each change once

    @property
    def is_open(self):
        return self.dev is not None

    def _report(self, what, text):
        if self._reported != what:
            log(text)
            self._reported = what

    def open(self):
        dev = usb.core.find(idVendor=sfl.VENDOR_ID, idProduct=sfl.PRODUCT_ID,
                            backend=self.usb_backend)
        found = sfl.find_bulk_interface(dev) if dev is not None else None
        if found is None:
            self._report("absent", "no console found, waiting...")
            return False
        intf_num, _alt, ep_in, ep_out = found
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
            usb.util.dispose_resources(dev)
            self._report("busy", f"console found but can't be claimed ({e}): in use by another "
                                 "program? waiting...")
            return False
        try:
            product = dev.product or ""
        except (usb.core.USBError, ValueError):
            product = ""
        self.dev, self.intf_num, self.ep_in, self.ep_out = dev, intf_num, ep_in, ep_out
        self.identity = (dev.idVendor, dev.idProduct, dev.bcdDevice, product)
        self._report("connected", f"console connected: {product!r}")
        self.on_change(self.identity)
        return True

    def close(self):
        if self.dev is None:
            return
        try:
            usb.util.release_interface(self.dev, self.intf_num)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self.dev)
        self.dev = self.identity = None
        self._report("lost", "console disconnected")
        self.on_change(None)

    def write(self, data):
        """(status, bytes written)"""
        if self.dev is None:
            return ST_NO_DEVICE, 0
        try:
            return ST_OK, self.dev.write(self.ep_out.bEndpointAddress, data,
                                         timeout=WRITE_TIMEOUT_MS)
        except usb.core.USBError as e:
            if _is_timeout(e):
                return ST_TIMEOUT, 0
            log(f"write failed ({e}), releasing the console")
            self.close()
            return ST_IO_ERROR, 0

    def read(self, max_len, timeout_ms):
        """(status, data)"""
        if self.dev is None:
            return ST_NO_DEVICE, b""
        try:
            data = bytes(self.dev.read(self.ep_in.bEndpointAddress, max_len,
                                       timeout=max(1, timeout_ms)))
        except usb.core.USBError as e:
            if _is_timeout(e):
                return ST_TIMEOUT, b""
            log(f"read failed ({e}), releasing the console")
            self.close()
            return ST_IO_ERROR, b""
        return (ST_OK, data) if data else (ST_TIMEOUT, b"")

    def drain(self):
        """Discard replies an interrupted session left on the console; returns how many."""
        drained = 0
        while drained < DRAIN_MAX_READS:
            status, _ = self.read(64 * 64, DRAIN_TIMEOUT_MS)
            if status != ST_OK:
                break
            drained += 1
        return drained


class BridgeServer:
    """Listens for the SmartSoft DLL on localhost and relays its transfers to the console.
    Binding happens in the constructor (OSError if the port is taken), so a caller can report
    that before starting serve_forever() on a thread."""

    def __init__(self, port=DEFAULT_PORT, usb_backend=None, on_message=None, on_console=None,
                 host="127.0.0.1", console=None):
        self.sock = socket.create_server((host, port))
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()   # one USB transfer at a time, and the tap's state
        self.console = console or UsbConsole(usb_backend, on_console)
        # on_message(kind, obj_type, data) for each relayed message (see StreamTap); can be set
        # after construction.
        self.on_message = on_message or (lambda kind, obj_type, data: None)
        self.tap = StreamTap(self._emit)

    def _emit(self, kind, obj_type, data):
        self.on_message(kind, obj_type, data)

    def handle_request(self, op, payload):
        """The reply frame for one request frame."""
        if op == OP_HELLO:
            if len(payload) < 4:
                return pack_frame(op | REPLY, ST_BAD_REQUEST)
            _version, flags = struct.unpack_from("<HH", payload, 0)
            with self.lock:
                if self.console.identity is None:
                    return pack_frame(op | REPLY, ST_NO_DEVICE)
                if flags & HELLO_SESSION_START:
                    drained = self.console.drain()
                    self.tap = StreamTap(self._emit)
                    log("SmartSoft session started"
                        + (f" ({drained} stale replies discarded)" if drained else ""))
                vid, pid, bcd, product = self.console.identity
            return pack_frame(op | REPLY, ST_OK,
                              struct.pack("<HHHH", PROTOCOL_VERSION, vid, pid, bcd)
                              + product.encode("ascii", "replace"))
        if op == OP_WRITE:
            with self.lock:
                status, written = self.console.write(payload)
                if status == ST_OK:
                    self.tap.out(payload[:written])
            return pack_frame(op | REPLY, status, struct.pack("<I", written))
        if op == OP_READ:
            if len(payload) != 8:
                return pack_frame(op | REPLY, ST_BAD_REQUEST)
            max_len, timeout_ms = struct.unpack("<II", payload)
            if not 0 < max_len <= MAX_FRAME:
                return pack_frame(op | REPLY, ST_BAD_REQUEST)
            with self.lock:
                status, data = self.console.read(max_len, min(timeout_ms, MAX_READ_TIMEOUT_MS))
                if status == ST_OK:
                    self.tap.inn(data)
            return pack_frame(op | REPLY, status, data)
        return pack_frame(op | REPLY, ST_BAD_REQUEST)

    def _serve_client(self, conn, stop_event):
        with conn:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(None)
            while not stop_event.is_set():
                # Wait for a request with a timeout (to notice stop_event), then read the whole
                # frame blocking: a timeout in the middle of a frame would lose its start.
                if not select.select([conn], [], [], 1.0)[0]:
                    continue
                try:
                    frame = read_frame(conn)
                except (OSError, ValueError):
                    break
                if frame is None:
                    break
                op, _status, payload = frame
                try:
                    conn.sendall(self.handle_request(op, payload))
                except OSError:
                    break

    def serve_forever(self, stop_event):
        last_open_try = 0.0
        try:
            while not stop_event.is_set():
                if not self.console.is_open and time.monotonic() - last_open_try >= REOPEN_INTERVAL_S:
                    last_open_try = time.monotonic()
                    with self.lock:
                        self.console.open()
                try:
                    conn, _addr = self.sock.accept()
                except TimeoutError:
                    continue
                threading.Thread(target=self._serve_client, args=(conn, stop_event),
                                 name="smartsoft-client", daemon=True).start()
        finally:
            self.sock.close()
            with self.lock:
                self.console.close()

    def close(self):
        """Release the port without serving (when start-up fails after binding)."""
        self.sock.close()
