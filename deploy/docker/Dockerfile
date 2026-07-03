# syntax=docker/dockerfile:1
# Multi-arch build (amd64, arm64, armv7) via `docker buildx build --platform ...`.
# Non-root, read-only-rootfs friendly (NFR-C-004, system-architecture.md §6.2).

FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY xedge ./xedge
COPY config/schema ./config/schema

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim AS runtime

RUN groupadd --gid 10001 xedge \
    && useradd --uid 10001 --gid xedge --no-create-home --shell /usr/sbin/nologin xedge \
    && mkdir -p /data /var/log/xedge /etc/xedge \
    && chown -R xedge:xedge /data /var/log/xedge /etc/xedge

COPY --from=builder /install /usr/local
COPY config/schema /etc/xedge/schema

USER xedge
WORKDIR /

VOLUME ["/data"]
EXPOSE 4840 8443 9090

# Placeholder until the REST /health endpoint ships (Sprint 5, XEDGE-044);
# for now this only proves the interpreter and installed package are intact.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import xedge"]

ENTRYPOINT ["xedge"]
CMD ["--config", "/etc/xedge/xedge.yaml"]
