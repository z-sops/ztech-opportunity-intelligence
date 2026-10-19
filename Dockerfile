# ZTech Opportunity Intelligence - container image.
#
# Added in F0 (audit gap G-48). The repository previously had no Dockerfile, no
# compose file and no CI, so the service could only be started by hand from a
# developer checkout.
#
# The image runs the REST service. Two security properties of the service are
# preserved by design and must be understood before it is deployed:
#
#   1. The Host-header check is always on. A request whose Host header is not
#      a loopback name is refused with 421 MISDIRECTED_REQUEST. This is the
#      DNS-rebinding defence. A request that arrives as
#      "Host: 127.0.0.1:8099" is served; a request arriving as
#      "Host: oi.internal:8099" is not. Put a reverse proxy in front that
#      rewrites the Host header, or keep the service on a loopback-only
#      network namespace.
#
#   2. ZTECH_OI_AUTH_TOKEN must be set. When it is unset the service runs
#      with NO authentication at all. There is no default token in this image.
#      Set it in the deployment environment:
#        docker run -e ZTECH_OI_AUTH_TOKEN="$(openssl rand -hex 32)" ...
#
# The engine reads every provider credential from the environment. With none
# configured the service still starts and honestly reports each provider as
# "unavailable" - it never fails to boot and never invents a finding.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ZTECH_OI_DB_PATH=/data/ztech_oi.sqlite3

WORKDIR /app

# Dependencies first, so a source change does not reinstall the world.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[rest]"

# The runtime data directory (the SQLite store) lives on a volume.
RUN mkdir -p /data && useradd --create-home --uid 10001 ztech \
    && chown -R ztech:ztech /app /data
USER ztech

EXPOSE 8099

# The container is unhealthy if the service does not answer /v1/health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/v1/health', timeout=4).status==200 else 1)"

# Bound to 0.0.0.0 so the port mapping works; the Host-header check and the
# bearer token are what actually protect the service.
CMD ["python", "-m", "uvicorn", "ztech_oi.adapters.rest:app", \
     "--host", "0.0.0.0", "--port", "8099", "--no-access-log"]
