import logging
import os
import time
import json
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

# --- Constants ---
HISTORY_FILE = "/config/search_history.json"

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

    def test_connection(self):
        """Tests the connection to the service by fetching system status."""
        logger.debug(f"Testing connection to {self.url}...")
        status = self._get("system/status")
        return status is not None

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

def get_radarr_upgradeables(config, search_history, cooldown_seconds, debug_mode=False):
    """Finds all upgradeable movies for a single Radarr instance."""
    url = config['url']
    logger.info(f"--- Processing Radarr instance: {url} ---")
    service = ArrService(url, config['api_key'])
    upgradeable_items = []
    debug_data = []

    if not service.test_connection():
        logger.error(f"Could not connect to Radarr instance at {url}. Check URL and API Key. Skipping.")
        return service, upgradeable_items

    quality_scores = service.get_quality_profile_scores()
    if not quality_scores:
        logger.error("Could not fetch quality profiles. Aborting for this instance.")
        return service, upgradeable_items

    all_movies = service._get("movie")
    if all_movies is None:
        logger.error("Could not fetch movies. Aborting for this instance.")
        return service, upgradeable_items

    for movie in all_movies:
        upgradeable = False
        current_score = "N/A"
        cutoff_score = quality_scores.get(movie["qualityProfileId"])

        if movie.get("monitored") and movie.get("movieFileId", 0) > 0:
            # A movieFileId > 0 indicates that a file is associated with the movie.
            # We need to fetch the movie file details separately to get the custom format score.
            movie_file = service._get(f"moviefile/{movie['movieFileId']}")
            if not movie_file:
                logger.debug(f"Could not retrieve movie file details for '{movie['title']}'. Skipping.")
                continue # Skip to the next movie

            current_score = movie_file.get("customFormatScore", 0)

            if cutoff_score is not None and current_score < cutoff_score:
                history_key = f"radarr-{movie['id']}"
                last_searched_timestamp = search_history.get(history_key)
                if not (last_searched_timestamp and (time.time() - last_searched_timestamp) < cooldown_seconds):
                    upgradeable = True
                    upgradeable_items.append({
                        "id": movie["id"],
                        "title": movie["title"],
                        "type": "movie"
                    })
                else:
                    logger.debug(f"Skipping recently searched movie: {movie['title']}")

        if debug_mode:
            debug_data.append({
                "title": movie["title"],
                "monitored": movie.get("monitored"),
                "hasFile": movie.get("hasFile"),
                "current_score": current_score,
                "cutoff_score": cutoff_score,
                "upgradeable": upgradeable
            })

    if debug_mode:
        debug_file_path = "/config/radarr_debug_list.json"
        logger.info(f"Saving Radarr debug list to {debug_file_path}")
        try:
            with open(debug_file_path, 'w') as f:
                json.dump(debug_data, f, indent=4)
        except IOError as e:
            logger.error(f"Failed to save debug list to {debug_file_path}: {e}")

    logger.info(f"Found {len(upgradeable_items)} movies that can be upgraded.")
    return service, upgradeable_items

def get_sonarr_upgradeables(config, search_history, cooldown_seconds, debug_mode=False):
    """Finds all upgradeable episodes for a single Sonarr instance."""
    url = config['url']
    logger.info(f"--- Processing Sonarr instance: {url} ---")
    service = ArrService(url, config['api_key'])
    upgradeable_items = []
    debug_data = []

    if not service.test_connection():
        logger.error(f"Could not connect to Sonarr instance at {url}. Check URL and API Key. Skipping.")
        return service, upgradeable_items

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
            logger.debug(f"Skipping series '{series['title']}' (unmonitored or no files).")
            continue

        cutoff_score = quality_scores.get(series["qualityProfileId"])
        if cutoff_score is None:
            logger.debug(f"Skipping series '{series['title']}' (no valid cutoff score in quality profile).")
            continue

        all_episodes = service._get("episode", params={"seriesId": series["id"]})
        all_episode_files = service._get("episodefile", params={"seriesId": series["id"]})

        if not all_episodes or not all_episode_files:
            continue

        episode_map = {ep["id"]: ep for ep in all_episodes}

        for ep_file in all_episode_files:
            upgradeable = False
            current_score = ep_file.get("customFormatScore", 0)
            episode = episode_map.get(ep_file.get("episodeId"))
            title = "Unknown Episode"
            
            if episode:
                title = f"{series['title']} - S{episode['seasonNumber']:02d}E{episode['episodeNumber']:02d} - {episode.get('title', 'N/A')}"

            if current_score < cutoff_score:
                if episode and episode.get("monitored"):
                    history_key = f"sonarr-{episode['id']}"
                    last_searched_timestamp = search_history.get(history_key)
                    if not (last_searched_timestamp and (time.time() - last_searched_timestamp) < cooldown_seconds):
                        upgradeable = True
                        upgradeable_items.append({
                            "id": episode["id"],
                            "title": title,
                            "type": "episode",
                        })
                    else:
                        logger.debug(f"Skipping recently searched episode: {title}")

            if debug_mode:
                debug_data.append({
                    "title": title,
                    "series_monitored": series.get("monitored"),
                    "episode_monitored": episode.get("monitored") if episode else "N/A",
                    "hasFile": True,
                    "current_score": current_score,
                    "cutoff_score": cutoff_score,
                    "upgradeable": upgradeable
                })

    if debug_mode:
        debug_file_path = "/config/sonarr_debug_list.json"
        logger.info(f"Saving Sonarr debug list to {debug_file_path}")
        try:
            with open(debug_file_path, 'w') as f:
                json.dump(debug_data, f, indent=4)
        except IOError as e:
            logger.error(f"Failed to save debug list to {debug_file_path}: {e}")

    logger.info(f"Found {len(upgradeable_items)} episodes that can be upgraded.")
    return service, upgradeable_items

def load_configs(service_name):
    """Loads all configurations for a given service (e.g., 'RADARR', 'SONARR')."""
    configs = []
    i = 0
    while True:
        prefix = f"{service_name}{i}"
        url = os.getenv(f"{prefix}_URL")
        api_key = os.getenv(f"{prefix}_API_KEY")

        if not all([url, api_key]): # URL and API key are mandatory
            break  # Stop when we can't find a complete set of variables

        num_to_upgrade_str = os.getenv(f"{prefix}_NUM_TO_UPGRADE")
        
        try:
            if num_to_upgrade_str is None:
                # Default to no limit if the variable is not set
                instance_limit = sys.maxsize
            else:
                limit_val = int(num_to_upgrade_str)
                if limit_val == 0:
                    logger.info(f"NUM_TO_UPGRADE for {prefix} is 0. This instance will be skipped.")
                    i += 1
                    continue
                # A negative value means no limit for this instance
                instance_limit = sys.maxsize if limit_val < 0 else limit_val
        except (ValueError, TypeError):
            logger.warning(f"Invalid NUM_TO_UPGRADE value '{num_to_upgrade_str}' for {prefix}. Treating as unlimited.")
            instance_limit = sys.maxsize
        
        # Determine service type from the variable group name ('RADARR' or 'SONARR')
        # This is more reliable than inferring from the URL.
        config = {
            "url": url,
            "api_key": api_key,
            "num_to_upgrade": instance_limit,
            "service_type": service_name.lower()
        }
        configs.append(config)
        
        limit_str = 'unlimited' if instance_limit == sys.maxsize else str(instance_limit)
        logger.info(f"Loaded configuration for {prefix} (instance limit: {limit_str})")
        
        i += 1
    return configs

def trigger_grouped_searches(items_to_search, search_history, dry_run=False):
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

    # If not a dry run, update the history for all items about to be searched
    if not dry_run:
        update_history(items_to_search, search_history)

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

def load_history(cooldown_seconds):
    """Loads the search history from the JSON file and prunes old entries."""
    try:
        with open(HISTORY_FILE, 'r') as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("No valid search history found. Starting fresh.")
        return {}

    # Prune old entries that are past the cooldown period.
    now = time.time()
    pruned_history = {
        key: timestamp for key, timestamp in history.items()
        if (now - timestamp) < cooldown_seconds
    }

    pruned_count = len(history) - len(pruned_history)
    if pruned_count > 0:
        logger.info(f"Pruned {pruned_count} old entr(y/ies) from search history.")

    return pruned_history

def save_history(history_data):
    """Saves the search history to the JSON file."""
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history_data, f, indent=4)
    except IOError as e:
        logger.error(f"Failed to save search history to {HISTORY_FILE}: {e}")

def update_history(searched_items, search_history):
    """Updates the history with the items that were just searched."""
    for item in searched_items:
        search_history[f"{item['service_type']}-{item['id']}"] = time.time() # e.g., "radarr-123" or "sonarr-456"

def main():
    """Main execution function."""
    logger.info("======================================================")
    logger.info("Starting Custom Format Search script")

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    if dry_run:
        logger.info("DRY RUN is enabled. No actual search commands will be sent.")

    debug_mode = os.getenv("DEBUG_MODE", "false").lower() == "true"
    if debug_mode:
        logger.info("DEBUG MODE is enabled. Detailed item lists will be saved.")

    try:
        cooldown_days = int(os.getenv("HISTORY_COOLDOWN_DAYS", "30"))
        if cooldown_days < 0:
            cooldown_days = 0
    except ValueError:
        cooldown_days = 30
    cooldown_seconds = cooldown_days * 86400
    logger.info(f"Search history cooldown is set to {cooldown_days} days.")

    delay_str = os.getenv("DELAY_BETWEEN_INSTANCES", "10")
    try:
        DELAY_BETWEEN_INSTANCES = int(delay_str)
        if DELAY_BETWEEN_INSTANCES < 0:
            logger.warning(f"Invalid DELAY_BETWEEN_INSTANCES value '{delay_str}'. Using 0.")
            DELAY_BETWEEN_INSTANCES = 0
    except ValueError:
        logger.warning(f"Invalid DELAY_BETWEEN_INSTANCES value '{delay_str}'. Using default of 10.")
        DELAY_BETWEEN_INSTANCES = 10

    max_upgrades_str = os.getenv("MAX_UPGRADES", "20")
    try:
        MAX_UPGRADES = int(max_upgrades_str)
        if MAX_UPGRADES < 0:
            # A negative global limit is treated as no limit.
            MAX_UPGRADES = sys.maxsize
            logger.info("MAX_UPGRADES is negative, disabling global limit.")
    except ValueError:
        logger.warning(f"Invalid MAX_UPGRADES value '{max_upgrades_str}'. Disabling global limit.")
        MAX_UPGRADES = sys.maxsize

    logger.info(f"Global upgrade limit is set to {MAX_UPGRADES if MAX_UPGRADES != sys.maxsize else 'unlimited'}.")

    # Combine all configs into a single list for sequential processing
    radarr_configs = load_configs("RADARR")
    sonarr_configs = load_configs("SONARR")
    all_configs = radarr_configs + sonarr_configs

    # Load history at the start of the run
    search_history = load_history(cooldown_seconds)

    if MAX_UPGRADES == 0:
        logger.info("MAX_UPGRADES is set to 0. No searches will be performed.")
        all_configs = [] # Clear configs to prevent any processing

    remaining_global_upgrades = MAX_UPGRADES

    for i, config in enumerate(all_configs):        
        # Determine the correct function to call based on the service name in the URL
        get_upgradeables_func = get_radarr_upgradeables if config['service_type'] == 'radarr' else get_sonarr_upgradeables
        
        service, items = get_upgradeables_func(config, search_history, cooldown_seconds, debug_mode)
        if not items:
            # If there are no items, we might still need to delay before the next instance.
            if DELAY_BETWEEN_INSTANCES > 0 and i < len(all_configs) - 1:
                logger.info(f"Waiting for {DELAY_BETWEEN_INSTANCES} seconds before processing next instance...")
                if not dry_run:
                    time.sleep(DELAY_BETWEEN_INSTANCES)
            continue

        # Apply instance and global limits
        instance_limit = config['num_to_upgrade']
        num_to_select = min(len(items), instance_limit, remaining_global_upgrades)
        
        items_to_search = random.sample(items, k=num_to_select)

        # Add the service object to each item so it can be used for grouping.
        for item in items_to_search:
            item['service'] = service
            item['service_type'] = config['service_type']

        remaining_global_upgrades -= len(items_to_search)

        trigger_grouped_searches(items_to_search, search_history, dry_run=dry_run)
        
        # Check if the global limit has been reached and break for the next run.
        if remaining_global_upgrades <= 0:
            logger.info(f"Global upgrade limit of {MAX_UPGRADES} reached. No more instances will be processed in this run.")
            break

        if DELAY_BETWEEN_INSTANCES > 0 and i < len(all_configs) - 1:
            logger.info(f"Waiting for {DELAY_BETWEEN_INSTANCES} seconds before processing next instance...")
            if not dry_run:
                time.sleep(DELAY_BETWEEN_INSTANCES)

    # Save the updated history at the end of the run
    if not dry_run:
        save_history(search_history)

    logger.info("Script finished.")
    logger.info("======================================================")

if __name__ == "__main__":
    main()