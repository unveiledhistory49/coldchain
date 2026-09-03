# NOTE: no module-level `app` exists in src/coldchain/api.py (verified via
# `grep -rn "^app =" src/`); the ASGI app is built by the `create_app`
# factory, so uvicorn must run with `--factory`.
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd -m appuser && mkdir -p /app/data && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"

CMD ["uvicorn", "coldchain.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
