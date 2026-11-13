"""SmartSoft bridge: the TCP relay between the CrossOver DLL and the console (smartsoft_bridge.py)."""
import socket
import struct
import sys
import threading

import pytest

from consolelink import app
from consolelink import smartsoft_bridge as sb


class FakeConsole:
    """Stands in for UsbConsole: records writes, serves queued reads."""

    def __init__(self, identity=(0x14D5, 0x0201, 0x0001, "ETC Smartfade")):
        self.identity = identity
        self.written = []
        self.reads = []          # (status, data) to return, in order
        self.drained = 0

    @property
    def is_open(self):
        return self.identity is not None

    def open(self):
        return self.is_open

    def close(self):
        pass

    def write(self, data):
        self.written.append(bytes(data))
        return sb.ST_OK, len(data)

    def read(self, max_len, timeout_ms):
        if not self.reads:
            return sb.ST_TIMEOUT, b""
        status, data = self.reads.pop(0)
        return status, data[:max_len]

    def drain(self):
        self.drained += 1
        return 0


def header(msg_type, payload_len=0, state=(0, 0, 0, 0)):
    return struct.pack("<HHHHHH", msg_type, payload_len, *state)


def in_payload(seq, obj_type, data):
    return struct.pack("<HH", seq, len(data)) + bytes([obj_type]) + data


def out_payload(seq, obj_type, data):
    return bytes([seq]) + struct.pack("<H", len(data)) + bytes([obj_type]) + data


def unpack_reply(frame):
    op, status, _, length = sb.FRAME.unpack_from(frame)
    payload = frame[sb.FRAME.size:]
    assert len(payload) == length
    return op, status, payload


@pytest.fixture
def server():
    srv = sb.BridgeServer(port=0, console=FakeConsole())
    messages = []
    srv.on_message = lambda kind, obj_type, data: messages.append((kind, obj_type, data))
    srv.messages = messages
    yield srv
    srv.close()


# ---- frames

def test_frame_round_trip_over_a_socket():
    a, b = socket.socketpair()
    with a, b:
        a.sendall(sb.pack_frame(sb.OP_WRITE, sb.ST_OK, b"\x01\x02\x03"))
        a.sendall(sb.pack_frame(sb.OP_HELLO | sb.REPLY, sb.ST_NO_DEVICE))
        assert sb.read_frame(b) == (sb.OP_WRITE, sb.ST_OK, b"\x01\x02\x03")
        assert sb.read_frame(b) == (sb.OP_HELLO | sb.REPLY, sb.ST_NO_DEVICE, b"")
        a.close()
        assert sb.read_frame(b) is None


def test_read_frame_rejects_an_oversized_length():
    a, b = socket.socketpair()
    with a, b:
        a.sendall(sb.FRAME.pack(sb.OP_WRITE, 0, 0, sb.MAX_FRAME + 1))
        with pytest.raises(ValueError):
            sb.read_frame(b)


# ---- the tap that decodes relayed transfers

def test_tap_decodes_console_payloads_and_announces():
    seen = []
    tap = sb.StreamTap(lambda *m: seen.append(m))
    version = bytes([12]) + b"3.0.2.9.0.43"
    tap.inn(header(4))                      # idle reply: nothing follows
    tap.inn(header(3, 5 + 7))
    tap.inn(in_payload(202, 0x28, bytes([0, 1, 0x25, 0, 0, 0, 0])))
    tap.inn(header(3, 5 + len(version)))
    tap.inn(in_payload(203, 0x25, version))
    assert seen == [("announce", 0x28, bytes([0, 1, 0x25, 0, 0, 0, 0])),
                    ("payload", 0x25, version)]


def test_tap_takes_a_12_byte_payload_as_payload_not_header():
    seen = []
    tap = sb.StreamTap(lambda *m: seen.append(m))
    data = bytes(range(7))
    tap.inn(header(3, 12))
    tap.inn(in_payload(1, 0x17, data))      # 5 + 7 = 12 bytes, header-sized
    assert seen == [("payload", 0x17, data)]


def test_tap_decodes_what_smartsoft_sends():
    seen = []
    tap = sb.StreamTap(lambda *m: seen.append(m))
    tap.out(header(0))
    tap.out(header(1, 7))
    tap.out(out_payload(0, 0x27, bytes([0x09, 0, 0])))
    assert seen == [("sent", 0x27, bytes([0x09, 0, 0]))]


def test_tap_resyncs_after_a_transfer_of_the_wrong_length():
    seen = []
    tap = sb.StreamTap(lambda *m: seen.append(m))
    tap.inn(header(3, 20))
    tap.inn(bytes(5))                       # not the announced length: dropped
    tap.inn(header(3, 6))
    tap.inn(in_payload(1, 0x18, bytes([0])))
    assert seen == [("payload", 0x18, bytes([0]))]


# ---- requests

def test_hello_reports_the_console(server):
    op, status, payload = unpack_reply(
        server.handle_request(sb.OP_HELLO, struct.pack("<HH", 1, 0)))
    assert (op, status) == (sb.OP_HELLO | sb.REPLY, sb.ST_OK)
    assert struct.unpack_from("<HHHH", payload) == (sb.PROTOCOL_VERSION, 0x14D5, 0x0201, 0x0001)
    assert payload[8:] == b"ETC Smartfade"
    assert server.console.drained == 0      # a presence check doesn't touch the console


def test_hello_session_start_drains_stale_replies(server):
    server.handle_request(sb.OP_HELLO, struct.pack("<HH", 1, sb.HELLO_SESSION_START))
    assert server.console.drained == 1


def test_hello_without_console(server):
    server.console.identity = None
    _, status, payload = unpack_reply(server.handle_request(sb.OP_HELLO, struct.pack("<HH", 1, 0)))
    assert (status, payload) == (sb.ST_NO_DEVICE, b"")


def test_write_and_read_relay_and_feed_the_tap(server):
    _, status, payload = unpack_reply(server.handle_request(sb.OP_WRITE, header(0)))
    assert (status, payload) == (sb.ST_OK, struct.pack("<I", 12))
    assert server.console.written == [header(0)]

    data = bytes([0x18]) + bytes(98)
    server.console.reads = [(sb.ST_OK, header(3, 5 + len(data))),
                            (sb.ST_OK, in_payload(7, 0x0e, data))]
    _, status, got = unpack_reply(server.handle_request(sb.OP_READ, struct.pack("<II", 12, 500)))
    assert (status, got) == (sb.ST_OK, header(3, 5 + len(data)))
    _, status, got = unpack_reply(
        server.handle_request(sb.OP_READ, struct.pack("<II", 5 + len(data), 500)))
    assert status == sb.ST_OK and len(got) == 5 + len(data)
    assert server.messages == [("payload", 0x0e, data)]


def test_read_timeout_and_bad_requests(server):
    _, status, payload = unpack_reply(server.handle_request(sb.OP_READ, struct.pack("<II", 12, 5)))
    assert (status, payload) == (sb.ST_TIMEOUT, b"")
    for op, payload in ((sb.OP_READ, b"\x00"), (sb.OP_READ, struct.pack("<II", 0, 5)),
                        (sb.OP_HELLO, b""), (0x7f, b"")):
        assert unpack_reply(server.handle_request(op, payload))[1] == sb.ST_BAD_REQUEST


def test_relays_over_tcp(server):
    stop = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(stop,))
    thread.start()
    try:
        server.console.reads = [(sb.ST_OK, header(4))]
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as c:
            c.sendall(sb.pack_frame(sb.OP_HELLO, payload=struct.pack("<HH", 1, 1)))
            assert sb.read_frame(c)[:2] == (sb.OP_HELLO | sb.REPLY, sb.ST_OK)
            c.sendall(sb.pack_frame(sb.OP_WRITE, payload=header(0)))
            assert sb.read_frame(c) == (sb.OP_WRITE | sb.REPLY, sb.ST_OK, struct.pack("<I", 12))
            c.sendall(sb.pack_frame(sb.OP_READ, payload=struct.pack("<II", 12, 500)))
            assert sb.read_frame(c) == (sb.OP_READ | sb.REPLY, sb.ST_OK, header(4))
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert server.console.written == [header(0)]


# ---- command line and settings

def test_bridge_and_page_control_are_exclusive(bridge_platform):
    with pytest.raises(ValueError):
        app.Settings(smartsoft_bridge=sb.DEFAULT_PORT, allow_write=True).validate()
    app.Settings(smartsoft_bridge=sb.DEFAULT_PORT).validate()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_bridge_allowed_off_windows(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    app.Settings(smartsoft_bridge=sb.DEFAULT_PORT).validate()


def test_bridge_refused_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError, match="on Windows, run SmartSoft directly"):
        app.Settings(smartsoft_bridge=sb.DEFAULT_PORT).validate()
    app.Settings().validate()   # without the bridge, Windows is fine
    parser = app.build_parser()
    with pytest.raises(SystemExit):   # the command line reports it through parser.error
        app.check_args(parser, parser.parse_args(["--smartsoft-bridge"]))


def test_command_line_option(bridge_platform):
    parser = app.build_parser()
    args = parser.parse_args(["--smartsoft-bridge", "--no-web"])
    app.check_args(parser, args)   # --no-web is fine: relaying is something to do
    assert app.Settings.from_args(args).smartsoft_bridge == sb.DEFAULT_PORT
    args = parser.parse_args(["--smartsoft-bridge", "5000"])
    assert app.Settings.from_args(args).smartsoft_bridge == 5000
    assert app.Settings.from_args(parser.parse_args([])).smartsoft_bridge is None


def test_service_binds_and_releases_the_bridge_port(bridge_platform):
    service = app.Service(app.Settings(web=False, smartsoft_bridge=0)).start()
    try:
        port = service.bridge_port
        assert port
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(sb.pack_frame(sb.OP_HELLO, payload=struct.pack("<HH", 1, 0)))
            assert sb.read_frame(c)[0] == sb.OP_HELLO | sb.REPLY
    finally:
        service.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_hello_from_another_protocol_version_is_refused(server, capsys):
    for _ in range(2):
        op, status, payload = unpack_reply(
            server.handle_request(sb.OP_HELLO, struct.pack("<HH", sb.PROTOCOL_VERSION + 1, 1)))
        assert (op, status) == (sb.OP_HELLO | sb.REPLY, sb.ST_VERSION_MISMATCH)
        assert payload == struct.pack("<H", sb.PROTOCOL_VERSION)
    assert server.console.drained == 0   # no session was started
    err = capsys.readouterr().err
    assert err.count("consolelink smartsoft install") == 1   # reported once, not per HELLO
