"""Plugin test fixtures for disney_parks_times."""

import pytest


@pytest.fixture(autouse=True)
def reset_plugin_singletons():
    """Clear the module-level catalog caches so tests cannot leak into each other."""
    import plugins.disney_parks_times as mod

    mod._park_names_cache = {}
    mod._park_names_cache_time = 0
    mod._park_catalog_cache = []
    mod._park_catalog_cache_time = 0
    yield
    mod._park_names_cache = {}
    mod._park_names_cache_time = 0
    mod._park_catalog_cache = []
    mod._park_catalog_cache_time = 0


# Queue-Times catalog used by the get_options providers. Deliberately includes a
# non-Disney operator group and three Disney parks in non-alphabetical order.
FULL_PARKS_JSON = [
    {
        "id": 1,
        "name": "Merlin Entertainments",
        "parks": [{"id": 1, "name": "Alton Towers", "country": "United Kingdom"}],
    },
    {
        "id": 2,
        "name": "Walt Disney Attractions",
        "parks": [
            {"id": 16, "name": "Disneyland", "country": "United States"},
            {"id": 6, "name": "Magic Kingdom", "country": "United States"},
            {"id": 17, "name": "Disney California Adventure", "country": "United States"},
        ],
    },
]

# Per-park ride catalogs, keyed by park id.
PARK_RIDES_JSON = {
    16: {
        "lands": [
            {
                "id": 117,
                "name": "Tomorrowland",
                "rides": [
                    {"id": 284, "name": "Space Mountain", "is_open": True, "wait_time": 45},
                    {"id": 279, "name": "Matterhorn Bobsleds", "is_open": False, "wait_time": 0},
                ],
            },
            {
                "id": 118,
                "name": "New Orleans Square",
                "rides": [
                    {"id": 291, "name": "Haunted Mansion", "is_open": True, "wait_time": 25},
                ],
            },
        ],
        "rides": [],
    },
    17: {
        "lands": [
            {
                "id": 201,
                "name": "Cars Land",
                "rides": [
                    {"id": 329, "name": "Radiator Springs Racers", "is_open": True, "wait_time": 90},
                ],
            },
        ],
        "rides": [],
    },
    6: {"lands": [], "rides": []},
}


@pytest.fixture
def patched_queue_times():
    """Patch requests.get with the full Queue-Times catalog above."""
    from unittest.mock import Mock, patch

    def side_effect(url, timeout=None):
        if "parks.json" in url:
            return Mock(json=Mock(return_value=FULL_PARKS_JSON), raise_for_status=Mock())
        for pid, payload in PARK_RIDES_JSON.items():
            if f"/parks/{pid}/queue_times.json" in url:
                return Mock(json=Mock(return_value=payload), raise_for_status=Mock())
        raise AssertionError(f"unexpected URL: {url}")

    with patch("plugins.disney_parks_times.requests.get", side_effect=side_effect) as m:
        yield m


@pytest.fixture
def sample_manifest():
    """Return sample manifest for testing."""
    return {
        "id": "disney_parks_times",
        "name": "Disney Park Queue Times",
        "version": "1.0.0",
        "settings_schema": {
            "type": "object",
            "properties": {
                "parks": {"type": "array", "items": {"type": "object"}},
                "refresh_seconds": {"type": "integer", "default": 300},
            },
            "required": ["parks"],
        },
        "variables": {
            "groups": {"display": {"label": "Display"}},
            "simple": {
                "formatted": {"description": "Pre-formatted ride wait times display", "type": "string", "max_length": 22, "group": "display", "example": "Space Mtn: 45m"},
            },
            "arrays": {"parks": {"label_field": "park_name", "item_fields": ["park_name"], "sub_arrays": {"rides": {"label_field": "ride_label", "item_fields": ["ride_name", "ride_label", "ride_abbr", "tiny_abbr", "custom_name", "wait_time", "is_open", "status", "state_color", "formatted"]}}}},
        },
        "max_lengths": {"parks.*.park_name": 22, "parks.*.rides.*.ride_name": 22},
    }


@pytest.fixture
def sample_config():
    """Return sample configuration for testing."""
    return {
        "parks": [
            {"park_id": 16, "ride_ids": [284, 279]},
        ],
        "refresh_seconds": 300,
        "enabled": True,
    }


@pytest.fixture
def parks_json_response():
    """Queue-Times parks.json (Disney group only)."""
    return [
        {"id": 11, "name": "Other", "parks": []},
        {
            "id": 2,
            "name": "Walt Disney Attractions",
            "parks": [
                {"id": 16, "name": "Disneyland", "country": "United States", "timezone": "America/Los_Angeles"},
                {"id": 17, "name": "Disney California Adventure", "country": "United States", "timezone": "America/Los_Angeles"},
            ],
        },
    ]


@pytest.fixture
def queue_times_json_response():
    """Queue-Times queue_times.json for one park."""
    return {
        "lands": [
            {
                "id": 117,
                "name": "Tomorrowland",
                "rides": [
                    {"id": 284, "name": "Space Mountain", "is_open": True, "wait_time": 60, "last_updated": "2026-02-14T18:05:39.000Z"},
                    {"id": 279, "name": "Matterhorn Bobsleds", "is_open": True, "wait_time": 55, "last_updated": "2026-02-14T18:05:39.000Z"},
                ],
            },
        ],
        "rides": [],
    }
