FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 manager
WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN pip install --no-cache-dir .

USER manager
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
