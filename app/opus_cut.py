"""Cut recorded Opus audio without re-encoding, in pure Python.

The browsers record the radio in short Opus segments (WebM for Chrome / Edge,
Ogg for Firefox). To hand out one file per worked callsign, the Opus packets
are taken out of their container, kept when they fall inside the wanted time
windows, and written again as a single Ogg Opus file (RFC 7845). No decoding,
no loss, no ffmpeg: the cut is precise to one packet (20 ms).

- ``read_packets(data)``: (OpusHead, [packets]) of a WebM or Ogg segment;
- ``packet_samples(packet)``: its duration, in samples at 48 kHz (RFC 6716);
- ``cut(segments, windows)``: the Ogg Opus file of the windows.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

RATE = 48_000                       # Opus timestamps are always 48 kHz

# ── Opus packets (RFC 6716, section 3.1) ───────────────────────────────────

# Frame duration of each TOC configuration, in samples at 48 kHz.
_SILK = (480, 960, 1920, 2880)      # 10, 20, 40, 60 ms
_HYBRID = (480, 960)                # 10, 20 ms
_CELT = (120, 240, 480, 960)        # 2.5, 5, 10, 20 ms


def packet_samples(packet: bytes) -> int:
    """Duration of an Opus packet, in 48 kHz samples (0 if malformed)."""
    if not packet:
        return 0
    toc = packet[0]
    config = toc >> 3
    if config < 12:
        frame = _SILK[config % 4]
    elif config < 16:
        frame = _HYBRID[config % 2]
    else:
        frame = _CELT[config % 4]
    code = toc & 0x03
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    else:
        if len(packet) < 2:
            return 0
        frames = packet[1] & 0x3F
    return frame * frames


def default_head(channels: int = 1, pre_skip: int = 312) -> bytes:
    return b"OpusHead" + struct.pack("<BBHIhB", 1, channels, pre_skip, RATE, 0, 0)


# ── WebM (Matroska / EBML) ─────────────────────────────────────────────────

_EBML_CONTAINERS = {
    0x18538067,   # Segment
    0x1F43B675,   # Cluster
    0x1654AE6B,   # Tracks
    0xAE,         # TrackEntry
    0xA0,         # BlockGroup
}
_CODEC_PRIVATE = 0x63A2
_SIMPLE_BLOCK = 0xA3
_BLOCK = 0xA1


def _vint(data: bytes, pos: int, keep_marker: bool) -> tuple[int, int, bool]:
    """EBML variable-length integer → (value, new position, all ones)."""
    first = data[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(data):
        raise ValueError("EBML: bad variable-length integer")
    value = first if keep_marker else first & (mask - 1)
    all_ones = (first & (mask - 1)) == mask - 1
    for b in data[pos + 1: pos + length]:
        value = (value << 8) | b
        all_ones = all_ones and b == 0xFF
    return value, pos + length, all_ones


def _block_frames(payload: bytes) -> list[bytes]:
    """Frames of a (Simple)Block: no lacing, Xiph or fixed-size lacing."""
    _track, pos, _ = _vint(payload, 0, keep_marker=False)
    flags = payload[pos + 2]
    pos += 3
    lacing = (flags >> 1) & 0x03
    if lacing == 0:
        return [payload[pos:]]
    count = payload[pos] + 1
    pos += 1
    if lacing == 2:                                        # fixed size
        size = (len(payload) - pos) // count
        return [payload[pos + i * size: pos + (i + 1) * size] for i in range(count)]
    if lacing == 1:                                        # Xiph
        sizes = []
        for _i in range(count - 1):
            size = 0
            while True:
                b = payload[pos]
                pos += 1
                size += b
                if b != 255:
                    break
            sizes.append(size)
        sizes.append(len(payload) - pos - sum(sizes))
        frames = []
        for size in sizes:
            frames.append(payload[pos: pos + size])
            pos += size
        return frames
    raise ValueError("WebM: EBML lacing not supported")


def _webm_packets(data: bytes) -> tuple[bytes | None, list[bytes]]:
    head: bytes | None = None
    packets: list[bytes] = []
    pos = 0
    while pos < len(data):
        elem_id, pos, _ = _vint(data, pos, keep_marker=True)
        size, pos, unknown = _vint(data, pos, keep_marker=False)
        if elem_id in _EBML_CONTAINERS:
            continue                                       # step inside (size may be unknown)
        if unknown:
            break
        body = data[pos: pos + size]
        if elem_id == _CODEC_PRIVATE and body.startswith(b"OpusHead"):
            head = body
        elif elem_id in (_SIMPLE_BLOCK, _BLOCK):
            packets.extend(f for f in _block_frames(body) if f)
        pos += size
    return head, packets


# ── Ogg (RFC 3533) ─────────────────────────────────────────────────────────


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        table.append(r & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) & 0xFF) ^ b]
    return crc


def ogg_pages(data: bytes) -> list[dict]:
    """Pages of an Ogg stream, CRC checked (ValueError otherwise)."""
    pages = []
    pos = 0
    while pos < len(data):
        if data[pos: pos + 4] != b"OggS":
            raise ValueError("Ogg: page expected")
        header_type, granule, serial, seq, crc, count = struct.unpack_from("<BqIIIB", data, pos + 5)
        lacing = data[pos + 27: pos + 27 + count]
        start = pos + 27 + count
        end = start + sum(lacing)
        page = bytearray(data[pos:end])
        page[22:26] = b"\0\0\0\0"
        if ogg_crc(bytes(page)) != crc:
            raise ValueError("Ogg: bad CRC")
        pages.append({"type": header_type, "granule": granule, "serial": serial, "seq": seq,
                      "lacing": lacing, "body": data[start:end]})
        pos = end
    return pages


def _ogg_packets(data: bytes) -> tuple[bytes | None, list[bytes]]:
    packets: list[bytes] = []
    current = b""
    for page in ogg_pages(data):
        body, pos = page["body"], 0
        for value in page["lacing"]:
            current += body[pos: pos + value]
            pos += value
            if value < 255:
                packets.append(current)
                current = b""
    head = packets[0] if packets and packets[0].startswith(b"OpusHead") else None
    audio = [p for p in packets if not p.startswith((b"OpusHead", b"OpusTags"))]
    return head, audio


def read_packets(data: bytes) -> tuple[bytes | None, list[bytes]]:
    """(OpusHead or None, audio packets) of a WebM or Ogg Opus segment."""
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return _webm_packets(data)
    if data.startswith(b"OggS"):
        return _ogg_packets(data)
    raise ValueError("unknown container")


class _OggWriter:
    MAX_LACING = 255

    def __init__(self, serial: int = 0x544D4143) -> None:   # "TMAC"
        self.out = bytearray()
        self.serial = serial
        self.seq = 0

    def page(self, packets: list[bytes], granule: int, header_type: int = 0) -> None:
        lacing = bytearray()
        for p in packets:
            lacing += b"\xff" * (len(p) // 255) + bytes([len(p) % 255])
        header = struct.pack("<4sBBqIIIB", b"OggS", 0, header_type, granule, self.serial, self.seq, 0, len(lacing))
        page = bytearray(header + lacing + b"".join(packets))
        page[22:26] = struct.pack("<I", ogg_crc(bytes(page)))
        self.out += page
        self.seq += 1


def _tags(title: str) -> bytes:
    vendor = b"tm-activation"
    comments = [f"TITLE={title}".encode()] if title else []
    body = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
    for c in comments:
        body += struct.pack("<I", len(c)) + c
    return body


@dataclass
class Segment:
    start_ms: int          # UTC, server clock
    data: bytes            # WebM or Ogg Opus


def cut(segments: list[Segment], windows: list[tuple[int, int]], title: str = "") -> bytes | None:
    """Ogg Opus file of the audio inside the windows [from_ms, to_ms[, in order.

    Overlapping windows are merged; packets repeated where two recordings
    overlap (the browser starts the next recorder before stopping the
    previous one) are skipped. None if no packet falls in the windows."""
    merged: list[list[int]] = []
    for frm, to in sorted(windows):
        if merged and frm <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], to)
        else:
            merged.append([frm, to])
    decoded = []
    head = None
    for seg in sorted(segments, key=lambda s: s.start_ms):
        try:
            h, packets = read_packets(seg.data)
        except (ValueError, IndexError, struct.error):
            continue                                        # damaged segment: skipped
        head = head or h
        decoded.append((seg.start_ms, packets))
    kept: list[bytes] = []
    for frm, to in merged:
        last_end = frm
        for start_ms, packets in decoded:
            t = float(start_ms)
            for p in packets:
                ms = packet_samples(p) * 1000 / RATE
                if t >= last_end - 1 and t < to and t + ms > frm and ms:
                    kept.append(p)
                    last_end = t + ms
                t += ms
                if t >= to:
                    break
    if not kept:
        return None
    head = head or default_head()
    pre_skip = struct.unpack_from("<H", head, 10)[0] if len(head) >= 12 else 312
    w = _OggWriter()
    w.page([head], 0, header_type=0x02)                     # beginning of stream
    w.page([_tags(title)], 0)
    granule, batch, lacing = pre_skip, [], 0
    for i, p in enumerate(kept):
        need = len(p) // 255 + 1
        if batch and (lacing + need > _OggWriter.MAX_LACING or len(batch) >= 50):
            w.page(batch, granule)
            batch, lacing = [], 0
        batch.append(p)
        lacing += need
        granule += packet_samples(p)
        if i == len(kept) - 1:
            w.page(batch, granule, header_type=0x04)        # end of stream
    return bytes(w.out)
