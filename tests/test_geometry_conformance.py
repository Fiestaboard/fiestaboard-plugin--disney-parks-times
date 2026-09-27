"""Board-geometry conformance for the disney_parks_times plugin.

Runs the shared cross-repo suite (see src/plugins/geometry_conformance.py in
FiestaBoard core) against this plugin: every board shape from a Note (15x3)
up to the largest note-array/FiestaPanel (120x24) must render without
crashing, overflowing a row, or overflowing a column -- and, since this
plugin renders a list of rides, a taller board must show strictly more of
them once a shorter one is full (strict_growth=True).
"""

import json
from pathlib import Path
from unittest.mock import Mock

from plugins.disney_parks_times import DisneyParksTimesPlugin
from src.plugins.geometry_conformance import assert_board_conformance

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text())

# Disney's own group id in the upstream parks.json. Park names use each
# park's short form (e.g. "California Adventure", not the official "Disney
# California Adventure Park") so this fixture stays <=22 tiles and does not
# trip the manifest's max_lengths bound on park_name -- see the ride-name
# note below, which is the same pre-existing concern.
_PARKS_JSON = [
    {
        "id": 2,
        "name": "Walt Disney Attractions",
        "parks": [
            {"id": 16, "name": "Disneyland", "country": "United States"},
            {"id": 17, "name": "California Adventure", "country": "United States"},
            {"id": 6, "name": "Magic Kingdom", "country": "United States"},
        ],
    }
]

# Ten rides per park, alternating open/closed, with SHORT names (<=22 tiles)
# so this fixture never trips the manifest's own max_lengths bounds on
# ride_name/ride_label -- that is a separate, pre-existing concern (Disney's
# real ride names, e.g. "Mickey and Minnie's Runaway Railway", already run
# longer than the declared bound) and not one of the findings this fix
# addresses.
#
# Thirty rides across three parks is deliberately more than the largest
# geometry in the growth ladder can show (15x24 fits at most ~23 ride lines
# -- see _build_formatted_lines): with fewer rides than that, a taller board
# would run out of content and "growing" would prove nothing.
def _rides_payload(park_prefix: int, count: int) -> dict:
    return {
        "lands": [
            {
                "id": 1,
                "name": "Land",
                "rides": [
                    {
                        "id": park_prefix * 100 + i,
                        "name": f"Ride {park_prefix}-{i}",
                        "is_open": i % 3 != 0,
                        "wait_time": 5 + i,
                    }
                    for i in range(count)
                ],
            }
        ],
        "rides": [],
    }


_RIDE_PAYLOADS = {
    16: _rides_payload(1, 10),
    17: _rides_payload(2, 10),
    6: _rides_payload(3, 10),
}


def _make_plugin(monkeypatch) -> DisneyParksTimesPlugin:
    """A fresh, configured plugin with the network already stubbed."""

    def side_effect(url, timeout=None):
        if "parks.json" in url:
            return Mock(json=Mock(return_value=_PARKS_JSON), raise_for_status=Mock())
        for park_id, payload in _RIDE_PAYLOADS.items():
            if f"/parks/{park_id}/queue_times.json" in url:
                return Mock(json=Mock(return_value=payload), raise_for_status=Mock())
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(
        "plugins.disney_parks_times.requests.get",
        Mock(side_effect=side_effect),
    )

    plugin = DisneyParksTimesPlugin(MANIFEST)
    plugin.enabled = True
    plugin.config = {
        "enabled": True,
        "refresh_seconds": 300,
        "parks": [
            {
                "park_id": park_id,
                "ride_ids": [ride["id"] for land in payload["lands"] for ride in land["rides"]],
            }
            for park_id, payload in _RIDE_PAYLOADS.items()
        ],
    }
    return plugin


def test_renders_on_every_board_shape(monkeypatch):
    """Every board shape from a Note up to the largest note array/panel."""

    def factory() -> DisneyParksTimesPlugin:
        return _make_plugin(monkeypatch)

    assert_board_conformance(
        factory,
        manifest=MANIFEST,
        strict_growth=True,
        require_note_array_preview=True,
    )
