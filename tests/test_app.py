import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import time
import logging

# Patch FileHandler BEFORE importing app to avoid FileNotFoundError on /config/output.log
# This is necessary because app.py initializes the logger at the module level.
mock_handler = MagicMock()
mock_handler.level = logging.INFO
patcher = patch('logging.FileHandler', return_value=mock_handler)
patcher.start()

# Add parent directory to path to import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import get_sonarr_upgradeables, update_history

# Stop the patcher after import
patcher.stop()

class TestApp(unittest.TestCase):

    def setUp(self):
        self.config = {
            'url': 'http://test',
            'api_key': 'test',
            'instance_name': 'SONARR0',
            'queue_size_limit': 100
        }
        self.search_history = {}
        self.cooldown_seconds = 3600

    @patch('app.ArrService')
    def test_issue_3_sonarr_score_logic(self, MockArrService):
        """
        Test that an episode is upgraded if it meets the quality cutoff 
        BUT is below the custom format score cutoff.
        """
        service = MockArrService.return_value
        service.test_connection.return_value = True
        service.get_queue_size.return_value = 0
        
        # Profile: Cutoff is met (quality-wise) but score cutoff is 1000
        service.get_quality_profile_details.return_value = {
            1: {'cutoffFormatScore': 1000, 'cutoffQualityName': 'HD-1080p'}
        }
        
        # Series using profile 1
        service._get.side_effect = lambda endpoint, params=None: {
            'series': [{
                'id': 1, 'title': 'Test Series', 'qualityProfileId': 1, 
                'statistics': {'episodeFileCount': 1}, 'monitored': True
            }],
            'episodefile': [{
                'id': 100, 'seriesId': 1, 'qualityCutoffNotMet': False, # Quality met!
                'customFormatScore': 500 # Score (500) < Cutoff (1000) -> Should upgrade
            }],
            'episode': [{
                'id': 10, 'seriesId': 1, 'episodeFileId': 100, 
                'seasonNumber': 1, 'episodeNumber': 1, 'title': 'Test Ep',
                'monitored': True, 'hasFile': True
            }]
        }.get(endpoint)

        _, cutoff_unmet, cf_upgradeable = get_sonarr_upgradeables(self.config, self.search_history, self.cooldown_seconds)
        
        # Should NOT be in cutoff_unmet (because qualityCutoffNotMet is False)
        self.assertEqual(len(cutoff_unmet), 0, "Should not be in cutoff_unmet list")
        
        # SHOULD be in cf_upgradeable (because 500 < 1000)
        # This fails in the current buggy implementation
        self.assertEqual(len(cf_upgradeable), 1, "Should be in cf_upgradeable list due to score")
        self.assertEqual(cf_upgradeable[0]['id'], 10)

    def test_issue_5_season_search_history(self):
        """
        Test that season search history is correctly updated and respected.
        """
        # 1. Test update_history adds season key
        items = [{
            'service_type': 'sonarr',
            'id': 10,
            'seriesId': 1,
            'seasonNumber': 1,
            'search_mode': 'season'
        }]
        
        update_history(items, self.search_history)
        
        # Check if season key exists
        season_key = "sonarr-series-1-season-1"
        self.assertIn(season_key, self.search_history)
        
        # 2. Test get_sonarr_upgradeables respects season key
        # Mock service again
        with patch('app.ArrService') as MockArrService:
            service = MockArrService.return_value
            service.test_connection.return_value = True
            service.get_queue_size.return_value = 0
            service.get_quality_profile_details.return_value = {
                1: {'cutoffFormatScore': 1000, 'cutoffQualityName': 'HD-1080p'}
            }
            
            # Setup data for an episode that WOULD be upgradeable
            service._get.side_effect = lambda endpoint, params=None: {
                'series': [{
                    'id': 1, 'title': 'Test Series', 'qualityProfileId': 1, 
                    'statistics': {'episodeFileCount': 1}, 'monitored': True
                }],
                'episodefile': [{
                    'id': 100, 'seriesId': 1, 'qualityCutoffNotMet': True,
                    'customFormatScore': 0
                }],
                'episode': [{
                    'id': 10, 'seriesId': 1, 'episodeFileId': 100, 
                    'seasonNumber': 1, 'episodeNumber': 1, 'title': 'Test Ep',
                    'monitored': True, 'hasFile': True
                }]
            }.get(endpoint)
            
            # Run with history containing the season key
            # We just added it above, so it should be there.
            # Ensure timestamp is recent so it's within cooldown
            self.search_history[season_key] = time.time()
            
            _, cutoff_unmet, cf_upgradeable = get_sonarr_upgradeables(self.config, self.search_history, self.cooldown_seconds)
            
            # Should be skipped because the SEASON was recently searched
            self.assertEqual(len(cutoff_unmet), 0, "Should be skipped due to season history")
            self.assertEqual(len(cf_upgradeable), 0, "Should be skipped due to season history")

if __name__ == '__main__':
    unittest.main()
