FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py messaging.py ./
COPY static ./static
COPY templates ./templates

# Il database e la chiave segreta vivono in /app/instance (volume persistente)
RUN useradd --system --no-create-home scontrino \
    && mkdir -p /app/instance && chown scontrino /app/instance
USER scontrino
VOLUME /app/instance

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)"
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--access-logfile", "-", "app:app"]
