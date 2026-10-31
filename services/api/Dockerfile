FROM python:3.10-slim-bullseye
ARG APP_VERSION=0.2.0
LABEL org.apiserver.managed="true"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_VERSION=${APP_VERSION} TMPDIR=/resources/tmp
WORKDIR /app
COPY services/api/requirements.lock /app/requirements.lock
COPY wheelhouse /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels --require-hashes -r requirements.lock && rm -rf /wheels
COPY services/api/app /app/app
COPY services/api/migrations /app/migrations
EXPOSE 8110
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8110/health/ready', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8110"]
