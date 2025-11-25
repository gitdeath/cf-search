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

# Load environment variables from .env file so they are available to this script
if [ -f "/config/.env" ]; then
  echo "INFO: Loading environment variables from /config/.env"
  # Export variables automatically
  set -a
  source /config/.env
  set +a
fi

# Dump all environment variables to /etc/environment so cron can see them
printenv | grep -v "no_proxy" >> /etc/environment

# Set a default cron schedule if the CRON_SCHEDULE environment variable is not provided.
if [ -z "$CRON_SCHEDULE" ]; then
  if [ -n "$SEARCH_INTERVAL" ]; then
    echo "INFO: CRON_SCHEDULE not set, parsing SEARCH_INTERVAL: '${SEARCH_INTERVAL}'"
    # Extract the number and the unit (last character)
    unit="${SEARCH_INTERVAL: -1}"
    number="${SEARCH_INTERVAL%?}"

    if [[ "$number" =~ ^[0-9]+$ ]]; then
      case "$unit" in
        m)
          CRON_SCHEDULE="*/$number * * * *"
          ;;
        h)
          CRON_SCHEDULE="0 */$number * * *"
          ;;
        d)
          CRON_SCHEDULE="0 0 */$number * *"
          ;;
        *)
          echo "WARNING: Invalid unit '$unit' in SEARCH_INTERVAL. Defaulting to '0 2 * * *'."
          CRON_SCHEDULE="0 2 * * *"
          ;;
      esac
    else
      echo "WARNING: Invalid number '$number' in SEARCH_INTERVAL. Defaulting to '0 2 * * *'."
      CRON_SCHEDULE="0 2 * * *"
    fi
  else
    echo "INFO: Neither CRON_SCHEDULE nor SEARCH_INTERVAL set. Using default schedule of '0 2 * * *' (2 AM daily)."
    CRON_SCHEDULE="0 2 * * *"
  fi
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