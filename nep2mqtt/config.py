"""Configuration, read once from the environment.

This is the single source of truth for defaults. Nothing downstream repeats
them: `Publisher` takes its arguments without defaults on purpose, so a value
changed here cannot silently disagree with a copy somewhere else.

Note what is *not* here: there is no inverter list. Each frame carries its own
serial, so units register themselves and adding a microinverter means plugging
it in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Known address of the NEP cloud. An IP and not a hostname on purpose: DNS for
# www.nepviewer.net is redirected at this service, so resolving the name would
# loop straight back into us.
DEFAULT_UPSTREAM_IP = "162.215.212.139"
DEFAULT_UPSTREAM_HOST = "www.nepviewer.net"
DEFAULT_UPSTREAM_TIMEOUT_S = 8.0

# Provisional. Measured at ~9.25 Wh per count by integrating power across three
# units over a short window; 10 Wh is still plausible. Compare a full day
# against the vendor app and correct it here.
DEFAULT_ENERGY_WH_PER_COUNT = 9.25

# 17.32 is 10*sqrt(3), and yields the phase-to-phase voltage (~220 V) that a
# microinverter measures at its own terminals. Dividing the same reading by 30
# instead yields phase-to-neutral (~127 V) on a 127/220 system - the same
# measurement, stated the other way round. If your readings come out low by a
# factor of sqrt(3), that is the one you want.
DEFAULT_GRID_VOLTAGE_DIVISOR = 17.32

# An input with no panel reads a steady ~0.93 V on a BDM-2250, while a populated
# one stays above 30 V even in weak light. Exposed because another model may sit
# somewhere else entirely.
DEFAULT_UNPOPULATED_VOLTAGE_V = 5.0


class ConfigError(RuntimeError):
    """The environment does not describe a runnable service."""


def _text(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _integer(name: str, default: int) -> int:
    raw = _text(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from error


def _number(name: str, default: float) -> float:
    raw = _text(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from error


def _positive(name: str, default: float) -> float:
    value = _number(name, default)
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    mqtt_host: str
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "nep2mqtt"
    mqtt_prefix: str = "nep2mqtt"
    discovery_prefix: str = "homeassistant"
    upstream_ip: str | None = DEFAULT_UPSTREAM_IP
    upstream_host: str = DEFAULT_UPSTREAM_HOST
    upstream_timeout_s: float = DEFAULT_UPSTREAM_TIMEOUT_S
    energy_wh_per_count: float = DEFAULT_ENERGY_WH_PER_COUNT
    grid_voltage_divisor: float = DEFAULT_GRID_VOLTAGE_DIVISOR
    unpopulated_voltage_v: float = DEFAULT_UNPOPULATED_VOLTAGE_V
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        host = _text("MQTT_HOST")
        if not host:
            raise ConfigError(
                "MQTT_HOST is required (the address of your MQTT broker)"
            )

        # An empty UPSTREAM_IP is a deliberate choice rather than a missing
        # value: it turns off forwarding to the vendor cloud and runs offline.
        upstream = os.environ.get("UPSTREAM_IP", DEFAULT_UPSTREAM_IP).strip()

        return cls(
            mqtt_host=host,
            mqtt_port=_integer("MQTT_PORT", 1883),
            mqtt_username=_text("MQTT_USERNAME") or None,
            mqtt_password=_text("MQTT_PASSWORD") or None,
            mqtt_client_id=_text("MQTT_CLIENT_ID", "nep2mqtt"),
            mqtt_prefix=_text("MQTT_PREFIX", "nep2mqtt"),
            discovery_prefix=_text("HA_DISCOVERY_PREFIX", "homeassistant"),
            upstream_ip=upstream or None,
            upstream_host=_text("UPSTREAM_HOST", DEFAULT_UPSTREAM_HOST),
            upstream_timeout_s=_positive(
                "UPSTREAM_TIMEOUT_S", DEFAULT_UPSTREAM_TIMEOUT_S
            ),
            # Divisors: a zero here would be a division by zero at the first
            # frame, so reject it at startup where the message is readable.
            energy_wh_per_count=_positive(
                "ENERGY_WH_PER_COUNT", DEFAULT_ENERGY_WH_PER_COUNT
            ),
            grid_voltage_divisor=_positive(
                "GRID_VOLTAGE_DIVISOR", DEFAULT_GRID_VOLTAGE_DIVISOR
            ),
            unpopulated_voltage_v=_number(
                "UNPOPULATED_VOLTAGE_V", DEFAULT_UNPOPULATED_VOLTAGE_V
            ),
            log_level=_text("LOG_LEVEL", "INFO").upper(),
        )
