FROM python:3.11-slim
# git is needed at build time because `lighter-sdk` is installed from a git URL.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY . .
CMD ["python", "-m", "radar.main"]
