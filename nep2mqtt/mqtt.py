"""MQTT publishing with Home Assistant MQTT Discovery.

Follows the zigbee2mqtt conventions: one state topic per device carrying the
whole payload as JSON, retained discovery configs, and bridge availability via
Last Will.

On availability: there is a **bridge** LWT but no per-inverter availability.
That is deliberate. A microinverter powers itself down when the sun sets, so a
per-device watchdog would mark every unit ``offline`` each night and train you to
ignore the alarm. Each device publishes ``last_seen`` instead, so anyone who
wants a staleness alert can build it with their own threshold.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

MANUFACTURER = "NEP"
MODEL = "BDM-2250"

# (key, label, unit, device_class, state_class)
DEVICE_SENSORS = [
    ("power_w", "Power", "W", "power", "measurement"),
    ("energy_today_kwh", "Energy today", "kWh", "energy", "total_increasing"),
    ("grid_voltage_v", "Grid voltage", "V", "voltage", "measurement"),
    ("frequency_hz", "Grid frequency", "Hz", "frequency", "measurement"),
    ("temperature_c", "Temperature", "°C", "temperature", "measurement"),
]

# Transport metadata rather than telemetry: the IP is not in the frame, it is
# where the frame came from. Published as a diagnostic entity so Home Assistant
# files it under Diagnostic instead of mixing it in with the readings.
DIAGNOSTIC_SENSORS = [
    ("source_ip", "IP address", "mdi:ip-network"),
]

CHANNEL_SENSORS = [
    ("voltage_v", "voltage", "V", "voltage", "measurement"),
    ("power_w", "power", "W", "power", "measurement"),
    ("energy_kwh", "energy", "kWh", "energy", "total_increasing"),
]


class Publisher:
    """Holds the MQTT connection and publishes decoded telemetry.

    Discovery is sent the first time each device - and each populated channel -
    shows up. A channel that gets a panel later is announced as soon as it starts
    reporting voltage.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        client_id: str,
        prefix: str,
        discovery_prefix: str,
        energy_wh_per_count: float,
    ) -> None:
        """No defaults here on purpose.

        Every one of these has a default in :mod:`nep2mqtt.config`, which is the
        single source of truth. Repeating them would let the two drift apart,
        and the copy that wins would be whichever caller forgot an argument.
        """
        self.prefix = prefix.rstrip("/")
        self.discovery_prefix = discovery_prefix.rstrip("/")
        self.energy_wh_per_count = energy_wh_per_count
        self.availability_topic = f"{self.prefix}/bridge/state"
        self._announced: set[str] = set()

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id
        )
        if username:
            self.client.username_pw_set(username, password or None)
        self.client.will_set(self.availability_topic, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self._host, self._port = host, port

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        log.info("connecting to MQTT broker at %s:%s", self._host, self._port)
        self.client.connect(self._host, self._port, keepalive=60)
        self.client.loop_start()

    def disconnect(self) -> None:
        self.client.publish(self.availability_topic, "offline", retain=True)
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("connected to MQTT broker")
        client.publish(self.availability_topic, "online", retain=True)
        # After a reconnect the broker may have dropped the retained configs,
        # so forget what we announced and let the next reading re-announce it.
        self._announced.clear()

    # ------------------------------------------------------------ discovery

    def _device_block(self, serial: str) -> dict:
        return {
            "identifiers": [f"nep2mqtt_{serial}"],
            "name": f"NEP {serial}",
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }

    def _publish_discovery(self, serial: str, key: str, config: dict) -> None:
        topic = f"{self.discovery_prefix}/sensor/nep2mqtt_{serial}/{key}/config"
        self.client.publish(topic, json.dumps(config), retain=True)

    def _announce_device(self, serial: str) -> None:
        state_topic = f"{self.prefix}/{serial}"
        for key, label, unit, device_class, state_class in DEVICE_SENSORS:
            self._publish_discovery(
                serial,
                key,
                {
                    "name": label,
                    "unique_id": f"nep2mqtt_{serial}_{key}",
                    "state_topic": state_topic,
                    "value_template": f"{{{{ value_json.{key} }}}}",
                    "unit_of_measurement": unit,
                    "device_class": device_class,
                    "state_class": state_class,
                    "availability_topic": self.availability_topic,
                    "device": self._device_block(serial),
                },
            )

    def _announce_diagnostics(self, serial: str) -> None:
        state_topic = f"{self.prefix}/{serial}"
        for key, label, icon in DIAGNOSTIC_SENSORS:
            self._publish_discovery(
                serial,
                key,
                {
                    "name": label,
                    "unique_id": f"nep2mqtt_{serial}_{key}",
                    "state_topic": state_topic,
                    "value_template": f"{{{{ value_json.{key} }}}}",
                    "icon": icon,
                    # No device_class or state_class: this is a text value, and
                    # declaring either would have Home Assistant try to graph it.
                    "entity_category": "diagnostic",
                    "availability_topic": self.availability_topic,
                    "device": self._device_block(serial),
                },
            )

    def _announce_channel(self, serial: str, index: int) -> None:
        state_topic = f"{self.prefix}/{serial}"
        label_number = index + 1
        for field, suffix, unit, device_class, state_class in CHANNEL_SENSORS:
            key = f"ch{label_number}_{suffix}"
            self._publish_discovery(
                serial,
                key,
                {
                    "name": f"Channel {label_number} {suffix}",
                    "unique_id": f"nep2mqtt_{serial}_{key}",
                    "state_topic": state_topic,
                    "value_template": (
                        f"{{{{ value_json.channels[{index}].{field} }}}}"
                    ),
                    "unit_of_measurement": unit,
                    "device_class": device_class,
                    "state_class": state_class,
                    "availability_topic": self.availability_topic,
                    "device": self._device_block(serial),
                },
            )

    # ---------------------------------------------------------------- state

    def _to_kwh(self, counts: int) -> float:
        return round(counts * self.energy_wh_per_count / 1000, 3)

    def publish(self, reading: dict, source_ip: str | None = None) -> None:
        """Publish one reading.

        ``source_ip`` is where the frame arrived from. It is deliberately not
        part of ``reading``: the decoder reports what the bytes contain, and the
        address is not among them.
        """
        serial = reading["serial"]
        if serial not in self._announced:
            self._announce_device(serial)
            self._announce_diagnostics(serial)
            self._announced.add(serial)
            log.info("announced device to Home Assistant: %s", serial)

        channels = []
        for channel in reading["channels"]:
            index = channel["index"]
            if not channel["unpopulated"]:
                marker = f"{serial}:ch{index}"
                if marker not in self._announced:
                    self._announce_channel(serial, index)
                    self._announced.add(marker)
                    log.info("announced channel %s of %s", index + 1, serial)
            channels.append(
                {
                    "voltage_v": channel["voltage_v"],
                    "power_w": channel["power_w"],
                    "energy_kwh": self._to_kwh(channel["energy_raw"]),
                    "unpopulated": channel["unpopulated"],
                }
            )

        payload = {
            "serial": serial,
            "power_w": reading["power_w"],
            "energy_today_kwh": self._to_kwh(reading["energy_today_raw"]),
            "grid_voltage_v": reading["grid_voltage_v"],
            "frequency_hz": reading["frequency_hz"],
            "temperature_c": reading["temperature_c"],
            "checksum_ok": reading["checksum_ok"],
            "source_ip": source_ip,
            "last_seen": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "channels": channels,
        }
        self.client.publish(
            f"{self.prefix}/{serial}", json.dumps(payload), retain=True
        )
