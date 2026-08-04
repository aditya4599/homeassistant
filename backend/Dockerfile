FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

WORKDIR /srv

# Run as a non-root user.
# /srv/data holds runtime state (Spotify refresh token); docker-compose mounts a volume there.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/data \
    && chown -R appuser:appuser /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini .

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('APP_PORT', '8000'), timeout=3)" || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host \"${APP_HOST}\" --port \"${APP_PORT}\""]
