"""Tests for declining a ringing door-station INVITE.

The listener module is loaded directly from its file so the suite runs
without Home Assistant installed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "abb_welcome"


def _load(name: str) -> types.ModuleType:
    pkg = types.ModuleType("abb_welcome")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules.setdefault("abb_welcome", pkg)
    for mod in ("intercom_dialer", name):
        full = f"abb_welcome.{mod}"
        if full in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{mod}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
    return sys.modules[f"abb_welcome.{name}"]


sl = _load("sip_listener")


class _FakeWriter:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def _invite(station: str, call_id: str, received_at: float) -> object:
    headers = [
        ("Via", "SIP/2.0/TLS 192.168.10.22:5061;branch=z9hG4bK-1"),
        ("From", f"<sip:{station}@gw>;tag=abc"),
        ("To", "<sip:user@gw>"),
        ("Call-ID", call_id),
        ("CSeq", "1 INVITE"),
    ]
    frame = sl._SipFrame(
        start_line="INVITE sip:user@gw SIP/2.0", headers=headers, body=b"", raw=b""
    )
    setattr(frame, "_received_at", received_at)
    return frame


def _listener() -> tuple[object, _FakeWriter]:
    listener = sl.SipListener("192.168.10.22", "user", "pw", "gw")
    writer = _FakeWriter()
    listener._writer = writer
    return listener, writer


def test_reject_pending_call_by_station_sends_603() -> None:
    async def run() -> None:
        listener, writer = _listener()
        now = time.time()
        listener._open_invites = {
            "c1": _invite("100000001", "c1", now - 2),
            "c2": _invite("100000002", "c2", now - 1),
        }
        assert await listener.reject_pending_call("100000001") == "c1"
        wire = writer.sent[-1].decode()
        assert wire.startswith("SIP/2.0 603 Decline\r\n")
        assert "Call-ID: c1\r\n" in wire
        assert "CSeq: 1 INVITE\r\n" in wire
        assert "To: <sip:user@gw>;tag=" in wire
        assert list(listener._open_invites) == ["c2"]

    asyncio.run(run())


def test_reject_without_station_picks_most_recent() -> None:
    async def run() -> None:
        listener, writer = _listener()
        now = time.time()
        listener._open_invites = {
            "old": _invite("100000001", "old", now - 30),
            "new": _invite("100000002", "new", now - 1),
            "stale": _invite("100000003", "stale", now - 120),
        }
        assert await listener.reject_pending_call() == "new"
        assert "Call-ID: new\r\n" in writer.sent[-1].decode()
        assert list(listener._open_invites) == ["old"]

    asyncio.run(run())


def test_reject_without_pending_call_returns_none() -> None:
    async def run() -> None:
        listener, writer = _listener()
        assert await listener.reject_pending_call() is None
        assert await listener.reject_pending_call("100000001") is None
        assert writer.sent == []

    asyncio.run(run())


if __name__ == "__main__":
    test_reject_pending_call_by_station_sends_603()
    test_reject_without_station_picks_most_recent()
    test_reject_without_pending_call_returns_none()
    print("OK")
