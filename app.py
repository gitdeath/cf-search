import logging
import os
import time
import random
import sys

import requests
from dotenv import load_dotenv

# Create logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Create handlers
stream_handler = logging.StreamHandler(sys.stdout)
file_handler = logging.FileHandler('/config/output.log')

# Create formatters and add it to handlers
log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
stream_handler.setFormatter(log_format)
file_handler.setFormatter(log_format)

# Add handlers to the logger
logger.addHandler(stream_handler)
logger.addHandler(file_handler)

# Load .env
load_dotenv(dotenv_path="/config/.env")

class ArrService:
    """A generic service for interacting with Radarr or Sonarr APIs."""
    def __init__(self, url, api_key):
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({'X-Api-Key': self.api_key})

    def _get(self, endpoint, params=None):
        """Performs a GET request to the API."""
        try:
            response = self.session.get(f"{self.url}/api/v3/{endpoint}", params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API GET request to {self.url}/api/v3/{endpoint} failed: {e}")
            return None

    def _post(self, endpoint, json_data):
        """Performs a POST request to the API."""
        try:
            response = self.session.post(f"{self.url}/api/v3/{endpoint}", json=json_data)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API POST request to {self.url}/api/v3/{endpoint} failed: {e}")
            return None

    def get_quality_profile_scores(self):
        """Fetches quality profiles and returns a map of ID to cutoff score."""
        profiles = self._get("qualityprofile")
        if profiles is None:
            return {}
        return {profile["id"]: profile["cutoffFormatScore"] for profile in profiles}

    def trigger_search(self, command_name, item_ids_key, item_ids, dry_run=False):
        """Triggers a search command for a list of item IDs."""
        if not item_ids:
            logger.info("No items to search for.")
            return

        payload = {
            "name": command_name,
            item_ids_key: item_ids
        }

        if dry_run:
            logger.info(f"[DRY RUN] Would trigger search for {len(item_ids)} items.")
            logger.debug(f"[DRY RUN] Payload: {payload}")
            return

        logger.info(f"Triggering search for {len(item_ids)} items...")
        self._post("command", payload)
        logger.info("Search command sent successfully.")

def get_radarr_upgradeables(config):
    """Finds all upgradeable movies for a single Radarr instance."""
    url = config['url']
    logger.info(f"--- Processing Radarr instance: {url} ---")
    service = ArrService(url, config['api_key'])
    upgradeable_items = []

    quality_scores = service.get_quality_profile_scores()
    if not quality_scores:
        logger.error("Could not fetch quality profiles. Aborting for this instance.")
        return service, upgradeable_items

    all_movies = service._get("movie")
    if all_movies is None:
        logger.error("Could not fetch movies. Aborting for this instance.")
        return service, upgradeable_items

    for movie in all_movies:
        if movie.get("monitored") and movie.get("hasFile"):
            movie_file = movie.get("movieFile", {})
            current_score = movie_file.get("customFormatScore", 0)
            cutoff_score = quality_scores.get(movie["qualityProfileId"])

            if cutoff_score is not None and current_score < cutoff_score:
                upgradeable_items.append({
                    "id": movie["id"],
                    "title": movie["title"],
                    "type": "movie"
                })

    logger.info(f"Found {len(upgradeable_items)} movies that can be upgraded.")
    return service, upgradeable_items

def get_sonarr_upgradeables(config):
    """Finds all upgradeable episodes for a single Sonarr instance."""
    url = config['url']
    logger.info(f"--- Processing Sonarr instance: {url} ---")
    service = ArrService(url, config['api_key'])
    upgradeable_items = []

    quality_scores = service.get_quality_profile_scores()
    if not quality_scores:
        logger.error("Could not fetch quality profiles. Aborting for this instance.")
        return service, upgradeable_items

    all_series = service._get("series")
    if all_series is None:
        logger.error("Could not fetch series. Aborting for this instance.")
        return service, upgradeable_items

    for series in all_series:
        if not series.get("monitored") or series.get("statistics", {}).get("episodeFileCount", 0) == 0:
            continue

        cutoff_score = quality_scores.get(series["qualityProfileId"])
        if cutoff_score is None:
            continue

        # Fetch all episodes and all episode files for the series in two efficient calls
        all_episodes = service._get("episode", params={"seriesId": series["id"]})
        all_episode_files = service._get("episodefile", params={"seriesId": series["id"]})

        if not all_episodes or not all_episode_files:
            continue

        # Create a map of episodeFileId to episode details for quick lookup
        episode_map = {ep["episodeFileId"]: ep for ep in all_episodes if ep.get("episodeFileId")}

        for ep_file in all_episode_files:
            current_score = ep_file.get("customFormatScore", 0)
            if current_score < cutoff_score:
                # Look up the episode details from our map instead of a new API call
                episode = episode_map.get(ep_file["id"])
                if episode and episode.get("monitored"):
                    title = f"{series['title']} - S{episode['seasonNumber']:02d}E{episode['episodeNumber']:02d} - {episode['title']}"
                    upgradeable_items.append({
                        "id": episode["id"],
                        "title": title,
                        "type": "episode",
                    })

    logger.info(f"Found {len(upgradeable_items)} episodes that can be upgraded.")
    return service, upgradeable_items

def load_configs(service_name):
    """Loads all configurations for a given service (e.g., 'RADARR', 'SONARR')."""
    configs = []
    i = 0
    while True:
        url = os.getenv(f"{service_name}{i}_URL")
        api_key = os.getenv(f"{service_name}{i}_API_KEY")
        num_to_upgrade_str = os.getenv(f"{service_name}{i}_NUM_TO_UPGRADE")

        if not all([url, api_key]): # URL and API key are mandatory
            break  # Stop when we can't find a complete set of variables

        try:
            # Default to no limit for the instance. sys.maxsize is used as a practical stand-in for infinity.
            instance_limit = sys.maxsize
            if num_to_upgrade_str is not None and num_to_upgrade_str.isdigit():
                limit_val = int(num_to_upgrade_str)
                if limit_val == 0:
                    logger.info(f"NUM_TO_UPGRADE for {service_name}{i} is 0. This instance will be skipped.")
                    i += 1
                    continue
                elif limit_val > 0:
                    instance_limit = limit_val
            
            # If num_to_upgrade_str is not set, is not a digit, or is negative, it's treated as unlimited.
            
            config = {
                "url": url,
                "api_key": api_key,
                "num_to_upgrade": instance_limit
            }
            configs.append(config)
            logger.info(f"Loaded configuration for {service_name}{i} (instance limit: {instance_limit if instance_limit != sys.maxsize else 'no limit'})")
        except (ValueError, TypeError):
            logger.error(f"Invalid NUM_TO_UPGRADE value for {service_name}{i}. Skipping.")
        i += 1
    return configs

def trigger_grouped_searches(items_to_search, dry_run=False):
    """Groups items by service and triggers the appropriate search commands."""
    searches_by_service = {}
    for item in items_to_search:
        service = item['service']
        if service not in searches_by_service:
            searches_by_service[service] = {'movies': [], 'episodes': []}

        if item['type'] == 'movie':
            searches_by_service[service]['movies'].append(item)
        elif item['type'] == 'episode':
            searches_by_service[service]['episodes'].append(item)

    for service, items in searches_by_service.items():
        if items['movies']:
            logger.info(f"Queueing search for {len(items['movies'])} movies on {service.url}")
            for movie in items['movies']:
                logger.info(f"  - {movie['title']}")
            service.trigger_search("MoviesSearch", "movieIds", [m['id'] for m in items['movies']], dry_run)

        if items['episodes']:
            logger.info(f"Queueing search for {len(items['episodes'])} episodes on {service.url}")
            for episode in items['episodes']:
                logger.info(f"  - {episode['title']}")
            service.trigger_search("EpisodeSearch", "episodeIds", [e['id'] for e in items['episodes']], dry_run)

def main():
    """Main execution function."""
    logger.info("======================================================")
    logger.info("Starting Custom Format Search script")

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    if dry_run:
        logger.info("DRY RUN is enabled. No actual search commands will be sent.")

    delay_str = os.getenv("DELAY_BETWEEN_INSTANCES", "0")
    try:
        DELAY_BETWEEN_INSTANCES = int(delay_str) if delay_str.isdigit() else 0
        if DELAY_BETWEEN_INSTANCES < 0:
            DELAY_BETWEEN_INSTANCES = 0
    except ValueError:
        DELAY_BETWEEN_INSTANCES = 0

    max_upgrades_str = os.getenv("MAX_UPGRADES")
    try:
        MAX_UPGRADES = int(max_upgrades_str) if max_upgrades_str and max_upgrades_str.isdigit() else None
        if MAX_UPGRADES is not None and MAX_UPGRADES <= 0:
            # A global limit of 0 or less is nonsensical, so treat it as no limit.
            MAX_UPGRADES = None # Treat 0 or less as no limit
    except ValueError:
        logger.warning(f"Invalid MAX_UPGRADES value '{max_upgrades_str}'. Disabling global limit.")
        MAX_UPGRADES = None

    # Combine all configs into a single list for sequential processing
    radarr_configs = load_configs("RADARR")
    sonarr_configs = load_configs("SONARR")
    all_configs = radarr_configs + sonarr_configs

    remaining_global_upgrades = sys.maxsize if MAX_UPGRADES is None else MAX_UPGRADES

    for i, config in enumerate(all_configs):
        if remaining_global_upgrades <= 0:
            logger.info("Global upgrade limit reached. No more instances will be processed.")
            break

        # Determine the correct function to call based on the service name in the URL
        get_upgradeables_func = get_radarr_upgradeables if 'radarr' in config['url'].lower() else get_sonarr_upgradeables
        
        service, items = get_upgradeables_func(config)
        if not items:
            continue

        # Apply instance and global limits
        instance_limit = config['num_to_upgrade']
        num_to_select = min(len(items), instance_limit, remaining_global_upgrades)
        
        items_to_search = random.sample(items, k=num_to_select)
        remaining_global_upgrades -= len(items_to_search)

        trigger_grouped_searches(items_to_search, dry_run=dry_run)

        if DELAY_BETWEEN_INSTANCES > 0 and i < len(all_configs) - 1:
            logger.info(f"Waiting for {DELAY_BETWEEN_INSTANCES} seconds before processing next instance...")
            if not dry_run:
                time.sleep(DELAY_BETWEEN_INSTANCES)

    logger.info("Script finished.")
    logger.info("======================================================")

if __name__ == "__main__":
    main()