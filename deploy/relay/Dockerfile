# Mycel relay: a stateless forwarder for end-to-end-encrypted frames.
# Build from the repo root:  docker build -f deploy/relay/Dockerfile -t mycel-relay .
# Run it only behind your own TLS load balancer (see README.md). It serves plain ws:// on 7600.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY mycel ./mycel
RUN pip install . && rm -rf /src && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin relay

USER 10001
WORKDIR /
EXPOSE 7600
# --behind-load-balancer: allows the 0.0.0.0 bind on the task's private interface (the load
# balancer's security group is the only ingress) and reads the client address from the
# balancer's X-Forwarded-For entry for per-address handshake limits.
CMD ["sh", "-c", "exec mycel relay --host 0.0.0.0 --port 7600 --behind-load-balancer --region \"${MYCEL_RELAY_REGION:-}\" --id \"${MYCEL_RELAY_ID:-}\""]
