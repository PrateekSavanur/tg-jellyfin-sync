FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so code changes don't invalidate this layer
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y tg-jellyfin-sync

COPY app/ ./app/

# Non-root; the Deployment's init container hands /data and /state to this uid
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app
USER 1000

CMD ["python", "-m", "app.main"]
