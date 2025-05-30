from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import os
import requests as requests
import random

# Load .env
load_dotenv()

# Set variables
API_KEY = os.getenv("RADARR_API_KEY")
RADARR_URL = os.getenv("RADARR_URL")
NUM_MOVIES_TO_UPGRADE = int(os.getenv("NUM_MOVIES_TO_UPGRADE"))
API_PATH = "/api/v3/"
MOVIE_ENDPOINT = "movie"
MOVIEFILE_ENDPOINT = "moviefile/"
QUALITY_PROFILE_ENDPOINT = "qualityprofile"
COMMAND_ENDPOINT = "command"

# Set Authorization headers for API calls
headers = {
    'Authorization': API_KEY,
}

quality_to_formats = {}

def get_quality_cutoff_scores():
    QUALITY_PROFILES_GET_API_CALL = RADARR_URL + API_PATH + QUALITY_PROFILE_ENDPOINT
    quality_profiles = requests.get(QUALITY_PROFILES_GET_API_CALL, headers=headers).json()
    for quality in quality_profiles:
        quality_to_formats.update({quality["id"]: quality["cutoffFormatScore"]})

# Get all movies and return a dictionary of movies
def get_movies():
    MOVIES_GET_API_CALL = RADARR_URL + API_PATH + MOVIE_ENDPOINT
    movies = requests.get(MOVIES_GET_API_CALL, headers=headers).json()
    return movies

# Get all moviefiles for all movies and if moviefile exists and the customFormatScore is less than the wanted score, add it to dictionary and return dictionary
def get_movie_files(movies):
    movie_files = {}
    for movie in movies:
        if movie["movieFileId"] > 0:
            MOVIE_FILE_GET_API_CALL = RADARR_URL + API_PATH + MOVIEFILE_ENDPOINT + str(movie["movieFileId"])
            movie_file = requests.get(MOVIE_FILE_GET_API_CALL, headers=headers).json()
            movie_quality_profile_id = movie["qualityProfileId"]
            # Build dictionary of movie files needing upgrades
            if movie_file["customFormatScore"] < quality_to_formats[movie_quality_profile_id]:
                movie_files[movie["id"]] = {}
                movie_files[movie["id"]]["title"] = movie["title"]
                movie_files[movie["id"]]["customFormatScore"] = movie_file["customFormatScore"]
                movie_files[movie["id"]]["wantedCustomFormatScore"] = quality_to_formats[movie_quality_profile_id]
    return movie_files


# Get all quality profile ids and their cutoff scores and add to dictionary
get_quality_cutoff_scores()

# Select random movies to upgrade
random_keys = random.choices(list(get_movie_files(get_movies()).keys()), k=NUM_MOVIES_TO_UPGRADE)

# Set data payload for the movies to search
data = {
    "name": "MoviesSearch",
    "movieIds": random_keys
}
# Do the thing
SEARCH_MOVIES_POST_API_CALL = RADARR_URL + API_PATH + COMMAND_ENDPOINT
requests.post(SEARCH_MOVIES_POST_API_CALL, headers=headers, json=data)