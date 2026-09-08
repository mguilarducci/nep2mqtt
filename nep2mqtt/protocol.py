"""Decoder for the NEP BDM-2250 microinverter telemetry frame.

The inverter POSTs 69 unencrypted bytes to ``www.nepviewer.net/t.php`` roughly
every five minutes. This layout was reverse engineered from real traffic of
three units (2026-04-17) and validated by checksum and by cross-sums.

Frame map (decimal offsets, little-endian integers)::

    [0]      0x79, start byte
    [1]      0x3e (62), probably a length
    [19:23]  serial, uint32
    [25:27]  total power, uint16, /10 -> W  (equals the sum of the channels)
    [27:29]  DC group 1 voltage (mirrors channel 0), /256 -> V
    [29:31]  grid voltage, uint16 (see GRID_VOLTAGE_DIVISOR)
    [33:35]  frequency, uint16, /256 -> Hz
    [35:37]  temperature, uint16, /100 -> C
    [37:39]  energy today, uint16, in counts (equals the sum of the channels)
    [41:65]  four 6-byte blocks, one per channel:
                 +0 voltage, uint16, /256 -> V
                 +2 power, uint16, /10 -> W
                 +4 energy today, uint16, in counts
    [67]     additive checksum, sum(bytes[1:67]) % 256
    [68]     XOR checksum, xor(bytes[1:67])

Bytes with no known purpose: [1], [3], [4], [7:11] (always ``0f 0f 0f 0f``),
[15:19] (always ``c3 c3 c3 c3``) and [65:67]. The four-byte arrays are identical
even on a unit with two empty channels, so they are not per-channel status flags.
[65:67] is not a counter or a timestamp either: across consecutive samples it
moves up and then down, and it tracks power loosely rather than time.

Two scales are still provisional, which is why they stay configurable:

``GRID_VOLTAGE_DIVISOR``
    30.0 yields the phase-to-neutral voltage (~127 V) on a 127/220 system. The
    same reading divided by 17.32 (10*sqrt(3)) yields phase-to-phase (~220 V).
    The convention still needs confirming with a multimeter.

Energy
    This module returns **raw counts**, never Wh. The conversion is about
    9.25 Wh per count (measured by integrating power across three units), but it
    is not settled yet, so the choice belongs to the publishing layer.
"""

from __future__ import annotations

import struct

FRAME_SIZE = 69
START_BYTE = 0x79
CHANNEL_COUNT = 4
CHANNEL_BLOCK_OFFSET = 41
CHANNEL_BLOCK_SIZE = 6
CHECKSUM_RANGE = slice(1, 67)
GRID_VOLTAGE_DIVISOR = 30.0
UNPOPULATED_VOLTAGE_V = 5.0
"""Below this the input has no panel attached: an empty input reads a steady
~0.93 V, while a populated one stays above 30 V even in weak sunlight."""


class ProtocolError(ValueError):
    """The payload is not a valid NEP telemetry frame."""


def _u16(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<H", buf, offset)[0]


def _checksums(buf: bytes) -> tuple[int, int]:
    body = buf[CHECKSUM_RANGE]
    additive = sum(body) % 256
    xor = 0
    for byte in body:
        xor ^= byte
    return additive, xor


def decode(
    payload: bytes,
    *,
    grid_voltage_divisor: float = GRID_VOLTAGE_DIVISOR,
    unpopulated_voltage_v: float = UNPOPULATED_VOLTAGE_V,
) -> dict:
    """Decode a 69-byte frame.

    The two scales are arguments rather than fixed constants because neither is
    confirmed yet; the module-level values are the current best estimates and
    the service overrides them from the environment.

    Raises :class:`ProtocolError` if the frame is unrecognisable. A checksum
    mismatch is **not** an error: the frame comes back with ``checksum_ok`` set
    to False, because one corrupted value still beats dropping the whole sample,
    and the caller gets to decide what to do about it.
    """
    if len(payload) != FRAME_SIZE:
        raise ProtocolError(f"expected {FRAME_SIZE} bytes, got {len(payload)}")
    if payload[0] != START_BYTE:
        raise ProtocolError(
            f"start byte {payload[0]:#04x}, expected {START_BYTE:#04x}"
        )

    additive, xor = _checksums(payload)

    channels = []
    for index in range(CHANNEL_COUNT):
        base = CHANNEL_BLOCK_OFFSET + index * CHANNEL_BLOCK_SIZE
        voltage = _u16(payload, base) / 256
        channels.append(
            {
                "index": index,
                "voltage_v": round(voltage, 2),
                "power_w": round(_u16(payload, base + 2) / 10, 1),
                "energy_raw": _u16(payload, base + 4),
                "unpopulated": voltage < unpopulated_voltage_v,
            }
        )

    return {
        "serial": payload[19:23][::-1].hex().upper(),
        "power_w": round(_u16(payload, 25) / 10, 1),
        "grid_voltage_v": round(_u16(payload, 29) / grid_voltage_divisor, 2),
        "grid_voltage_raw": _u16(payload, 29),
        "frequency_hz": round(_u16(payload, 33) / 256, 2),
        "temperature_c": round(_u16(payload, 35) / 100, 2),
        "energy_today_raw": _u16(payload, 37),
        "channels": channels,
        "checksum_ok": additive == payload[67] and xor == payload[68],
    }
