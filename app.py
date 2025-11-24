# -*- coding: utf-8 -*-
"""
This script automates media library upgrades for Radarr and Sonarr.

It periodically scans the libraries of configured Radarr and Sonarr instances,
identifies items that have not met their quality profile's cutoff, and then
finds items that are below the profile's custom format score. It then triggers
searches for a limited number of these items to find better-quality versions.

The script is configured entirely through environment variables and is designed
to be run in a container. It maintains a history of searched items to avoid
re-searching the same item within a configurable cooldown period, preventing
API spam.
"""
import logging
import os
import time
import json
import random
import sys

import requests
from dotenv import load_dotenv

# --- Logging Setup ---
# Configure a logger to output to both the console (stdout) and a file.
# This provides immediate feedback and a persistent record of script activity.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Handlers direct log messages to the appropriate destination.
stream_handler = logging.StreamHandler(sys.stdout)
file_handler = logging.FileHandler('/config/output.log') # Assumes a /config volume mount

# A consistent log format aids in parsing and readability.
log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
stream_handler.setFormatter(log_format)
file_handler.setFormatter(log_format)

# Register the handlers with the logger.
logger.addHandler(stream_handler)
logger.addHandler(file_handler)

# Load environment variables from a .env file located in the /config directory.
# This allows for easy configuration without modifying the script itself.
load_dotenv(dotenv_path="/config/.env")

# --- Constants ---
HISTORY_FILE = "/config/search_history.json"

class ArrService:
    """A generic service for interacting with Radarr or Sonarr APIs."""
    def __init__(self, url, api_key):
        """
        Initializes the service with API credentials.

        Args:
            url (str): The base URL of the Radarr or Sonarr instance.
            api_key (str): The API key for authentication.
        """
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({'X-Api-Key': self.api_key})

    def _get(self, endpoint, params=None):
        """
        Performs a GET request to a specified API endpoint.

        Args:
            endpoint (str): The API endpoint to query (e.g., 'system/status').
            params (dict, optional): A dictionary of query parameters.

        Returns:
            dict or None: The JSON response from the API, or None if the request fails.
        """
        try:
            response = self.session.get(f"{self.url}/api/v3/{endpoint}", params=params)
            response.raise_for_status() # Raises an HTTPError for bad responses (4xx or 5xx)
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API GET request to {self.url}/api/v3/{endpoint} failed: {e}")
            return None

    def _post(self, endpoint, json_data):
        """
        Performs a POST request to a specified API endpoint.

        Args:
            endpoint (str): The API endpoint to which the data is posted.
            json_data (dict): The JSON payload to send.

        Returns:
            dict or None: The JSON response from the API, or None if the request fails.
        """
        try:
            response = self.session.post(f"{self.url}/api/v3/{endpoint}", json=json_data)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API POST request to {self.url}/api/v3/{endpoint} failed: {e}")
            return None

    def get_quality_profile_details(self):
        """
        Fetches all quality profiles and maps their IDs to their cutoff details.
        This is crucial for determining if an item is upgradeable based on score or quality.

        Returns:
            dict: A mapping of quality profile IDs to a dict containing
                  'cutoffFormatScore' and 'cutoffQualityName'.
        """
        profiles = self._get("qualityprofile")
        if profiles is None:
            return {}
        
        profile_details = {}
        for profile in profiles:
            cutoff_quality_id = profile.get("cutoff")
            cutoff_quality_name = "N/A"
            # Find the name of the cutoff quality by iterating through the profile's items
            for item in profile.get("items", []):
                if item.get("quality", {}).get("id") == cutoff_quality_id:
                    cutoff_quality_name = item["quality"]["name"]
                    break
            
            profile_details[profile["id"]] = {
                "cutoffFormatScore": profile.get("cutoffFormatScore"),
                "cutoffQualityName": cutoff_quality_name
            }
        return profile_details

    def test_connection(self):
        """
        Tests the connection to the service by fetching system status.
        This is a lightweight way to validate credentials and connectivity.

        Returns:
            bool: True if the connection is successful, False otherwise.
        """
        logger.debug(f"Testing connection to {self.url}...")
        status = self._get("system/status")
        return status is not None

    def get_queue_size(self):
        """
        Gets the current number of items in the queue.

        Returns:
            int: The number of items in the queue, or 0 if the request fails.
        """
        queue = self._get("queue")
        if queue is None:
            return 0
        return queue.get("totalRecords", 0)

    def trigger_search(self, command_name, item_ids_key, item_ids, dry_run=False):
        """
        Triggers a search command (e.g., 'MoviesSearch') for a list of item IDs.

        Args:
            command_name (str): The name of the command to trigger (e.g., 'MoviesSearch').
            item_ids_key (str): The key for the list of IDs in the payload (e.g., 'movieIds').
            item_ids (list): A list of movie or episode IDs to search for.
            dry_run (bool): If True, logs the action without sending the command.
        """
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
    """
    Scans a Radarr instance to find movies eligible for an upgrade. It prioritizes
    movies that have not met their quality profile's cutoff, then looks for
    movies that can be upgraded based on custom format score.

    Args:
        config (dict): Configuration for the Radarr instance.
        search_history (dict): A history of recently searched items.
        cooldown_seconds (int): The minimum time in seconds before an item can be searched again.
        debug_mode (bool): If True, saves a detailed list of all processed items.

    Returns:
        tuple: A tuple containing the ArrService instance, a list of "cutoff unmet"
               items, and a list of "custom format score" upgradeable items.
    """
    url = config['url']
    logger.info(f"--- Processing Radarr instance: {url} ---")
    service = ArrService(url, config['api_key'])
    cutoff_unmet_items = []
    cf_upgradeable_items = []
    debug_data = []

    if not service.test_connection():
        logger.error(f"Could not connect to Radarr instance at {url}. Check URL and API Key. Skipping.")
        return service, [], []

    # Check queue size if a limit is configured
    queue_limit = config.get("queue_size_limit")
    if queue_limit is not None:
        current_queue_size = service.get_queue_size()
        if current_queue_size >= queue_limit:
            logger.info(f"Queue size ({current_queue_size}) exceeds limit ({queue_limit}). Skipping this instance.")
            return service, [], []

    quality_profile_details = service.get_quality_profile_details()
    if not quality_profile_details:
        logger.error("Could not fetch quality profiles. Aborting for this instance.")
        return service, [], []

    all_movies = service._get("movie")
    if all_movies is None:
        logger.error("Could not fetch movies. Aborting for this instance.")
        return service, [], []

    for movie in all_movies:
        upgradeable = False
        upgrade_reason = ""
        current_score = "N/A"
        current_quality = "N/A"
        
        profile_id = movie.get("qualityProfileId")
        profile_info = quality_profile_details.get(profile_id, {})
        cutoff_score = profile_info.get("cutoffFormatScore")
        cutoff_quality = profile_info.get("cutoffQualityName", "N/A")

        # Only consider movies that are monitored and have an associated file.
        if not (movie.get("monitored") and movie.get("hasFile")):
            if debug_mode:
                debug_data.append({
                    "title": movie["title"], "monitored": movie.get("monitored"), "hasFile": movie.get("hasFile"),
                    "qualityCutoffNotMet": False, # Cannot be met if there is no file
                    "current_quality": "N/A",
                    "cutoff_quality": cutoff_quality,
                    "current_score": "N/A", "cutoff_score": cutoff_score, "upgradeable": False, "reason": "Not monitored or no file"
                })
            continue

        history_key = f"radarr-{movie['id']}"
        last_searched_timestamp = search_history.get(history_key)
        if last_searched_timestamp and (time.time() - last_searched_timestamp) < cooldown_seconds:
            logger.debug(f"Skipping recently searched movie: {movie['title']}")
            continue

        # Fetch movie file details to check for upgrades
        movie_file = service._get(f"moviefile/{movie['movieFileId']}")
        if movie_file:
            if debug_mode:
                current_quality = movie_file.get("quality", {}).get("quality", {}).get("name", "N/A")
            
            # Priority 1: Check if the quality profile cutoff has not been met.
            # This flag is on the movieFile object, not the movie object.
            if movie_file.get("qualityCutoffNotMet", False):
                upgradeable = True
                upgrade_reason = "Quality cutoff not met"
                cutoff_unmet_items.append({
                    "id": movie["id"],
                    "title": f"{movie['title']} (Reason: {upgrade_reason})",
                    "type": "movie"
                })
            # Priority 2: Check for custom format score upgrades.
            elif cutoff_score is not None:
                current_score = movie_file.get("customFormatScore", 0)
                if current_score < cutoff_score:
                    upgradeable = True
                    upgrade_reason = f"CF score {current_score} < cutoff {cutoff_score}"
                    cf_upgradeable_items.append({
                        "id": movie["id"],
                        "title": f"{movie['title']} (Reason: {upgrade_reason})",
                        "type": "movie"
                    })
        else:
            logger.debug(f"Could not retrieve movie file details for '{movie['title']}'. Skipping upgrade checks.")

        if debug_mode:
            # Fetch score for debug if not already fetched
            if movie_file:
                current_score = movie_file.get("customFormatScore", 0)
                current_quality = movie_file.get("quality", {}).get("quality", {}).get("name", "N/A")

            debug_data.append({
                "title": movie["title"],
                "monitored": movie.get("monitored"),
                "hasFile": movie.get("hasFile"),
                "qualityCutoffNotMet": movie_file.get("qualityCutoffNotMet", False) if movie_file else False,
                "current_quality": current_quality,
                "cutoff_quality": cutoff_quality,
                "current_score": current_score,
                "cutoff_score": cutoff_score,
                "upgradeable": upgradeable,
                "reason": upgrade_reason
            })

    if debug_mode:
        instance_name = config['instance_name']
        debug_file_path = f"/config/radarr_debug_list_{instance_name}.json"
        logger.info(f"Saving Radarr debug list to {debug_file_path}")
        try:
            with open(debug_file_path, 'w') as f:
                json.dump(debug_data, f, indent=4)
        except IOError as e:
            logger.error(f"Failed to save debug list to {debug_file_path}: {e}")

    logger.info(f"Found {len(cutoff_unmet_items)} movies that have not met their quality cutoff.")
    logger.info(f"Found {len(cf_upgradeable_items)} movies that can be upgraded based on Custom Format score.")
    return service, cutoff_unmet_items, cf_upgradeable_items

def get_sonarr_upgradeables(config, search_history, cooldown_seconds, debug_mode=False):
    """
    Scans a Sonarr instance to find episodes eligible for an upgrade. It prioritizes
    episodes that have not met their quality profile's cutoff, then looks for
    episodes that can be upgraded based on custom format score.

    Args:
        config (dict): Configuration for the Sonarr instance.
        search_history (dict): A history of recently searched items.
        cooldown_seconds (int): The minimum time in seconds before an item can be searched again.
        debug_mode (bool): If True, saves a detailed list of all processed items.

    Returns:
        tuple: A tuple containing the ArrService instance, a list of "cutoff unmet"
               items, and a list of "custom format score" upgradeable items.
    """
    url = config['url']
    logger.info(f"--- Processing Sonarr instance: {url} ---")
    service = ArrService(url, config['api_key'])
    cutoff_unmet_items = []
    cf_upgradeable_items = []
    debug_data = []

    if not service.test_connection():
        logger.error(f"Could not connect to Sonarr instance at {url}. Check URL and API Key. Skipping.")
        return service, [], []

    # Check queue size if a limit is configured
    queue_limit = config.get("queue_size_limit")
    if queue_limit is not None:
        current_queue_size = service.get_queue_size()
        if current_queue_size >= queue_limit:
            logger.info(f"Queue size ({current_queue_size}) exceeds limit ({queue_limit}). Skipping this instance.")
            return service, [], []

    quality_profile_details = service.get_quality_profile_details()
    if not quality_profile_details:
        logger.error("Could not fetch quality profiles. Aborting for this instance.")
        return service, [], []

    all_series = service._get("series")
    if all_series is None:
        logger.error("Could not fetch series. Aborting for this instance.")
        return service, [], []

    for series in all_series:
        # Skip series that have no downloaded episodes.
        if series.get("statistics", {}).get("episodeFileCount", 0) == 0:
            logger.debug(f"Skipping series '{series['title']}' (no files).")
            continue

        profile_info = quality_profile_details.get(series["qualityProfileId"], {})
        cutoff_score = profile_info.get("cutoffFormatScore")
        if cutoff_score is None:
            logger.debug(f"Skipping series '{series['title']}' (no valid cutoff score in quality profile).")
            continue

        # Fetch all episode files for the series to check their scores.
        all_episode_files = service._get("episodefile", params={"seriesId": series["id"]})
        if not all_episode_files:
            continue

        for episode_file in all_episode_files:
            upgradeable = False
            upgrade_reason = ""
            episode = None # Will hold the episode data if fetched
            current_score = episode_file.get("customFormatScore", 0)

            # The episode file endpoint doesn't include monitoring status, so a separate
            # API call is required to get the full episode details.
            episode_list = service._get("episode", params={"episodeFileId": episode_file["id"]})
            if not episode_list:
                logger.debug(f"Could not find matching episode for file ID {episode_file['id']}. Skipping.")
                continue
            
            episode = episode_list[0]
            title = f"{series['title']} - S{episode['seasonNumber']:02d}E{episode['episodeNumber']:02d} - {episode.get('title', 'N/A')}"

            if not episode.get("monitored"):
                logger.debug(f"Skipping unmonitored episode: {title}")
                continue

            history_key = f"sonarr-{episode['id']}"
            last_searched_timestamp = search_history.get(history_key)
            if last_searched_timestamp and (time.time() - last_searched_timestamp) < cooldown_seconds:
                logger.debug(f"Skipping recently searched episode: {title}")
                continue

            # Priority 1: Check if the quality profile cutoff has not been met.
            if episode_file.get("qualityCutoffNotMet", False):
                upgradeable = True
                upgrade_reason = "Quality cutoff not met"
                cutoff_unmet_items.append({
                    "id": episode["id"],
                    "title": f"{title} (Reason: {upgrade_reason})",
                    "type": "episode",
                    "seriesId": series["id"],
                    "seasonNumber": episode["seasonNumber"]
                })
            # Priority 2: Check for custom format score upgrades.
                upgradeable = True
                upgrade_reason = f"CF score {current_score} < cutoff {cutoff_score}"
                cf_upgradeable_items.append({
                    "id": episode["id"],
                    "title": f"{title} (Reason: {upgrade_reason})",
                    "type": "episode",
                    "seriesId": series["id"],
                    "seasonNumber": episode["seasonNumber"]
                })

            if debug_mode:
                if episode:
                    debug_data.append({
                        "title": title,
                        "series_monitored": series.get("monitored"),
                        "episode_monitored": episode.get("monitored"),
                        "hasFile": episode.get("hasFile"),
                        "qualityCutoffNotMet": episode_file.get("qualityCutoffNotMet", False),
                        "current_score": current_score,
                        "cutoff_score": cutoff_score,
                        "upgradeable": upgradeable,
                        "reason": upgrade_reason
                    })

    if debug_mode:
        instance_name = config['instance_name']
        debug_file_path = f"/config/sonarr_debug_list_{instance_name}.json"
        logger.info(f"Saving Sonarr debug list to {debug_file_path}")
        try:
            with open(debug_file_path, 'w') as f:
                json.dump(debug_data, f, indent=4)
        except IOError as e:
            logger.error(f"Failed to save debug list to {debug_file_path}: {e}")

    logger.info(f"Found {len(cutoff_unmet_items)} episodes that have not met their quality cutoff.")
    logger.info(f"Found {len(cf_upgradeable_items)} episodes that can be upgraded based on Custom Format score.")
    return service, cutoff_unmet_items, cf_upgradeable_items

def load_configs(service_name):
    """
    Loads all configurations for a given service type (e.g., 'RADARR', 'SONARR')
    from environment variables. It looks for variables in the format:
    {SERVICE_NAME}{INDEX}_{VARIABLE}, e.g., RADARR0_URL, RADARR0_API_KEY.

    Args:
        service_name (str): The service to load configs for ('RADARR' or 'SONARR').

    Returns:
        list: A list of configuration dictionaries for the specified service.
    """
    configs = []
    i = 0
    while True:
        prefix = f"{service_name}{i}"
        url = os.getenv(f"{prefix}_URL")
        api_key = os.getenv(f"{prefix}_API_KEY")

        # A URL and API key are the minimum requirements for a configuration.
        if not all([url, api_key]):
            break  # Stop searching for configs when a complete set isn't found.

        num_to_upgrade_str = os.getenv(f"{prefix}_NUM_TO_UPGRADE")
        
        try:
            if num_to_upgrade_str is None:
                # Default to no limit if the variable is not set.
                instance_limit = sys.maxsize
            else:
                limit_val = int(num_to_upgrade_str)
                if limit_val == 0:
                    logger.info(f"NUM_TO_UPGRADE for {prefix} is 0. Custom Format score upgrades will be skipped for this instance.")
                instance_limit = sys.maxsize if limit_val < 0 else limit_val
        except (ValueError, TypeError):
            logger.warning(f"Invalid NUM_TO_UPGRADE value '{num_to_upgrade_str}' for {prefix}. Treating as unlimited.")
            instance_limit = sys.maxsize
        
        # Load cutoff unmet limit (applies to both Radarr and Sonarr)
        instance_cutoff_limit = 0 # Default to 0 if not specified
        num_cutoff_unmet_str = os.getenv(f"{prefix}_NUM_CUTOFF_UNMET_TO_UPGRADE")
        try:
            if num_cutoff_unmet_str is None:
                pass # Keep the default of 0
            else:
                limit_val = int(num_cutoff_unmet_str)
                if limit_val == 0:
                    logger.info(f"NUM_CUTOFF_UNMET_TO_UPGRADE for {prefix} is 0. This type of upgrade will be skipped for this instance.")
                instance_cutoff_limit = sys.maxsize if limit_val < 0 else limit_val
        except (ValueError, TypeError):
            logger.warning(f"Invalid NUM_CUTOFF_UNMET_TO_UPGRADE value '{num_cutoff_unmet_str}' for {prefix}. Treating as unlimited.")
            instance_cutoff_limit = sys.maxsize

        # Load queue size limit
        queue_size_limit = None
        queue_size_limit_str = os.getenv(f"{prefix}_QUEUE_SIZE_LIMIT")
        if queue_size_limit_str is not None:
            try:
                queue_size_limit = int(queue_size_limit_str)
                if queue_size_limit < 0:
                    logger.warning(f"Invalid QUEUE_SIZE_LIMIT '{queue_size_limit_str}' for {prefix}. Ignoring.")
                    queue_size_limit = None
            except ValueError:
                logger.warning(f"Invalid QUEUE_SIZE_LIMIT '{queue_size_limit_str}' for {prefix}. Ignoring.")
                queue_size_limit = None

        # Load search mode (episode or season)
        search_mode = os.getenv(f"{prefix}_SEARCH_MODE", "episode").lower()
        if search_mode not in ["episode", "season"]:
            logger.warning(f"Invalid SEARCH_MODE '{search_mode}' for {prefix}. Defaulting to 'episode'.")
            search_mode = "episode"

        config = {
            "url": url,
            "api_key": api_key,
            "num_to_upgrade": instance_limit,
            "service_type": service_name.lower(),
            "instance_name": prefix,
            "search_mode": search_mode
        }
        config["num_cutoff_unmet_to_upgrade"] = instance_cutoff_limit
        config["queue_size_limit"] = queue_size_limit

        configs.append(config)
        
        limit_str = 'unlimited' if instance_limit == sys.maxsize else str(instance_limit)
        cutoff_limit_str = 'unlimited' if instance_cutoff_limit == sys.maxsize else str(instance_cutoff_limit)
        queue_limit_str = str(queue_size_limit) if queue_size_limit is not None else 'disabled'
        logger.info(f"Loaded configuration for {prefix} (Cutoff Unmet limit: {cutoff_limit_str}, CF Score limit: {limit_str}, Queue Limit: {queue_limit_str})")
        
        i += 1
    return configs

def trigger_grouped_searches(items_to_search, search_history, dry_run=False):
    """
    Groups items by their service instance and triggers the appropriate search commands.
    This batching is more efficient than sending one search request per item.

    Args:
        items_to_search (list): A list of items to be searched.
        search_history (dict): The search history object to be updated.
        dry_run (bool): If True, logs actions without sending commands or updating history.
    """
    searches_by_service = {}
    # Group items by the service instance they belong to.
    for item in items_to_search:
        service = item['service']
        if service not in searches_by_service:
            searches_by_service[service] = {'movies': [], 'episodes': []}

        if item['type'] == 'movie':
            searches_by_service[service]['movies'].append(item)
        elif item['type'] == 'episode':
            searches_by_service[service]['episodes'].append(item)

    # Update the history before triggering the search to ensure items aren't
    # immediately re-queued if the script runs again quickly.
    if not dry_run:
        update_history(items_to_search, search_history)

    for service, items in searches_by_service.items():
        if items['movies']:
            logger.info(f"Queueing search for {len(items['movies'])} movies on {service.url}")
            for movie in items['movies']:
                logger.info(f"  - {movie['title']}")
            service.trigger_search("MoviesSearch", "movieIds", [m['id'] for m in items['movies']], dry_run)

        if items['episodes']:
            # Check search mode for this service
            # We need to find the config for this service to check the search mode.
            # Since we don't have the config object here, we can infer it or pass it.
            # Better yet, let's assume the first item has the config or we pass it.
            # Actually, 'items' is a list of dicts. We can attach the search mode to the item in main().
            
            # Group by search mode
            episodes_by_mode = {'episode': [], 'season': []}
            for episode in items['episodes']:
                mode = episode.get('search_mode', 'episode')
                episodes_by_mode[mode].append(episode)

            # Handle 'episode' mode searches
            if episodes_by_mode['episode']:
                logger.info(f"Queueing search for {len(episodes_by_mode['episode'])} episodes on {service.url} (Mode: Episode)")
                for episode in episodes_by_mode['episode']:
                    logger.info(f"  - {episode['title']}")
                service.trigger_search("EpisodeSearch", "episodeIds", [e['id'] for e in episodes_by_mode['episode']], dry_run)

            # Handle 'season' mode searches
            if episodes_by_mode['season']:
                # Group by series and season to minimize API calls
                seasons_to_search = {}
                for episode in episodes_by_mode['season']:
                    key = (episode['seriesId'], episode['seasonNumber'])
                    if key not in seasons_to_search:
                        seasons_to_search[key] = []
                    seasons_to_search[key].append(episode)
                
                logger.info(f"Queueing search for {len(seasons_to_search)} seasons on {service.url} (Mode: Season)")
                for (series_id, season_number), episodes in seasons_to_search.items():
                    logger.info(f"  - Series ID: {series_id}, Season: {season_number} (Triggered by {len(episodes)} episodes)")
                    # For SeasonSearch, we need seriesId and seasonNumber
                    # The payload structure for SeasonSearch is usually { "name": "SeasonSearch", "seriesId": X, "seasonNumber": Y }
                    # We can't batch these easily into one call like EpisodeSearch, so we trigger one per season.
                    
                    payload = {
                        "name": "SeasonSearch",
                        "seriesId": series_id,
                        "seasonNumber": season_number
                    }
                    
                    if dry_run:
                        logger.info(f"[DRY RUN] Would trigger SeasonSearch for Series {series_id} Season {season_number}")
                    else:
                        service._post("command", payload)
                        logger.info(f"Triggered SeasonSearch for Series {series_id} Season {season_number}")

def load_history(cooldown_seconds):
    """
    Loads the search history from a JSON file and prunes entries that are older
    than the configured cooldown period.

    Args:
        cooldown_seconds (int): The time in seconds to keep an item in history.

    Returns:
        dict: The pruned search history.
    """
    try:
        with open(HISTORY_FILE, 'r') as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("No valid search history found. Starting fresh.")
        return {}

    # Prune old entries to keep the history file from growing indefinitely.
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
    """
    Saves the provided search history data to the JSON file.

    Args:
        history_data (dict): The search history to save.
    """
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history_data, f, indent=4)
    except IOError as e:
        logger.error(f"Failed to save search history to {HISTORY_FILE}: {e}")

def update_history(searched_items, search_history):
    """
    Updates the search history with the items that were just searched.

    Args:
        searched_items (list): The list of items that have been queued for search.
        search_history (dict): The current search history object to update.
    """
    for item in searched_items:
        # Create a unique key for each item based on its service type and ID.
        search_history[f"{item['service_type']}-{item['id']}"] = time.time()

def update_cron_schedule():
    """
    Checks the current environment variables for CRON_SCHEDULE or SEARCH_INTERVAL
    and updates the system cron job if the schedule has changed.
    This allows users to update the schedule in .env without restarting the container.
    """
    cron_schedule = os.getenv("CRON_SCHEDULE")
    search_interval = os.getenv("SEARCH_INTERVAL")
    
    final_schedule = "0 2 * * *" # Default

    if cron_schedule:
        final_schedule = cron_schedule
        logger.debug(f"Using CRON_SCHEDULE: {final_schedule}")
    elif search_interval:
        logger.debug(f"Parsing SEARCH_INTERVAL: {search_interval}")
        unit = search_interval[-1]
        try:
            number = int(search_interval[:-1])
            if unit == 'm':
                final_schedule = f"*/{number} * * * *"
            elif unit == 'h':
                final_schedule = f"0 */{number} * * *"
            elif unit == 'd':
                final_schedule = f"0 0 */{number} * *"
            else:
                logger.warning(f"Invalid unit '{unit}' in SEARCH_INTERVAL. Using default: {final_schedule}")
        except ValueError:
            logger.warning(f"Invalid number in SEARCH_INTERVAL '{search_interval}'. Using default: {final_schedule}")
    else:
        logger.debug(f"No schedule set. Using default: {final_schedule}")

    cron_file_path = "/etc/cron.d/my-cron-job.actual"
    expected_cron_line = f"{final_schedule} /usr/local/bin/python /app/app.py > /proc/1/fd/1 2>&1\n"

    try:
        current_content = ""
        if os.path.exists(cron_file_path):
            with open(cron_file_path, 'r') as f:
                current_content = f.read()

        if current_content != expected_cron_line:
            logger.info(f"Cron schedule changed. Updating {cron_file_path}...")
            logger.info(f"New schedule: {final_schedule}")
            with open(cron_file_path, 'w') as f:
                f.write(expected_cron_line)
            
            # Reload cron
            os.system(f"crontab {cron_file_path}")
            logger.info("Cron reloaded successfully.")
        else:
            logger.debug("Cron schedule is up to date.")

    except Exception as e:
        logger.error(f"Failed to update cron schedule: {e}")

def main():
    """Main execution function."""
    logger.info("======================================================")
    logger.info("Starting Custom Format Search script")

    # --- Configuration Loading ---
    # Load all settings from environment variables, providing sensible defaults.
    # Reload .env file to pick up any changes
    load_dotenv(dotenv_path="/config/.env", override=True)

    # Check and update cron schedule if needed
    update_cron_schedule()

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    if dry_run:
        logger.info("DRY RUN is enabled. No actual search commands will be sent.")

    debug_mode = os.getenv("DEBUG_MODE", "false").lower() == "true"
    if debug_mode:
        logger.info("DEBUG MODE is enabled. Detailed item lists will be saved.")

    try:
        cooldown_days = int(os.getenv("HISTORY_COOLDOWN_DAYS", "30"))
        if cooldown_days < 0:
            cooldown_days = 0 # A negative cooldown is not logical.
    except ValueError:
        cooldown_days = 30
    cooldown_seconds = cooldown_days * 86400
    logger.info(f"Search history cooldown is set to {cooldown_days} days.")

    try:
        delay_between_instances = int(os.getenv("DELAY_BETWEEN_INSTANCES", "10"))
        if delay_between_instances < 0:
            logger.warning(f"Invalid DELAY_BETWEEN_INSTANCES. Using 0.")
            delay_between_instances = 0
    except ValueError:
        logger.warning(f"Invalid DELAY_BETWEEN_INSTANCES. Using default of 10.")
        delay_between_instances = 10

    try:
        max_upgrades = int(os.getenv("MAX_UPGRADES", "20"))
        if max_upgrades < 0:
            # A negative global limit is treated as no limit.
            max_upgrades = sys.maxsize
            logger.info("MAX_UPGRADES is negative, disabling global limit.")
    except ValueError:
        logger.warning(f"Invalid MAX_UPGRADES value. Disabling global limit.")
        max_upgrades = sys.maxsize

    logger.info(f"Global upgrade limit is set to {max_upgrades if max_upgrades != sys.maxsize else 'unlimited'}.")

    # --- Main Processing Loop ---
    all_configs = load_configs("RADARR") + load_configs("SONARR")
    search_history = load_history(cooldown_seconds)

    if max_upgrades == 0:
        logger.info("MAX_UPGRADES is set to 0. No searches will be performed.")
        all_configs = [] # Clear configs to prevent any processing.

    remaining_global_upgrades = max_upgrades

    for i, config in enumerate(all_configs):
        items_to_search = []
        service = None

        get_upgradeables_func = get_radarr_upgradeables if config['service_type'] == 'radarr' else get_sonarr_upgradeables
        service, cutoff_unmet_items, cf_upgradeable_items = get_upgradeables_func(config, search_history, cooldown_seconds, debug_mode)

        # 1. Select items that have not met their quality cutoff
        cutoff_limit = config['num_cutoff_unmet_to_upgrade']
        num_to_select_cutoff = min(len(cutoff_unmet_items), cutoff_limit, remaining_global_upgrades)
        cutoff_selected = random.sample(cutoff_unmet_items, k=num_to_select_cutoff)
        items_to_search.extend(cutoff_selected)
        
        remaining_global_upgrades -= len(cutoff_selected)
        
        # 2. Select items for custom format score upgrade
        if remaining_global_upgrades > 0:
            cf_limit = config['num_to_upgrade']
            num_to_select_cf = min(len(cf_upgradeable_items), cf_limit, remaining_global_upgrades)
            cf_selected = random.sample(cf_upgradeable_items, k=num_to_select_cf)
            items_to_search.extend(cf_selected)
            remaining_global_upgrades -= len(cf_selected)

        if not items_to_search:
            logger.info(f"No items to search for on instance {config['instance_name']}.")
            if delay_between_instances > 0 and i < len(all_configs) - 1:
                logger.info(f"Waiting for {delay_between_instances} seconds before processing next instance...")
                if not dry_run:
                    time.sleep(delay_between_instances)
            continue

        # Attach service and type information to each item for later use.
        for item in items_to_search:
            item['service'] = service # service is guaranteed to be set if items_to_search is not empty
            item['service_type'] = config['service_type']
            if config['service_type'] == 'sonarr':
                item['search_mode'] = config.get('search_mode', 'episode')

        trigger_grouped_searches(items_to_search, search_history, dry_run=dry_run)
        
        # Stop processing if the global upgrade limit for this run has been reached.
        if remaining_global_upgrades <= 0:
            logger.info(f"Global upgrade limit of {max_upgrades} reached. No more instances will be processed in this run.")
            break

        # Pause between processing instances to be respectful to APIs.
        if delay_between_instances > 0 and i < len(all_configs) - 1:
            logger.info(f"Waiting for {delay_between_instances} seconds before processing next instance...")
            if not dry_run:
                time.sleep(delay_between_instances)

    # Persist the updated history to disk at the end of the run.
    if not dry_run:
        save_history(search_history)

    logger.info("Script finished.")
    logger.info("======================================================")

if __name__ == "__main__":
    main()
