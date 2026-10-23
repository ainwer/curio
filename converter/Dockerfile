FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml app.py ./
RUN pip install --no-cache-dir .

ENV HF_HOME=/cache/huggingface

RUN useradd --uid 1000 --create-home converter \
    && mkdir -p /cache/huggingface /data/books \
    && chown -R converter:converter /cache /data/books

USER converter

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]