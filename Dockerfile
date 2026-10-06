FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir websockets==17.1 aiomqtt bcrypt pywebpush

# Glob rather than an explicit file list -- an explicit list silently drops
# new top-level modules when one is added and this file isn't updated to
# match (happened with backup.py and features.py: both shipped fine in dev,
# then crash-looped in the actual Docker image with ModuleNotFoundError since
# the old explicit COPY line never named them).
COPY *.py ./
COPY printers/ printers/
COPY public/ public/

# Runtime data lives in /data (mounted as volume)
ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8080 8443 8765 8766

# No curl/wget in python:slim — use urllib so the check needs no extra package.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('HTTP_PORT', '8080') + '/api/health', timeout=3)" || exit 1

CMD ["python3", "server.py"]
