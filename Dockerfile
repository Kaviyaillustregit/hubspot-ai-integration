FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src

COPY pyproject.toml README.md ./
# Hatchling needs the configured src/app package to exist while pip resolves the
# runtime dependencies. The full source is copied after this cacheable layer.
COPY src/app/__init__.py ./src/app/__init__.py
RUN pip install --no-cache-dir --default-timeout=180 --retries=10 .

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
