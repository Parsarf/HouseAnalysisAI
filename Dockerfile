FROM node:22-alpine AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

# PyMuPDF, Tesseract OCR, and WeasyPrint runtime libraries used by the API.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl libgdk-pixbuf-2.0-0 libpango-1.0-0 libpangoft2-1.0-0 \
       libcairo2 shared-mime-info tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY . .
COPY --from=web-build /web/dist ./web/dist
RUN pip install --no-cache-dir .

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
