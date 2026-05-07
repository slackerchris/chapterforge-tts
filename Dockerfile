FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Bake the version from the VERSION file into the image
COPY VERSION .
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Books, output, and logs are expected to be mounted as volumes
RUN mkdir -p /app/books /app/output /app/logs

EXPOSE 8890

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8890"]
