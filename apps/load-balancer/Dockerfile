# ── Build stage ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="AI Load Balancer Team" \
      description="LLM Load Balancer – enterprise-grade multi-endpoint router" \
      version="3.0.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser \
    && mkdir -p /data && chown appuser:appuser /data /app

COPY --chown=appuser:appuser load_balancer.py load_balancer_admin.html ./

USER appuser

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys;r=urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=3);sys.exit(0 if r.status<500 else 1)"

CMD ["python", "-u", "load_balancer.py"]
