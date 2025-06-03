#!/bin/bash

# Replace placeholder in cron job template with the environment variable
if [ -z "$CRON_SCHEDULE" ]; then
  echo "CRON_SCHEDULE environment variable not set, using default schedule of every hour."
  CRON_SCHEDULE="0 * * * *"  # Default to every hour if not set
fi

# Replace the cronjob template with the actual cron schedule
sed "s|\${CRON_SCHEDULE}|$CRON_SCHEDULE|" /etc/cron.d/my-cron-job > /etc/cron.d/my-cron-job.actual

# Set permissions for the cron job file
chmod 0644 /etc/cron.d/my-cron-job.actual
crontab /etc/cron.d/my-cron-job.actual

# Start cron in the foreground
cron -f