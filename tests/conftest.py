"""Shared test setup: provides `capture(name)`, a loader for the recorded-traffic fixtures in
tests/fixtures/. (The `consolelink` package itself is found through `pythonpath = ["src"]` in
pyproject.toml, or an installed copy.)

Fixture format -- the same line format `consolelink --capture` writes:

    # free-text comments: what the console was doing
    # @tag: names the message on the next line
    t=  29.748 acked    type=0x16 len=637 raw=6a46...
"""
import os
import re
from dataclasses import dataclass

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO, "tests", "fixtures")

_LINE_RE = re.compile(r"t=\s*([\d.]+)\s+(\S+)\s+type=0x([0-9a-f]+) len=(\d+) raw=([0-9a-f]*)$")
_TAG_RE = re.compile(r"#\s*@([\w-]+):")


@dataclass
class Message:
    t: float
    kind: str  # "requested" / "announce" / "acked" / "payload"
    type: int
    data: bytes
    tag: str | None


class Capture:
    def __init__(self, messages):
        self.messages = messages

    def tagged(self, tag):
        found = [m for m in self.messages if m.tag == tag]
        assert len(found) == 1, f"expected exactly one message tagged @{tag}, found {len(found)}"
        return found[0]

    def of_type(self, obj_type):
        return [m for m in self.messages if m.type == obj_type]


def load_capture(name):
    messages, tag = [], None
    with open(os.path.join(FIXTURES, name)) as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if line.startswith("#"):
                m = _TAG_RE.match(line)
                if m:
                    tag = m.group(1)
                continue
            m = _LINE_RE.match(line)
            assert m, f"{name}:{n}: not a capture line"
            data = bytes.fromhex(m.group(5))
            assert len(data) == int(m.group(4)), f"{name}:{n}: len= doesn't match raw="
            messages.append(Message(float(m.group(1)), m.group(2), int(m.group(3), 16), data, tag))
            tag = None
    return Capture(messages)


@pytest.fixture
def capture():
    return load_capture


@pytest.fixture
def bridge_platform(monkeypatch):
    """Run as on macOS, where the SmartSoft bridge is allowed: Settings.validate() refuses it on
    Windows (SmartSoft runs natively there), so bridge tests would otherwise fail on Windows CI."""
    import sys
    monkeypatch.setattr(sys, "platform", "darwin")
