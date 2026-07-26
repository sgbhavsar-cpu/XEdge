# syntax=docker/dockerfile:1
# Multi-arch build (amd64, arm64, armv7) via `docker buildx build --platform ...`.
# Non-root, read-only-rootfs friendly (NFR-C-004, system-architecture.md §6.2).

FROM python:3.11-slim AS builder

# armv7 has no prebuilt wheel for cffi (a transitive dependency of
# `cryptography`), so pip falls back to building it from source and the
# stock slim image has no compiler — the armv7 leg of the multi-arch build
# failed on `gcc: No such file or directory` from the first push until
# Sprint 0 (XEDGE-403). These are builder-stage only; the runtime stage
# below copies just /install, so the shipped image stays slim.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

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
COPY deploy/docker/healthcheck.py /usr/local/bin/xedge-healthcheck.py

USER xedge
WORKDIR /

VOLUME ["/data"]
EXPOSE 4840 8080 8443 9090

# tls.enabled defaults to true (Sprint 13) — a stock config serves
# HTTPS-only on 8080, so the probe itself needs to handle that (see
# deploy/docker/healthcheck.py), not just assume plain HTTP.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/usr/local/bin/xedge-healthcheck.py"]

ENTRYPOINT ["xedge"]
CMD ["--config", "/etc/xedge/xedge.yaml"]
