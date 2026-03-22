"""Tests for the disney_parks_times plugin."""

import json
from pathlib import Path

import pytest
from unittest.mock import patch, Mock

from plugins.disney_parks_times import DisneyParksTimesPlugin, _tiny_abbr

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"
REQUIRED_VAR_FIELDS = {"description", "type", "max_length", "group", "example"}


class TestDisneyParksTimesPlugin:
    """Test Disney Park Queue Times plugin."""

    def test_plugin_id(self, sample_manifest):
        """Plugin ID matches manifest."""
        plugin = DisneyParksTimesPlugin(sample_manifest)
        assert plugin.plugin_id == "disney_parks_times"

    def test_validate_config_valid(self, sample_manifest, sample_config):
        """Validate config with valid parks and rides."""
        plugin = DisneyParksTimesPlugin(sample_manifest)
        errors = plugin.validate_config(sample_config)
        assert errors == []

    def test_validate_config_empty_parks(self, sample_manifest):
        """Validate config requires at least one park."""
        plugin = DisneyParksTimesPlugin(sample_manifest)
        errors = plugin.validate_config({"parks": []})
        assert len(errors) > 0
        assert "one park" in errors[0].lower()

    def test_validate_config_park_with_no_rides(self, sample_manifest):
        """Validate config requires at least one ride per park."""
        plugin = DisneyParksTimesPlugin(sample_manifest)
        errors = plugin.validate_config({"parks": [{"park_id": 16, "ride_ids": []}]})
        assert len(errors) > 0
        assert "ride" in errors[0].lower()

    def test_fetch_data_no_config(self, sample_manifest):
        """fetch_data returns error when no parks configured."""
        plugin = DisneyParksTimesPlugin(sample_manifest)
        plugin.config = {}
        result = plugin.fetch_data()
        assert result.available is False
        assert "configure" in (result.error or "").lower()

    @patch("plugins.disney_parks_times.requests.get")
    def test_fetch_data_success(
        self, mock_get, sample_manifest, sample_config, parks_json_response, queue_times_json_response
    ):
        """fetch_data returns parks and rides with wait times."""
        def side_effect(url, timeout=None):
            if "parks.json" in url:
                r = Mock()
                r.json.return_value = parks_json_response
                r.raise_for_status = Mock()
                return r
            if "queue_times.json" in url:
                r = Mock()
                r.json.return_value = queue_times_json_response
                r.raise_for_status = Mock()
                return r
            return Mock(json=Mock(return_value=[]), raise_for_status=Mock())

        mock_get.side_effect = side_effect
        plugin = DisneyParksTimesPlugin(sample_manifest)
        plugin.config = sample_config
        plugin._cache = None
        plugin._cache_time = 0

        result = plugin.fetch_data()

        assert result.available is True
        assert result.data is not None
        assert "parks" in result.data
        parks = result.data["parks"]
        assert len(parks) >= 1
        assert parks[0]["park_name"] == "Disneyland"
        assert "rides" in parks[0]
        rides = parks[0]["rides"]
        assert len(rides) >= 1
        assert rides[0]["ride_name"] == "Space Mountain"
        assert rides[0]["wait_time"] == 60
        assert rides[0]["is_open"] is True
        # Formatted has no space between color and abbreviation (board shows one tile, not blank then abbr)
        formatted = rides[0]["formatted"]
        assert formatted.startswith("{66}SMNT"), f"formatted should be color then abbr with no space: {formatted!r}"
        assert result.formatted_lines is not None
        assert len(result.formatted_lines) <= 6

    @patch("plugins.disney_parks_times.requests.get")
    def test_fetch_data_api_error(self, mock_get, sample_manifest, sample_config):
        """fetch_data returns partial data with Unavailable when API fails for a park."""
        mock_get.side_effect = Exception("Network error")
        plugin = DisneyParksTimesPlugin(sample_manifest)
        plugin.config = sample_config
        plugin._cache = None

        result = plugin.fetch_data()

        assert result.available is True
        assert result.data is not None
        parks = result.data.get("parks", [])
        assert len(parks) == 1
        assert any(r.get("ride_name") == "Unavailable" or r.get("status") == "Error" for r in parks[0].get("rides", []))

    @patch("plugins.disney_parks_times.requests.get")
    def test_get_formatted_display(
        self, mock_get, sample_manifest, sample_config, parks_json_response, queue_times_json_response
    ):
        """get_formatted_display returns up to 6 lines."""
        def side_effect(url, timeout=None):
            if "parks.json" in url:
                r = Mock()
                r.json.return_value = parks_json_response
                r.raise_for_status = Mock()
                return r
            if "queue_times.json" in url:
                r = Mock()
                r.json.return_value = queue_times_json_response
                r.raise_for_status = Mock()
                return r
            return Mock(json=Mock(return_value=[]), raise_for_status=Mock())

        mock_get.side_effect = side_effect
        plugin = DisneyParksTimesPlugin(sample_manifest)
        plugin.config = sample_config
        plugin._cache = None

        lines = plugin.get_formatted_display()

        assert lines is not None
        assert len(lines) <= 6
        assert any("Queue" in (line or "") for line in lines)
        assert any("Queue-Times" in (line or "") or "queue" in (line or "").lower() for line in lines)

    def test_get_formatted_display_no_cache_no_config(self, sample_manifest):
        """get_formatted_display returns None when no config and no cache."""
        plugin = DisneyParksTimesPlugin(sample_manifest)
        plugin.config = {}
        plugin._cache = None
        lines = plugin.get_formatted_display()
        assert lines is None

    def test_cleanup(self, sample_manifest):
        """cleanup clears cache."""
        plugin = DisneyParksTimesPlugin(sample_manifest)
        plugin._cache = {"parks": []}
        plugin.cleanup()
        assert plugin._cache is None

    @patch("plugins.disney_parks_times.requests.get")
    def test_fetch_data_uses_cache_within_refresh(
        self, mock_get, sample_manifest, sample_config, parks_json_response, queue_times_json_response
    ):
        """Second fetch within refresh_seconds returns cached data without new requests."""
        def side_effect(url, timeout=None):
            if "parks.json" in url:
                r = Mock()
                r.json.return_value = parks_json_response
                r.raise_for_status = Mock()
                return r
            if "queue_times.json" in url:
                r = Mock()
                r.json.return_value = queue_times_json_response
                r.raise_for_status = Mock()
                return r
            return Mock(json=Mock(return_value=[]), raise_for_status=Mock())

        mock_get.side_effect = side_effect
        plugin = DisneyParksTimesPlugin(sample_manifest)
        plugin.config = {**sample_config, "refresh_seconds": 300}
        plugin._cache = None

        result1 = plugin.fetch_data()
        assert result1.available is True
        first_call_count = mock_get.call_count

        result2 = plugin.fetch_data()
        assert result2.available is True
        assert result2.data is result1.data
        # No additional requests when using cache
        assert mock_get.call_count == first_call_count

    def test_fetch_data_no_parks_data_returns_error(self, sample_manifest):
        """When no valid park config (e.g. invalid park_id), fetch_data returns available=False."""
        plugin = DisneyParksTimesPlugin(sample_manifest)
        # Invalid park_id so the loop never appends to parks_data
        plugin.config = {"parks": [{"park_id": "not_an_int", "ride_ids": [284]}]}
        plugin._cache = None

        result = plugin.fetch_data()

        assert result.available is False
        assert "no park data" in (result.error or "").lower() or "check" in (result.error or "").lower()

    @patch("plugins.disney_parks_times.requests.get")
    def test_build_formatted_lines_content(
        self, mock_get, sample_manifest, sample_config, parks_json_response, queue_times_json_response
    ):
        """Default formatted lines have title, ride lines, and attribution."""
        def side_effect(url, timeout=None):
            if "parks.json" in url:
                r = Mock()
                r.json.return_value = parks_json_response
                r.raise_for_status = Mock()
                return r
            if "queue_times.json" in url:
                r = Mock()
                r.json.return_value = queue_times_json_response
                r.raise_for_status = Mock()
                return r
            return Mock(json=Mock(return_value=[]), raise_for_status=Mock())

        mock_get.side_effect = side_effect
        plugin = DisneyParksTimesPlugin(sample_manifest)
        plugin.config = sample_config
        plugin._cache = None

        lines = plugin.get_formatted_display()

        assert lines is not None
        assert len(lines) == 6
        assert "DISNEY" in (lines[0] or "") and "QUEUE" in (lines[0] or "")
        assert "Queue-Times.com" in (lines[5] or "")
        # At least one ride line (Space Mountain 60m from fixture)
        assert any("60" in (line or "") or "Closed" in (line or "") for line in lines[1:5])


class TestTinyAbbr:
    """Test known tiny abbreviations for rides."""

    def test_rise_of_the_resistance_rise(self):
        """Rise of the Resistance uses common abbreviation RISE."""
        assert _tiny_abbr("Star Wars: Rise of the Resistance") == "RISE"
        assert _tiny_abbr("Rise of the Resistance") == "RISE"

    def test_other_known_abbreviations(self):
        """Other Disney rides use common fan abbreviations."""
        assert _tiny_abbr("Space Mountain") == "SMNT"
        assert _tiny_abbr("Haunted Mansion") == "HM"
        assert _tiny_abbr("Big Thunder Mountain Railroad") == "THUND"
        assert _tiny_abbr("Seven Dwarfs Mine Train") == "7DMT"
        assert _tiny_abbr("Jungle Cruise") == "JUNGL"
        assert _tiny_abbr("It's a Small World") == "SMALL"
        assert _tiny_abbr("Indiana Jones Adventure") == "INDY"
        assert _tiny_abbr("Buzz Lightyear Astro Blasters") == "BUZZ"

    def test_unknown_ride_fallback(self):
        """Unknown ride name falls back to first 5 chars (spaces removed)."""
        assert _tiny_abbr("Some Other Ride") == "SOMEO"

    def test_single_rider_gets_trailing_one(self):
        """Single-rider lines get '1' suffix so they differ from main line."""
        assert _tiny_abbr("Space Mountain - Single Rider") == "SMNT1"
        assert _tiny_abbr("Haunted Mansion Single Rider") == "HM1"
        assert _tiny_abbr("Big Thunder Mountain Railroad - Single Rider") == "THUN1"  # 5 chars: trim one, add 1
        assert _tiny_abbr("Seven Dwarfs Mine Train - Single Rider") == "7DMT1"

    def test_no_spaces_in_abbreviations(self):
        """Abbreviations never contain spaces so the board doesn't show blank tiles between letters."""
        assert " " not in _tiny_abbr("Jungle Cruise")
        assert " " not in _tiny_abbr("Space Mountain")
        # Fallback: name with spaces is collapsed then truncated
        abbr = _tiny_abbr("Some Weird Ride Name")
        assert " " not in abbr
        assert len(abbr) <= 5

    def test_tiny_abbr_empty_name_returns_empty(self):
        """Empty or whitespace name returns empty string."""
        assert _tiny_abbr("") == ""
        assert _tiny_abbr("   ") == ""


class TestManifestMetadata:
    """Validate the rich variable metadata in manifest.json."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        with open(MANIFEST_PATH) as f:
            self.manifest = json.load(f)
        self.variables = self.manifest["variables"]
        self.simple = self.variables["simple"]
        self.groups = self.variables["groups"]

    def test_required_top_level_fields(self):
        for field in ("id", "name", "version"):
            assert field in self.manifest

    def test_simple_is_dict(self):
        assert isinstance(self.simple, dict), "variables.simple must be a dict, not a list"

    def test_expected_simple_variable_count(self):
        assert len(self.simple) == 1

    def test_all_expected_simple_vars_present(self):
        assert set(self.simple.keys()) == {"formatted"}

    def test_each_simple_variable_has_required_fields(self):
        for var_name, meta in self.simple.items():
            missing = REQUIRED_VAR_FIELDS - set(meta.keys())
            assert not missing, f"{var_name} missing fields: {missing}"

    def test_groups_section_exists(self):
        assert isinstance(self.groups, dict)
        assert len(self.groups) >= 1

    def test_every_variable_references_valid_group(self):
        for var_name, meta in self.simple.items():
            assert meta["group"] in self.groups, (
                f"{var_name} references unknown group '{meta['group']}'"
            )

    def test_max_length_is_positive_int(self):
        for var_name, meta in self.simple.items():
            ml = meta["max_length"]
            assert isinstance(ml, int) and ml > 0, (
                f"{var_name}: max_length must be a positive int, got {ml}"
            )

    def test_type_values_are_valid(self):
        allowed = {"string", "number", "boolean"}
        for var_name, meta in self.simple.items():
            assert meta["type"] in allowed, (
                f"{var_name}: invalid type '{meta['type']}'"
            )

    def test_arrays_section_has_parks(self):
        arrays = self.variables.get("arrays", {})
        assert "parks" in arrays, "parks array must be defined"
        assert "item_fields" in arrays["parks"]
        assert "label_field" in arrays["parks"]

    def test_parks_sub_arrays_has_rides(self):
        parks = self.variables["arrays"]["parks"]
        assert "sub_arrays" in parks, "parks must have sub_arrays"
        rides = parks["sub_arrays"]["rides"]
        assert "label_field" in rides
        assert "item_fields" in rides
        expected_fields = {"ride_name", "ride_abbr", "tiny_abbr", "wait_time", "is_open", "status", "state_color", "formatted"}
        assert set(rides["item_fields"]) == expected_fields

    def test_max_lengths_keys_use_dot_star_notation(self):
        ml = self.manifest.get("max_lengths", {})
        for key in ml:
            assert "." in key, f"max_lengths key '{key}' should use dot-star notation"
