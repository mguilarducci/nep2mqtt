# syntax=docker/dockerfile:1
FROM python:3.14-slim AS builder

# uv ships a static binary; copying beats installing pip.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, without the project itself: this layer is rebuilt only
# when the lockfile changes, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY nep2mqtt ./nep2mqtt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.14-slim AS runtime

# Runs as a non-root user: a service that parses bytes off the network and is
# reachable by every device on the LAN should not be root in its own container.
RUN useradd --create-home --uid 1000 nep2mqtt

# The base image ships pip; this image never calls it. PATH points at the
# virtualenv, uv did the installing in the builder stage, and nothing in
# nep2mqtt/ imports pip or setuptools.
#
# It is not merely unused. pip vendors its own copies of other libraries, which
# image scanners report as findings that are not reachable by anything running
# here. Deleting it removes the code behind those reports.
#
# This does not shrink the image: the files still exist in the base layer and
# this only adds a whiteout on top. The gain is attack surface, not bytes.
RUN rm -rf /usr/local/lib/python*/site-packages/pip* /usr/local/bin/pip*

WORKDIR /app
COPY --from=builder --chown=nep2mqtt:nep2mqtt /app /app

# Use the virtualenv without needing `uv run`, so the image has one less moving
# part at runtime.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# OpenTelemetry is wired in but OFF by default, which is where this image
# differs from a private service that always has a collector waiting. Enabled
# unconditionally, every deployment without a collector would spend its life
# retrying exports to nothing. Set OTEL_SDK_DISABLED=false together with
# OTEL_EXPORTER_OTLP_ENDPOINT to turn it on; see docker-compose.yml.
#
# The protocol is NOT optional when it is on: the Python SDK defaults to gRPC
# and goes looking for otlp_proto_grpc, which is not installed here and never
# will be. The exporter shipped is the HTTP one, and this points the SDK at it.
ENV OTEL_SDK_DISABLED=true \
    OTEL_SERVICE_NAME=nep2mqtt \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf

# The inverters always POST to port 80 and that is not configurable on their
# side, but this container is not root and so cannot bind a privileged port.
# It listens high and the host publishes 80 onto it; see docker-compose.yml.
# UVICORN_PORT is uvicorn's own environment override for --port, so the port
# has a single owner instead of being threaded through application config.
ENV UVICORN_PORT=3333
EXPOSE 3333

USER nep2mqtt

# A GET carries no body, so the handler answers the time and publishes nothing.
# That makes it a free liveness probe for a service that would otherwise look
# healthy while wedged, since telemetry only arrives every five minutes.
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; \
urllib.request.urlopen('http://127.0.0.1:'+os.environ['UVICORN_PORT'], timeout=4)"

# --factory so nothing is built at import time; see nep2mqtt/app.py:build.
#
# --no-access-log because the application already logs one line per reading
# carrying the serial and the power; uvicorn's would repeat every one of them
# saying less.
# opentelemetry-instrument wraps the process whether or not the SDK is enabled;
# with OTEL_SDK_DISABLED=true it costs a no-op wrapper at startup and nothing
# after, which beats maintaining two different command lines.
CMD ["opentelemetry-instrument", \
     "uvicorn", "--factory", "nep2mqtt.app:build", \
     "--host", "0.0.0.0", "--no-access-log"]
