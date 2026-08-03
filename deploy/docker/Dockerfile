# syntax=docker/dockerfile:1
# Multi-arch build (amd64, arm64, armv7) via `docker buildx build --platform ...`.
# Non-root, read-only-rootfs friendly (NFR-C-004, system-architecture.md §6.2).

# Sprint P3 (ADR-013 §6): the React fleet dashboard, built here and copied
# into the Python builder stage below before `pip install` runs, so
# hatchling picks up xedge/fleet/static/dashboard/ as ordinary package
# data -- it's already inside the `xedge` package tree, unlike
# config/schema/ below, which needs its own pyproject.toml
# force-include entry because it isn't. `--platform=$BUILDPLATFORM`:
# this stage's output (static HTML/JS/CSS) has no CPU architecture of
# its own, so it must run on the *build* machine's native platform, not
# be redundantly re-emulated under QEMU once per target platform like
# the actual Python/C-extension stages below need to be.
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

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
COPY --from=frontend-builder /build/xedge/fleet/static/dashboard ./xedge/fleet/static/dashboard

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
