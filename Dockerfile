FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELABEL_DB=/data/relabel.sqlite3

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY static ./static

# Data directory is a named volume in docker-compose; make it writable for the
# unprivileged runtime user.
RUN mkdir -p /data && useradd --create-home --uid 10001 relabel \
    && chown -R relabel:relabel /data /srv
USER relabel
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", \
     "--threads", "4", "--timeout", "60", "app:create_app()"]
