# syntax=docker/dockerfile:1

# ---------- stage 1: build the client ----------
FROM node:22-alpine AS web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---------- stage 2: backend + built client ----------
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=web /build/dist /app/web/dist

# The container runs as a non-root user: a future code-execution bug in the
# backend must not land on root. /data (the config-volume mount point) is
# created and owned by that user, so a first-use named volume inherits the
# ownership through Docker's copy-up. A volume that already exists from an
# older, root-running image keeps root's ownership — docs/deploying.md carries
# the one-line migration.
RUN useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin livewall \
    && mkdir -p /data \
    && chown livewall:livewall /data
USER livewall

ENV LIVEWALL_HOST=0.0.0.0 \
    LIVEWALL_PORT=8470 \
    LIVEWALL_WEB_DIR=/app/web/dist \
    LIVEWALL_CONFIG_FILE=/data/config.json

EXPOSE 8470
CMD ["python", "app.py"]
