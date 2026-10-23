FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CHAINLIT_HOST=0.0.0.0 \
    CHAINLIT_PORT=8000

WORKDIR /app

COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY . /app
RUN useradd --create-home --uid 10001 algosage \
    && mkdir -p /app/.algosage \
    && chown -R algosage:algosage /app

USER algosage
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["python", "run_app.py", "--host", "0.0.0.0", "--port", "8000"]
