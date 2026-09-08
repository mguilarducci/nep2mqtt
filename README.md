# nep2mqtt

Local telemetry for **NEP BDM-2250** microinverters, published to MQTT with Home
Assistant discovery. No vendor cloud required.

Reverse engineered and tested against three real units. The decoder ships with
captured hardware packets as test fixtures, anonymised - see Development.

## How it works

These microinverters expose no local API. Their built-in web server has exactly
four pages and all of them are Wi-Fi configuration - there is no telemetry to
poll, no Modbus, no transparent UART port.

What they *do* is push. Every five minutes each unit POSTs 69 unencrypted bytes
to `www.nepviewer.net`. Redirect that hostname to the machine running this
container and the telemetry arrives on its own.

```
inverters --POST /t.php--> nep2mqtt --> MQTT --> Home Assistant
                              |
                              +--(optional) forward to the NEP cloud
```

The inverter expects the current time back as fourteen ASCII characters
(`YYYYMMDDHHMMSS`). That is the entire protocol response, so nep2mqtt can answer
it directly. Forwarding to the vendor cloud is optional and only exists to keep
the NEPViewer app working.

**There is no inverter list to configure.** Each frame carries its own serial, so
units register themselves. Adding another microinverter means plugging it in.

## Setup

You need two things: this container reachable on port 80, and DNS for
`www.nepviewer.net` pointing at it.

1. Run the container (see `docker-compose.yml`). **Something has to answer on
   port 80** - the inverters will not talk to any other port and it is not
   configurable on their side. Either publish this container there directly
   (`HOST_PORT=80`), or put it behind a reverse proxy that already holds 80 and
   forwards by Host header, which is what the default `HOST_PORT=3333` assumes.
   The container itself always listens on 3333, because the process runs
   unprivileged and cannot bind a privileged port.
2. Override DNS for `www.nepviewer.net` to the container host. Most routers can
   do this; a destination NAT rule works too.
3. If your inverters sit on an isolated IoT VLAN, allow that VLAN to reach the
   container host on TCP/80. Without this the connection dies silently at the
   gateway and you will see nothing at all - which looks exactly like the
   inverters not talking.

Devices appear in Home Assistant within about five minutes.

## Configuration

All configuration is environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `MQTT_HOST` | *(required)* | Broker address |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USERNAME` | *(none)* | Broker username |
| `MQTT_PASSWORD` | *(none)* | Broker password |
| `MQTT_CLIENT_ID` | `nep2mqtt` | Broker client id |
| `MQTT_PREFIX` | `nep2mqtt` | Topic prefix |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | Discovery prefix |
| `HOST_PORT` | `3333` | Host port to publish on; set to `80` when running without a proxy |
| `UVICORN_PORT` | `3333` in the image | Port to receive on; uvicorn's own override |
| `UPSTREAM_IP` | `162.215.212.139` | NEP cloud IP; empty disables forwarding |
| `UPSTREAM_HOST` | `www.nepviewer.net` | Host header sent upstream |
| `UPSTREAM_TIMEOUT_S` | `8` | Upstream request timeout |
| `ENERGY_WH_PER_COUNT` | `9.25` | Energy scale, see below |
| `GRID_VOLTAGE_DIVISOR` | `17.32` | Grid voltage scale, see below |
| `UNPOPULATED_VOLTAGE_V` | `5` | Below this a channel counts as having no panel |
| `LOG_LEVEL` | `INFO` | Python log level |
| `OTEL_SDK_DISABLED` | `true` | Set to `false` to enable OpenTelemetry |

`UPSTREAM_IP` is an IP and not a hostname on purpose: DNS for the vendor domain
now points here, so resolving the name would loop straight back into this
service.

## Topics

```
nep2mqtt/bridge/state     online | offline (retained, Last Will)
nep2mqtt/<serial>         full reading as JSON (retained)
homeassistant/sensor/...  discovery configs (retained)
```

Each inverter becomes one Home Assistant device carrying power, energy today,
grid voltage, grid frequency, temperature, and per-channel voltage, power and
energy. Energy uses `total_increasing`, so it works in the Energy Dashboard.

Channels with no panel attached are detected and skipped, so a unit with two
panels gets two channels rather than two entities stuck at zero.

## Known limits

Two scales are still provisional, which is why both are adjustable:

- **Energy.** The frame reports counts, not watt-hours. Integrating power across
  three units puts one count at about 9.25 Wh; 10 Wh is still plausible. Compare
  a full day against the vendor app and set `ENERGY_WH_PER_COUNT` accordingly.
- **Grid voltage.** Reported as phase-to-phase (~220 V), which is what the
  inverter measures at its own terminals. Dividing the raw value by 30 instead
  of 17.32 yields phase-to-neutral (~127 V) on a 127/220 system - the same
  measurement stated the other way round. If your readings come out low by a
  factor of sqrt(3), set `GRID_VOLTAGE_DIVISOR=30`.

Neither affects power, frequency, temperature or the per-channel readings.

Several header bytes remain unidentified. They are constant across all three
units observed, including one with empty channels, so they are not per-channel
status flags.

## OpenTelemetry

The image is built with `opentelemetry-instrument` and ships the OTLP/HTTP
exporter plus FastAPI and httpx instrumentation, but **the SDK is off by
default**. A service with a collector waiting can afford to always export; a
published image cannot, and one enabled against nothing spends its life retrying.

To turn it on, set `OTEL_SDK_DISABLED=false` and point
`OTEL_EXPORTER_OTLP_ENDPOINT` at your collector. Two things are worth knowing:

- **Keep `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`** (the image sets it). The
  Python SDK otherwise defaults to gRPC and looks for an exporter that is not
  installed, failing in a way that reads like a connection problem.
- **Keep the timeouts** in `docker-compose.yml`. The case that hurts is a
  collector whose address drops packets rather than refusing them: requests are
  unaffected, but shutdown crawls while the process flushes at something that
  never answers. With the shipped values a stop takes about three seconds.

## Security note

The inverters' own web interface has no meaningful authentication - some units
accept `admin:admin`, others require nothing at all - and it is reachable by
anyone on the same network. Put them on an isolated IoT VLAN.

While DNS is redirected, anything on your network that talks to
`www.nepviewer.net` reaches this container instead. That is what makes it work,
and it is worth knowing.

## Development

Managed with [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run python -m unittest discover -s tests -t . -v
```

The tests run against packets captured from three units, including one with two
unpopulated channels.

**The fixtures are anonymised, and any new one must be.** A frame carries the
inverter's serial number, which is how the vendor cloud identifies the device -
closer to a weak credential than to telemetry, and not something to publish.
Fixtures here have synthetic serials (`AA0000xx`) with the checksums recomputed,
filenames that carry no IP addresses or capture times, and one sample per unit
rather than a series, so they show no generation curve. None of that costs
anything in test value: what makes these fixtures worth having is the real frame
structure, the real physical readings, and the cross-sums tying each channel to
the totals - all preserved.

If you capture your own, rewrite bytes 19-23 and recompute bytes 67 and 68
before committing.

## License

MIT
