# Custom Format Search for Radarr & Sonarr

This is a Python script, run via Docker, that looks through your Radarr and Sonarr libraries. It finds media that has not met the "Custom Format Cutoff Score" defined in your quality profiles and triggers a search for a random selection of them, helping you to automatically upgrade your library over time.

## Features

-   **Automated Upgrades:** Periodically scans your libraries to find and search for media that falls below your defined custom format score.
-   **Multi-Instance Support:** Connect to and manage multiple Radarr and Sonarr instances from a single container.
-   **Granular Control:** Set both a global cap (`MAX_UPGRADES`) and per-instance limits (`*_NUM_TO_UPGRADE`) on the number of searches per run.
-   **Safe Testing:** Use the `DRY_RUN` mode to see what the script would do without sending any actual search commands.
-   **Search History:** Remembers which items have been searched for and prevents re-searching them for a configurable cooldown period.
-   **Easy First-Time Setup:** Automatically creates a default `.env` configuration file if one doesn't exist in your config volume.
-   **Flexible Configuration:** Configure via a `.env` file or directly in your `docker-compose.yml`.

## Configuration

The script is configured using environment variables. These can be placed in a `.env` file or directly in your `docker-compose.yml`.

| Variable | Description | Default Value |
| --- | --- | --- |
| `TZ` | Sets the timezone for the container, affecting cron scheduling and log timestamps. | `America/Los_Angeles` |
| `MAX_UPGRADES` | The maximum total number of items to search for across ALL instances in a single run. Set to `0` to disable all searches. A negative value means no global limit. | `20` |
| `DRY_RUN` | Enabled by default for safety. Set to `true` to run in simulation mode. Set to `false` to perform actual searches after verifying your configuration. | `true` |
| `DEBUG_MODE` | Set to `true` to save detailed lists of all processed media and their upgrade eligibility to JSON files in the config directory. Useful for troubleshooting. | `false` |
| `DELAY_BETWEEN_INSTANCES` | The number of seconds to wait between triggering searches for each instance. This helps to stagger the load on indexers and download clients. | `10` |
| `HISTORY_COOLDOWN_DAYS` | The number of days to wait before an item can be searched for again. Prevents the script from repeatedly searching for the same media. | `30` |
| `RADARR{n}_URL` | URL for the Radarr instance (e.g., `RADARR0_URL`, `RADARR1_URL`). | (none) |
| `RADARR{n}_API_KEY` | API Key for the corresponding Radarr instance. | (none) |
| `RADARR{n}_NUM_TO_UPGRADE` | The maximum number of movies to search for from THIS instance per run. Set to `0` to disable. Leave unset or set to a negative number for no limit. | `5` |
| `SONARR{n}_URL` | URL for the Sonarr instance (e.g., `SONARR0_URL`, `SONARR1_URL`). | (none) |
| `SONARR{n}_API_KEY` | API Key for the corresponding Sonarr instance. | (none) |
| `SONARR{n}_NUM_TO_UPGRADE` | The maximum number of episodes to search for from THIS instance per run. Set to `0` to disable. Leave unset or set to a negative number for no limit. | `10` |

## Setup Instructions

There are two primary ways to configure this script:

### Method 1: Using a `.env` File (Recommended)

This method is recommended for keeping your API keys and other secrets out of your `docker-compose.yml`.

1.  Create a directory on your host machine to store the configuration (e.g., `/path/to/your/config`).
2.  Create a `docker-compose.yml` file with the following content.

    ```yaml
    ---
    services:
      cf-search:
        image: ghcr.io/gitdeath/cf-search:latest
        container_name: cf-search
        restart: unless-stopped
        environment:
          - CRON_SCHEDULE=0 2 * * * # Run at 2 AM daily
          - TZ=America/Los_Angeles
        volumes:
          # Mount your config directory here. The script will create a .env file on first run.
          - /path/to/your/config:/config
    ```

3.  Run `docker compose up -d` to start the container. The first time it runs, it will create a `.env` file inside your config directory.
4.  Edit the newly created `.env` file with your instance URLs, API keys, and desired limits.
5.  Restart the container (`docker compose restart cf-search`) for the new settings to take effect.

### Method 2: Using Environment Variables in Docker Compose

You can also define all configuration variables directly in your `docker-compose.yml`. This is convenient but less secure if the file is committed to version control. Variables set here will override any values in a `.env` file.

1.  Create a `docker-compose.yml` file and add all necessary variables under the `environment` section.
2.  You still need to mount a volume for the script to write its persistent log file.
3.  Run `docker compose up -d` to start the container.

    ```yaml
    ---
    services:
      cf-search:
        image: ghcr.io/gitdeath/cf-search:latest
        container_name: cf-search
        restart: unless-stopped
        environment:
          # --- Core Settings ---
          - CRON_SCHEDULE=0 2 * * *
          - TZ=America/Los_Angeles
          # --- Application Settings ---
          # DRY_RUN is enabled by default for safety. Set to 'false' after verifying your configuration.
          - DRY_RUN=true
          - MAX_UPGRADES=20
          # --- Radarr Instance 0 ---
          - RADARR0_URL=http://192.168.0.100:7878
          - RADARR0_API_KEY=your_radarr0_api_key_here
          - RADARR0_NUM_TO_UPGRADE=5
          # --- Sonarr Instance 0 ---
          - SONARR0_URL=http://192.168.0.100:8989
          - SONARR0_API_KEY=your_sonarr0_api_key_here
          - SONARR0_NUM_TO_UPGRADE=10
        volumes:
          # A volume is still needed for persistent logging.
          - /path/to/your/config:/config
    ```

## Manual Trigger

If you want to run the script on-demand without waiting for the `CRON_SCHEDULE`, you can execute it manually inside the running container. This is useful for testing your configuration or forcing an immediate scan.

Make sure the container is running, then execute the following command:

```bash
docker exec -it cf-search python /app/app.py
```

This command will run the script immediately and output the logs directly to your terminal. It will respect all the environment variables you have configured, including `DRY_RUN`, `MAX_UPGRADES`, etc.

## Acknowledgements

This project was originally forked from TheHesster/radarr-cf-search. A big thank you to the original author for creating the foundation of this utility.
