FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY common contracts jobs auth api db extraction ingestion classification identity normalization finance strategies scoring flags pipeline ops ./
RUN pip install --no-cache-dir .
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
