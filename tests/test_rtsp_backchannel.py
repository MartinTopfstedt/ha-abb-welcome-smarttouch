"""RTSP backchannel (two-way audio) tests against a real TCP connection.

go2rtc asks for the ONVIF backchannel in DESCRIBE, SETUPs the sendonly
track on a channel of its choice and then sends microphone RTP as
interleaved frames on the control connection.
"""

from __future__ import annotations

import asyncio
import importlib.util
import struct
import sys
from pathlib import Path

_FILE = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "abb_welcome" / "rtsp_server.py"
)

_spec = importlib.util.spec_from_file_location("abb_rtsp_server_bc", _FILE)
rtsp_server = importlib.util.module_from_spec(_spec)
sys.modules["abb_rtsp_server_bc"] = rtsp_server
_spec.loader.exec_module(rtsp_server)


async def _request(reader, writer, lines: list[str]) -> str:
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    head = (await reader.readuntil(b"\r\n\r\n")).decode()
    length = 0
    for line in head.split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1])
    body = (await reader.readexactly(length)).decode() if length else ""
    return head + body


def test_backchannel_frames_reach_callback() -> None:
    async def run() -> None:
        describes: list[bool] = []
        received: list[tuple[str, bytes]] = []
        torn_down: list[str] = []

        async def on_describe(backchannel: bool) -> str:
            describes.append(backchannel)
            return "v=0\r\n"

        async def on_play(sess) -> None:
            return None

        async def on_teardown(sess) -> None:
            torn_down.append(sess.session_id)

        server = rtsp_server.RtspServer(
            host="127.0.0.1",
            on_describe=on_describe,
            on_play=on_play,
            on_teardown=on_teardown,
            on_backchannel=lambda sess, rtp: received.append((sess.session_id, rtp)),
        )
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        base = server.url

        resp = await _request(reader, writer, [
            f"DESCRIBE {base} RTSP/1.0", "CSeq: 1",
            "Require: www.onvif.org/ver20/backchannel",
        ])
        assert resp.startswith("RTSP/1.0 200")
        await _request(reader, writer, [f"DESCRIBE {base} RTSP/1.0", "CSeq: 2"])
        assert describes == [True, False]

        resp = await _request(reader, writer, [
            f"SETUP {base}trackID=0 RTSP/1.0", "CSeq: 3",
            "Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
        ])
        assert "interleaved=0-1" in resp
        resp = await _request(reader, writer, [
            f"SETUP {base}trackID=2 RTSP/1.0", "CSeq: 4",
            "Transport: RTP/AVP/TCP;unicast;interleaved=4-5",
        ])
        assert "interleaved=4-5" in resp
        resp = await _request(reader, writer, [f"PLAY {base} RTSP/1.0", "CSeq: 5"])
        assert resp.startswith("RTSP/1.0 200")

        rtp = struct.pack("!BBHII", 0x80, 8, 1, 160, 7) + b"\xd5" * 160
        rtcp = struct.pack("!BBHI", 0x80, 201, 1, 7)
        writer.write(struct.pack("!cBH", b"$", 4, len(rtp)) + rtp)
        writer.write(struct.pack("!cBH", b"$", 5, len(rtcp)) + rtcp)
        writer.write(struct.pack("!cBH", b"$", 1, len(rtcp)) + rtcp)
        # A stray line break between requests must not close the session.
        writer.write(b"\r\n")
        await writer.drain()
        resp = await _request(reader, writer, [f"OPTIONS {base} RTSP/1.0", "CSeq: 6"])
        assert resp.startswith("RTSP/1.0 200")

        assert len(received) == 1
        session_id, payload = received[0]
        assert payload == rtp
        assert server.session_count == 1
        sess = next(iter(server._sessions.values()))
        assert sess.has_video and not sess.has_audio
        assert sess.backchannel_channel == 4

        writer.close()
        await writer.wait_closed()
        for _ in range(50):
            if torn_down:
                break
            await asyncio.sleep(0.01)
        assert torn_down == [session_id]
        await server.stop()

    asyncio.run(run())


if __name__ == "__main__":
    test_backchannel_frames_reach_callback()
    print("OK")
