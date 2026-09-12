FROM python:3.12-slim AS runtime

ARG SERVICE
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERVICE_MODULE=${SERVICE} \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml ./
COPY services ./services
RUN pip install --no-cache-dir . && \
    addgroup --system app && adduser --system --ingroup app app && \
    mkdir -p /data && chown -R app:app /data /app

USER app
EXPOSE 8000
CMD ["sh", "-c", "uvicorn services.${SERVICE_MODULE}.app:app --host 0.0.0.0 --port 8000"]

