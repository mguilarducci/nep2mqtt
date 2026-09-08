"""FastAPI application that receives the telemetry the inverters push.

The units expose nothing pollable: they POST to ``www.nepviewer.net`` roughly
every five minutes. Point that hostname at this service (DNS override or DNAT)
and the telemetry arrives on its own.

The inverter expects the current time back as fourteen ASCII characters,
``YYYYMMDDHHMMSS``. That is the entire protocol response, so forwarding to the
vendor cloud is genuinely optional - without it this service answers with local
time and the whole system runs offline.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import APIRouter, FastAPI, Request, Response

from .config import ConfigError, Settings
from .mqtt import Publisher
from .protocol import ProtocolError, decode

log = logging.getLogger(__name__)

TIME_FORMAT = "%Y%m%d%H%M%S"

# The vendor cloud answers with this content type; mirror it rather than let
# FastAPI default to application/json, since the firmware on the other end was
# written against the original server and is not worth surprising.
RESPONSE_MEDIA_TYPE = "text/html"

router = APIRouter()


def _time_response() -> bytes:
    return datetime.now().strftime(TIME_FORMAT).encode("ascii")


def _hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {offset:04d}  {hex_part:<{width * 3}} |{text}|")
    return "\n".join(lines)


def _handle_frame(
    body: bytes, source: str, publisher: Publisher, settings: Settings
) -> None:
    try:
        reading = decode(
            body,
            grid_voltage_divisor=settings.grid_voltage_divisor,
            unpopulated_voltage_v=settings.unpopulated_voltage_v,
        )
    except ProtocolError as error:
        # The hex matters more than the message here. This is the only signal
        # anyone gets if NEP changes the firmware or a different model starts
        # reporting, and without the bytes there is nothing to work from.
        log.warning(
            "unrecognised frame from %s (%d bytes): %s\n%s",
            source, len(body), error, _hexdump(body),
        )
        return

    if not reading["checksum_ok"]:
        log.warning(
            "bad checksum from %s (serial %s); publishing anyway",
            source, reading["serial"],
        )

    log.info(
        "%s  %s  %.1f W  %.2f Hz  %.1f C",
        source, reading["serial"], reading["power_w"],
        reading["frequency_hz"], reading["temperature_c"],
    )
    try:
        publisher.publish(reading, source_ip=source)
    except Exception:
        # An MQTT failure must not cost the inverter its reply: with no reply it
        # retries, and losing the time sync is worse than losing one sample.
        log.exception("failed to publish over MQTT")


async def _forward(
    request: Request,
    body: bytes,
    client: httpx.AsyncClient,
    settings: Settings,
) -> bytes | None:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in ("host", "content-length")
    }
    headers["Host"] = settings.upstream_host
    try:
        response = await client.request(
            request.method,
            f"http://{settings.upstream_ip}{request.url.path}",
            content=body or None,
            headers=headers,
        )
        return response.content
    except httpx.HTTPError as error:
        log.warning("upstream forward failed: %s", error)
        return None


# A catch-all: this firmware posts to /t.php, but other NEP models are reported
# to use different paths and the payload is what identifies a frame, not the URL.
@router.api_route("/{_path:path}", methods=["GET", "POST"])
async def receive(request: Request, _path: str) -> Response:
    body = await request.body()
    source = request.client.host if request.client else "unknown"
    settings: Settings = request.app.state.settings

    if body:
        _handle_frame(body, source, request.app.state.publisher, settings)

    payload = None
    if settings.upstream_ip:
        payload = await _forward(request, body, request.app.state.http, settings)
    # `not payload` rather than `is None`: an upstream that answers 200 with an
    # empty body is as useless to the inverter as one that does not answer at
    # all. Either way it needs the time back, so fall through to ours.
    if not payload:
        payload = _time_response()

    return Response(content=payload, media_type=RESPONSE_MEDIA_TYPE)


def create_app(settings: Settings, publisher: Publisher | None = None) -> FastAPI:
    """Build the application.

    ``publisher`` is injectable so tests can exercise the request path without
    a live broker; in production it is built from the settings.
    """
    if publisher is None:
        publisher = Publisher(
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            client_id=settings.mqtt_client_id,
            prefix=settings.mqtt_prefix,
            discovery_prefix=settings.discovery_prefix,
            energy_wh_per_count=settings.energy_wh_per_count,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        publisher.connect()
        async with httpx.AsyncClient(
            timeout=settings.upstream_timeout_s
        ) as client:
            app.state.http = client
            log.info(
                "ready (upstream forwarding: %s)",
                settings.upstream_ip or "disabled",
            )
            yield
        publisher.disconnect()

    app = FastAPI(
        title="nep2mqtt",
        description="NEP microinverter telemetry to MQTT",
        lifespan=lifespan,
        # No docs: the only client is inverter firmware that will never read
        # them, and an unauthenticated service on a LAN should not offer an
        # interactive console it has no use for.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.publisher = publisher
    app.include_router(router)
    return app


def build() -> FastAPI:
    """Zero-argument factory, for ``uvicorn --factory nep2mqtt.app:build``.

    A factory rather than a module-level ``app``: building at import time would
    read the environment on import, so merely importing this module to test it
    would demand a configured broker.

    The port belongs to uvicorn (``UVICORN_PORT``), not here - one owner per
    concern.
    """
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        # A misconfigured environment is an operator error, not a crash: say
        # what is missing on one line instead of burying it in a traceback.
        sys.exit(str(error))

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return create_app(settings)
