FROM python:3.12-alpine

# The service is pure Python standard library — no pip install needed.
COPY app.py /app.py

EXPOSE 8100

# Lightweight liveness check against the always-200 /health endpoint.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health',timeout=4).status==200 else 1)"

# app.py reads /data/options.json itself, so no jq / run.sh wrapper is needed.
CMD ["python3", "/app.py"]
