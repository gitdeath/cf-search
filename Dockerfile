FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y cron && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install dependencies first. This layer is cached and only re-run
# when requirements.txt changes, speeding up subsequent builds.
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source code and supporting scripts.
COPY app.py .
COPY .example_env .
COPY cronjob.template .
COPY entrypoint.sh .
RUN chmod +x ./entrypoint.sh

RUN mkdir /config

ENTRYPOINT ["./entrypoint.sh"]