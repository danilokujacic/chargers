# One image for every Python process: the Central System (main.py), the public API
# (uvicorn api.app:app), the demo fleet, and one-off tools (migrate.py, the seed scripts,
# operate.py). docker-compose.yml picks the command per service.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run unprivileged. /data holds the files the scripts write (charger keys, the demo fleet's
# manifest and state), on a named volume so they survive rebuilds.
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /data \
    && chown app:app /data
USER app

EXPOSE 9000 8000
CMD ["python", "-u", "main.py"]
