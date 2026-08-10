"""Tests for the remote-options migration of disney_parks_times.

Covers the manifest grammar core validates, the ``get_options`` providers that
back the two pickers, and the backward-compatibility guarantees that let a
config saved by the old bespoke widget keep working untouched.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from plugins.disney_parks_times import DisneyParksTimesPlugin
from src.plugins.base import OptionsRequest, OptionsUnavailable

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"


def _plugin(manifest, config=None):
    plugin = DisneyParksTimesPlugin(manifest)
    plugin.config = config or {}
    return plugin


def _options(plugin, options_id, **kwargs):
    """Call get_options and normalise a bare list into an OptionsResult."""
    from src.plugins.base import OptionsResult

    result = plugin.get_options(OptionsRequest(options_id=options_id, **kwargs))
    if isinstance(result, OptionsResult):
        return result
    return OptionsResult(options=result)


def _manifest():
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def _park_item_properties():
    """The leaf properties inside each row of the ``parks`` array."""
    return _manifest()["settings_schema"]["properties"]["parks"]["items"]["properties"]


class TestManifestPickerGrammar:
    """The manifest declares its pickers instead of naming a core widget."""

    def test_park_id_declares_remote_options_widget(self):
        """park_id is a single-select backed by the 'parks' catalog."""
        park_id = _park_item_properties()["park_id"]
        assert park_id["ui:widget"] == "remote-options"
        assert park_id["ui:options"]["options_id"] == "parks"

    def test_ride_ids_is_a_dependent_reorderable_multi_select(self):
        """ride_ids is scoped by the chosen park and keeps the user's order."""
        ride_ids = _park_item_properties()["ride_ids"]
        assert ride_ids["ui:widget"] == "remote-options"
        opts = ride_ids["ui:options"]
        assert opts["options_id"] == "rides"
        assert opts["depends_on"] == ["park_id"]
        assert opts["multiple"] is True
        assert opts["searchable"] is True
        assert opts["reorderable"] is True

    def test_ride_ids_collects_custom_names_via_labels_field(self):
        """Per-ride short names land in the existing custom_names sibling."""
        item_props = _park_item_properties()
        assert item_props["ride_ids"]["ui:options"]["labels_field"] == "custom_names"
        # labels_field must name a property of the same object, not the root.
        assert "custom_names" in item_props

    def test_bespoke_disney_widget_is_no_longer_referenced(self):
        """Nothing in the manifest asks core for the hardcoded picker."""
        raw = MANIFEST_PATH.read_text()
        assert "disney-parks-times-picker" not in raw
        assert "customRideNames" not in raw
        assert "reorderRides" not in raw

    def test_settings_schema_passes_core_ui_validator(self):
        """Core's ui:options grammar check accepts the schema, labels_field included."""
        from src.plugins.manifest import validate_settings_schema_ui

        errors = validate_settings_schema_ui(_manifest()["settings_schema"])
        assert errors == []

    def test_whole_manifest_passes_core_validator(self):
        """load_manifest() would accept this file rather than returning None."""
        from src.plugins.manifest import validate_manifest

        is_valid, errors = validate_manifest(_manifest())
        assert is_valid, errors

    def test_requires_the_core_release_that_added_labels_field(self):
        """labels_field landed in core 8.25.0; older cores reject this manifest.

        Core's validator treats an unknown ui:options key as a hard error, and
        load_manifest() returns None on any validation error — so installing
        this manifest on 8.24.x would not degrade the picker, it would stop the
        plugin loading at all.
        """
        assert _manifest()["fiestaboard_version"] == ">=8.25.0"


class TestParksProvider:
    """options_id='parks' browses the Disney park catalog."""

    def test_returns_every_disney_park_sorted_by_name(
        self, sample_manifest, patched_queue_times
    ):
        """The whole Walt Disney Attractions group, alphabetically."""
        result = _options(_plugin(sample_manifest), "parks")

        assert [o.label for o in result.options] == [
            "Disney California Adventure",
            "Disneyland",
            "Magic Kingdom",
        ]
        # Values are the park ids the config already stores.
        assert [o.value for o in result.options] == [17, 16, 6]

    def test_excludes_non_disney_park_groups(self, sample_manifest, patched_queue_times):
        """Queue-Times lists every operator; only Disney's group is offered."""
        result = _options(_plugin(sample_manifest), "parks")

        labels = [o.label for o in result.options]
        assert "Alton Towers" not in labels

    def test_repeat_calls_reuse_the_module_level_cache(
        self, sample_manifest, patched_queue_times
    ):
        """A keystroke in the picker must not re-hit Queue-Times every time.

        The cache has to live at module level: core dispatches get_options into
        a throwaway instance, so per-instance state would never survive.
        """
        _options(_plugin(sample_manifest), "parks")
        calls_after_first = patched_queue_times.call_count

        # A brand-new instance, exactly as core's throwaway dispatch would make.
        _options(_plugin(sample_manifest), "parks")

        assert patched_queue_times.call_count == calls_after_first

    def test_cleanup_after_dispatch_does_not_clear_the_catalog_cache(
        self, sample_manifest, patched_queue_times
    ):
        """Core calls cleanup() on the sandbox after every options request.

        If cleanup() ever reached the module-level catalog cache, the cache
        would be destroyed on the way out of every keystroke and the picker
        would hammer Queue-Times.
        """
        plugin = _plugin(sample_manifest)
        _options(plugin, "parks")
        calls_after_first = patched_queue_times.call_count

        plugin.cleanup()
        _options(_plugin(sample_manifest), "parks")

        assert patched_queue_times.call_count == calls_after_first

    def test_the_park_catalog_call_also_warms_fetch_data(
        self, sample_manifest, patched_queue_times
    ):
        """Browsing parks primes the name cache fetch_data would otherwise fill."""
        _options(_plugin(sample_manifest), "parks")
        calls_after_browse = patched_queue_times.call_count

        import plugins.disney_parks_times as mod

        assert mod._get_park_name(16) == "Disneyland"
        assert patched_queue_times.call_count == calls_after_browse

    def test_park_description_is_the_country(self, sample_manifest, patched_queue_times):
        """description gives the user somewhere to disambiguate same-named parks."""
        result = _options(_plugin(sample_manifest), "parks")

        by_label = {o.label: o for o in result.options}
        assert by_label["Disneyland"].description == "United States"


class TestRidesProvider:
    """options_id='rides' is scoped to the park chosen in the parent field."""

    def test_two_parks_yield_different_rides(self, sample_manifest, patched_queue_times):
        """The cascade actually cascades; it is not one global ride list."""
        plugin = _plugin(sample_manifest)

        disneyland = _options(plugin, "rides", parent={"park_id": 16})
        dca = _options(plugin, "rides", parent={"park_id": 17})

        dl_labels = {o.label for o in disneyland.options}
        dca_labels = {o.label for o in dca.options}

        assert dl_labels == {"Space Mountain", "Matterhorn Bobsleds", "Haunted Mansion"}
        assert dca_labels == {"Radiator Springs Racers"}
        assert dl_labels.isdisjoint(dca_labels)

    def test_missing_parent_returns_nothing(self, sample_manifest, patched_queue_times):
        """No park chosen yet means an empty list, not every ride in the world."""
        plugin = _plugin(sample_manifest)

        assert _options(plugin, "rides").options == []
        assert _options(plugin, "rides", parent={}).options == []
        assert _options(plugin, "rides", parent={"park_id": None}).options == []

    def test_unparseable_parent_park_returns_nothing(self, sample_manifest, patched_queue_times):
        """A junk park_id yields an empty list rather than an exception."""
        plugin = _plugin(sample_manifest)

        assert _options(plugin, "rides", parent={"park_id": "not_an_int"}).options == []

    def test_ride_group_is_the_land_name(self, sample_manifest, patched_queue_times):
        """group lets the picker cluster rides by land."""
        result = _options(_plugin(sample_manifest), "rides", parent={"park_id": 16})

        by_label = {o.label: o for o in result.options}
        assert by_label["Space Mountain"].group == "Tomorrowland"
        assert by_label["Haunted Mansion"].group == "New Orleans Square"

    def test_ride_preview_shows_the_live_wait(self, sample_manifest, patched_queue_times):
        """preview surfaces the current wait, or 'closed' when shut."""
        result = _options(_plugin(sample_manifest), "rides", parent={"park_id": 16})

        by_label = {o.label: o for o in result.options}
        assert by_label["Space Mountain"].preview == "45m"
        assert by_label["Matterhorn Bobsleds"].preview == "closed"


class TestQueryAndPaging:
    """query filters and limit pages, for both catalogs."""

    def test_query_filters_rides_case_insensitively(self, sample_manifest, patched_queue_times):
        """Typing in the search box narrows the list by label."""
        result = _options(
            _plugin(sample_manifest), "rides", parent={"park_id": 16}, query="mountain"
        )

        assert [o.label for o in result.options] == ["Space Mountain"]
        assert result.total == 1

    def test_query_filters_parks(self, sample_manifest, patched_queue_times):
        """The same filter applies to the park catalog."""
        result = _options(_plugin(sample_manifest), "parks", query="magic")

        assert [o.label for o in result.options] == ["Magic Kingdom"]

    def test_query_that_matches_nothing_is_empty_not_everything(
        self, sample_manifest, patched_queue_times
    ):
        """A non-matching query must not fall back to the unfiltered list."""
        result = _options(_plugin(sample_manifest), "parks", query="zzzz")

        assert result.options == []
        assert result.total == 0

    def test_limit_caps_the_page_and_reports_has_more(self, sample_manifest, patched_queue_times):
        """limit truncates, has_more flags the remainder, total counts all matches."""
        result = _options(_plugin(sample_manifest), "rides", parent={"park_id": 16}, limit=2)

        assert len(result.options) == 2
        assert result.has_more is True
        assert result.total == 3

    def test_absent_limit_returns_the_whole_catalog(
        self, sample_manifest, patched_queue_times
    ):
        """A caller that omits a limit gets everything, not zero rows."""
        result = _options(_plugin(sample_manifest), "rides", parent={"park_id": 16}, limit=None)

        assert len(result.options) == 3
        assert result.has_more is False

    def test_limit_larger_than_the_catalog_reports_no_more(
        self, sample_manifest, patched_queue_times
    ):
        """has_more stays false when the whole catalog fits in the page."""
        result = _options(_plugin(sample_manifest), "rides", parent={"park_id": 16}, limit=50)

        assert len(result.options) == 3
        assert result.has_more is False
        assert result.total == 3


class TestOptionsFailureModes:
    """What the user sees when the catalog cannot be answered."""

    def test_unreachable_upstream_raises_options_unavailable_for_parks(self, sample_manifest):
        """Core renders OptionsUnavailable as an inline hint, not a 502."""
        with patch(
            "plugins.disney_parks_times.requests.get", side_effect=OSError("connection refused")
        ):
            with pytest.raises(OptionsUnavailable):
                _options(_plugin(sample_manifest), "parks")

    def test_unreachable_upstream_raises_options_unavailable_for_rides(self, sample_manifest):
        """The ride catalog fails the same way as the park catalog."""
        with patch(
            "plugins.disney_parks_times.requests.get", side_effect=OSError("connection refused")
        ):
            with pytest.raises(OptionsUnavailable):
                _options(_plugin(sample_manifest), "rides", parent={"park_id": 16})

    def test_http_error_status_raises_options_unavailable(self, sample_manifest):
        """A 5xx from Queue-Times is 'cannot answer now', not a plugin bug."""
        bad = Mock()
        bad.raise_for_status = Mock(side_effect=Exception("503 Server Error"))
        with patch("plugins.disney_parks_times.requests.get", return_value=bad):
            with pytest.raises(OptionsUnavailable):
                _options(_plugin(sample_manifest), "parks")

    def test_unknown_options_id_raises_not_implemented(self, sample_manifest):
        """An options_id this plugin does not serve is a 501, not an empty list."""
        with pytest.raises(NotImplementedError):
            _options(_plugin(sample_manifest), "restaurants")

    def test_options_work_while_the_plugin_is_unconfigured(
        self, sample_manifest, patched_queue_times
    ):
        """The picker runs before any park is saved — that is the normal case."""
        plugin = _plugin(sample_manifest, config={})

        result = _options(plugin, "parks")

        assert len(result.options) == 3


def _simulate_labels_field_save(ride_ids, typed_labels):
    """Reproduce what core's remote-options widget persists for labels_field.

    The widget keys the map with ``String(value)`` (``optionKey`` in
    remote-options-field.tsx), then the whole config is stored as JSON. Both
    steps are modelled here so the test pins the real save path rather than a
    hand-written guess at it.
    """
    park_entry = {
        "park_id": 16,
        "ride_ids": list(ride_ids),
        # String(value), exactly as optionKey() does it.
        "custom_names": {str(rid): label for rid, label in typed_labels.items()},
    }
    config = {"parks": [park_entry], "refresh_seconds": 300}
    # Round-trip through JSON, as persisting the config does.
    return json.loads(json.dumps(config))


class TestCustomNamesKeyTypeSeam:
    """custom_names is keyed by the stringified ride id, end to end.

    This is the seam most likely to break silently: the plugin looks up
    ``custom_names.get(str(ride_id))`` while every writer upstream produces
    string keys only because JSON object keys are always strings. A writer that
    ever used real integers would not raise — the custom name would just
    quietly stop appearing on the board.
    """

    def test_name_saved_through_labels_field_is_read_by_fetch_data(
        self, sample_manifest, patched_queue_times
    ):
        """A label typed into the new widget reaches the board."""
        config = _simulate_labels_field_save([284, 279], {284: "Rocket"})
        plugin = _plugin(sample_manifest, config)

        result = plugin.fetch_data()

        rides = {r["ride_id"]: r for r in result.data["parks"][0]["rides"]}
        assert rides[284]["custom_name"] == "Rocket"
        assert rides[284]["ride_label"] == "Rocket"
        # The ride without a typed label keeps its real name.
        assert rides[279]["custom_name"] == ""
        assert rides[279]["ride_label"] == "Matterhorn Bobsleds"

    def test_the_persisted_keys_really_are_strings(self):
        """Guards the assumption the lookup depends on."""
        config = _simulate_labels_field_save([284], {284: "Rocket"})

        keys = list(config["parks"][0]["custom_names"])
        assert keys == ["284"]
        assert all(isinstance(k, str) for k in keys)

    def test_integer_keyed_custom_names_are_silently_ignored(
        self, sample_manifest, patched_queue_times
    ):
        """Characterises the failure mode, so nobody 'helpfully' writes int keys.

        Not a wish — a warning. If this test ever starts failing because the
        lookup was made tolerant, that is an improvement; update it deliberately
        rather than letting the two writers disagree.
        """
        plugin = _plugin(
            sample_manifest,
            {"parks": [{"park_id": 16, "ride_ids": [284], "custom_names": {284: "Rocket"}}]},
        )

        result = plugin.fetch_data()

        ride = result.data["parks"][0]["rides"][0]
        assert ride["custom_name"] == ""
        assert ride["ride_label"] == "Space Mountain"

    def test_removing_a_ride_drops_only_its_own_label(
        self, sample_manifest, patched_queue_times
    ):
        """Deleting a chosen ride deletes that ride's custom name and no other."""
        config = _simulate_labels_field_save([284, 279], {284: "Rocket", 279: "Matter"})
        # What the widget writes after the user removes ride 284 (see remove()
        # in remote-options-field.tsx: the remaining keys are untouched).
        config["parks"][0]["ride_ids"] = [279]
        del config["parks"][0]["custom_names"]["284"]
        plugin = _plugin(sample_manifest, config)

        result = plugin.fetch_data()

        rides = result.data["parks"][0]["rides"]
        assert [r["ride_id"] for r in rides] == [279]
        assert rides[0]["custom_name"] == "Matter"


# A config exactly as the bespoke disney-parks-times-picker saved it: two park
# rows, hand-ordered rides, custom names on some rides only, and — from the
# oldest installs, before custom names existed — a row with no custom_names key
# at all. Nothing about the persisted shape changes in this migration, so this
# must keep working byte for byte.
PRE_MIGRATION_CONFIG = {
    "enabled": True,
    "refresh_seconds": 300,
    "parks": [
        {
            "park_id": 16,
            "ride_ids": [291, 284, 279],
            "custom_names": {"291": "Mansion", "284": "Rocket"},
        },
        {
            "park_id": 17,
            "ride_ids": [329],
        },
    ],
}


class TestPreMigrationConfigStillWorks:
    """A config saved by the old widget keeps working with no migration."""

    def test_validate_config_accepts_it(self, sample_manifest):
        """No new required field crept into validation."""
        plugin = _plugin(sample_manifest)

        assert plugin.validate_config(PRE_MIGRATION_CONFIG) == []

    def test_fetch_data_honours_order_and_custom_names(
        self, sample_manifest, patched_queue_times
    ):
        """Hand-ordered rides and old custom names render exactly as before."""
        plugin = _plugin(sample_manifest, PRE_MIGRATION_CONFIG)

        result = plugin.fetch_data()

        assert result.available is True
        parks = result.data["parks"]
        assert [p["park_id"] for p in parks] == [16, 17]
        assert parks[0]["park_name"] == "Disneyland"

        # Order follows the stored ride_ids, not the upstream land order.
        first = parks[0]["rides"]
        assert [r["ride_id"] for r in first] == [291, 284, 279]
        assert [r["ride_label"] for r in first] == ["Mansion", "Rocket", "Matterhorn Bobsleds"]

    def test_a_row_with_no_custom_names_key_is_fine(
        self, sample_manifest, patched_queue_times
    ):
        """The oldest installs have no custom_names at all; treat it as empty."""
        plugin = _plugin(sample_manifest, PRE_MIGRATION_CONFIG)

        result = plugin.fetch_data()

        dca = result.data["parks"][1]
        assert "custom_names" not in PRE_MIGRATION_CONFIG["parks"][1]
        assert dca["rides"][0]["ride_label"] == "Radiator Springs Racers"
        assert dca["rides"][0]["custom_name"] == ""

    def test_the_manifest_demo_config_still_matches_the_schema(self):
        """The shipped demo config is still a valid instance of the schema."""
        manifest = _manifest()
        demo = manifest["demo"]["flagship"]["config"]
        item_props = _park_item_properties()

        for row in demo["parks"]:
            assert set(row) <= set(item_props), f"demo row has unknown keys: {row}"
            assert isinstance(row["park_id"], int)
            assert all(isinstance(r, int) for r in row["ride_ids"])

    def test_persisted_shape_is_unchanged(self):
        """park_id / ride_ids / custom_names, same types, same nesting."""
        item_props = _park_item_properties()

        assert set(item_props) == {"park_id", "ride_ids", "custom_names"}
        assert item_props["park_id"]["type"] == "integer"
        assert item_props["ride_ids"]["type"] == "array"
        assert item_props["ride_ids"]["items"]["type"] == "integer"
        assert item_props["custom_names"]["type"] == "object"
        assert item_props["custom_names"]["additionalProperties"]["type"] == "string"
        assert _manifest()["settings_schema"]["properties"]["parks"]["items"]["required"] == [
            "park_id",
            "ride_ids",
        ]
