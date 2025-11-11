#!/bin/bash

echo "--- Starting cf-search entrypoint ---"

# If a .env file does not exist in the config volume, create one from the example.
# This provides a template configuration for first-time users.
if [ ! -f "/config/.env" ]; then
  echo "INFO: No .env file found in /config. Creating one from the example."
  cp /app/.example_env /config/.env
else
  echo "INFO: Existing .env file found in /config."
fi

# Set a default cron schedule if the CRON_SCHEDULE environment variable is not provided.
if [ -z "$CRON_SCHEDULE" ]; then
  echo "INFO: CRON_SCHEDULE environment variable not set, using default schedule of '0 2 * * *' (2 AM daily)."
  CRON_SCHEDULE="0 2 * * *"  # Default to 2 AM daily if not set
fi

echo "INFO: Cron schedule set to '${CRON_SCHEDULE}'"

# Dynamically create the final cron job file. Using 'echo' ensures the file
# always ends with a newline character, which is required by cron.
echo "${CRON_SCHEDULE} /usr/local/bin/python /app/app.py > /proc/1/fd/1 2>&1" > /etc/cron.d/my-cron-job.actual

# Ensure cron can read the job file and add it to the crontab.
chmod 0644 /etc/cron.d/my-cron-job.actual
crontab /etc/cron.d/my-cron-job.actual

echo "INFO: Starting cron daemon."
echo "--- Entrypoint setup complete ---"
# Start the cron daemon in the foreground. This is the main process (PID 1)
# that keeps the container running and allows Docker to capture its logs.
cron -f