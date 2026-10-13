FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md policy.yaml ./
COPY factory ./factory
COPY tests ./tests
COPY samples ./samples
RUN pip install --no-cache-dir -e .

CMD ["python", "-m", "pytest", "-q"]
