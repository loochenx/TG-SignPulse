# ── Stage 0: Frontend (arch-independent, runs only once) ─────────────────────
FROM node:20-slim AS frontend-builder

WORKDIR /frontend

# Dependency manifests first — layer cached until package-lock changes.
COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


# ── Stage 1: Python builder — compile all C extensions per target arch ────────
FROM python:3.12-slim AS python-builder

# TARGETPLATFORM is injected by docker buildx (e.g. "linux/amd64").
ARG TARGETPLATFORM

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:$PATH"

# Build tools for C extensions (tgcrypto, cryptography, etc.).
# These stay in the builder and never reach the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv — the whole directory is COPY'd into runtime.
RUN python -m venv /venv

WORKDIR /build

# ① Metadata only — this layer is reused when only source code changes.
COPY pyproject.toml ./
COPY tg_signer/__init__.py ./tg_signer/__init__.py

# ② All external deps — must stay in sync with pyproject.toml [project.dependencies].
#    Using an explicit list (rather than `pip install .`) keeps this layer cached
#    when only application source code changes.
RUN pip install --no-cache-dir \
      "kurigram<=2.2.7" \
      "pydantic<2" \
      "fastapi==0.109.2" \
      "bcrypt==4.0.1" \
      "uvicorn[standard]" \
      sqlalchemy \
      "passlib[bcrypt]==1.7.4" \
      "python-jose[cryptography]" \
      pyotp \
      "qrcode[pil]" \
      apscheduler \
      python-multipart \
      httpx \
      openai \
      croniter \
      json_repair \
      click \
      typing-extensions

# ③ tgcrypto: compile only on amd64; QEMU arm64 builds often fail.
#    TARGETPLATFORM is always set by buildx — no uname -m fallback needed.
RUN if [ "$TARGETPLATFORM" = "linux/amd64" ]; then \
      pip install --no-cache-dir tgcrypto; \
    else \
      echo "Skipping tgcrypto on ${TARGETPLATFORM}"; \
    fi

# ④ Install the project itself — invalidated only when source changes.
COPY . /build
RUN pip install --no-cache-dir --no-deps .


# ── Stage 2: Runtime — zero build tools, smallest possible image ─────────────
FROM python:3.12-slim AS app

# Build-time version metadata (injected by CI via --build-arg).
ARG BUILD_DATE="unknown"
ARG BUILD_SHA="dev"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    TZ=Asia/Shanghai \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:$PATH" \
    BUILD_DATE=${BUILD_DATE} \
    BUILD_SHA=${BUILD_SHA}

WORKDIR /app

# Only runtime OS deps: tzdata for TZ handling, gosu for uid-mapping.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata gosu \
    && rm -rf /var/lib/apt/lists/*

# All compiled Python packages — no pip, no build-essential needed.
COPY --from=python-builder /venv /venv

# Application source code.
COPY . /app

# Frontend static files served from /web.
RUN mkdir -p /web
COPY --from=frontend-builder /frontend/out /web

# Data dir (bind-mounted in production).
RUN mkdir -p /data

# Non-root user.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd -r -g ${APP_GID} app && \
    useradd -r -u ${APP_UID} -g app -d /app -s /usr/sbin/nologin app && \
    chown -R app:app /data

# Runtime entrypoint auto-adapts to mounted /data ownership.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.getenv(\"PORT\", \"8080\")}/healthz').read()"

ENTRYPOINT ["/entrypoint.sh"]
