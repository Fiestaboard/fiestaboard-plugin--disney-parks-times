"""Disney Park Queue Times plugin for FiestaBoard.

Displays wait times for Disney parks and rides from Queue-Times.com.
Data is updated every 5 minutes by the API. Attribution required.
"""

import copy
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from src.plugins.base import (
    Option,
    OptionsRequest,
    OptionsResult,
    OptionsUnavailable,
    PluginBase,
    PluginResult,
)

logger = logging.getLogger(__name__)

QUEUE_TIMES_BASE = "https://queue-times.com"
DISNEY_GROUP_ID = 2  # Walt Disney Attractions
CACHE_TTL = 300  # 5 minutes
MAX_LINE_LEN = 22
RIDE_ABBR_LEN = 14  # Abbreviated ride name for board display (fits "  Name: 99m" in 22 chars)
TINY_ABBR_LEN = 5  # Very short abbreviation for compact display
# Fixed width in tiles so multiple formatted on one line align (1 color + 5 abbr + 1 space + wait = 8-9; pad to 11 so two fit in 22)
FORMATTED_TILES = 11

# Board color codes for state_color / formatted
COLOR_OPEN = "{66}"   # green - operating normally
COLOR_CLOSED = "{63}"  # red - closed / not operating

# Known tiny abbreviations (max 5 chars) from common Disney fan usage (wdwmagic, touringplans, etc.).
# Keys are lowercase substrings to match in ride name; longest match wins. Sorted alphabetically by key.
_KNOWN_TINY_ABBR: List[tuple] = [
    ("big thunder mountain railroad", "THUND"),
    ("buzz lightyear", "BUZZ"),
    ("carousel of progress", "COP"),
    ("country bear jamboree", "CBJ"),
    ("expedition everest", "EE"),
    ("flight of passage", "FOP"),
    ("frozen ever after", "FRZN"),
    ("guardians of the galaxy", "GOTG"),
    ("haunted mansion", "HM"),
    ("indiana jones", "INDY"),
    ("it's a small world", "SMALL"),
    ("jungle cruise", "JUNGL"),
    ("kilimanjaro safaris", "KS"),
    ("living with the land", "LWTL"),
    ("mickey and minnie's runaway railway", "MMRR"),
    ("millennium falcon", "MFSR"),
    ("mission space", "MS"),
    ("mission: space", "MS"),
    ("na'vi river journey", "NRJ"),
    ("navi river journey", "NRJ"),
    ("peter pan's flight", "PPF"),
    ("pirates of the caribbean", "POTC"),
    ("radiator springs", "RADIA"),
    ("rise of the resistance", "RISE"),
    ("rock n roller coaster", "RNR"),
    ("rock 'n' roller coaster", "RNR"),
    ("runaway railway", "MMRR"),
    ("seven dwarfs mine train", "7DMT"),
    ("small world", "SMALL"),
    ("soarin", "SOARN"),
    ("soarin'", "SOARN"),
    ("space mountain", "SMNT"),
    ("spaceship earth", "SE"),
    ("splash mountain", "SPLMT"),
    ("star tours", "ST"),
    ("star wars: rise of the resistance", "RISE"),
    ("test track", "TT"),
    ("tower of terror", "TOT"),
    ("toy story mania", "TSMM"),
    ("toy story midway mania", "TSMM"),
    ("twilight zone tower of terror", "TOT"),
    ("web slingers", "WEBSL"),
]


def _abbreviate_ride_name(name: str, max_len: int = RIDE_ABBR_LEN) -> str:
    """Shorten ride name for display; prefer truncation at word boundary."""
    if not name or len(name) <= max_len:
        return (name or "").strip()
    truncated = name[: max_len + 1].rsplit(" ", 1)
    if len(truncated) == 2 and truncated[0]:
        return truncated[0].strip()
    return name[:max_len].strip()


def _tiny_abbr(name: str, max_len: int = TINY_ABBR_LEN) -> str:
    """Very short ride name (max 5 chars); use known abbreviations when possible.
    Single-rider lines get a trailing '1' so they differ from the main line (e.g. SMNT1 vs SMNT).
    No spaces in the result; always uppercase for board display.
    """
    n = (name or "").strip().lower()
    if not n:
        return ""
    is_single_rider = "single rider" in n
    match = ""
    abbr = ""
    for key_phrase, known in _KNOWN_TINY_ABBR:
        if key_phrase in n and len(key_phrase) > len(match):
            match, abbr = key_phrase, known
    if abbr:
        base = abbr[:max_len].upper()
    else:
        # Fallback: first max_len chars of name, spaces removed, then uppercase for board
        base = "".join((name or "").strip().split())[:max_len].upper()
    if is_single_rider:
        if len(base) < max_len:
            base = (base + "1")[:max_len]
        else:
            base = base[: max_len - 1] + "1"
    return base


# Module-level cache for park names (id -> name)
_park_names_cache: Dict[int, str] = {}
_park_names_cache_time: float = 0

# Module-level cache of the full Disney park records from parks.json. The name
# cache above answers "what is park 16 called"; this one keeps the extra fields
# (country) the settings picker shows. Both are module-level on purpose: core
# runs get_options on a throwaway instance, so per-instance state would never
# survive to the next keystroke.
_park_catalog_cache: List[Dict[str, Any]] = []
_park_catalog_cache_time: float = 0


def _fetch_disney_parks() -> List[Dict[str, Any]]:
    """Return the Walt Disney Attractions park records from parks.json.

    Cached module-side for CACHE_TTL. Raises on upstream failure so callers can
    decide between a fallback (fetch_data) and an inline hint (get_options).
    """
    global _park_catalog_cache, _park_catalog_cache_time, _park_names_cache, _park_names_cache_time
    now = time.time()
    # Guard on the timestamp rather than on the list being non-empty, so an
    # upstream response that legitimately contains no Disney parks is still
    # cached for CACHE_TTL instead of being re-fetched on every single call.
    if _park_catalog_cache_time and (now - _park_catalog_cache_time) < CACHE_TTL:
        return _park_catalog_cache

    resp = requests.get(f"{QUEUE_TIMES_BASE}/parks.json", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    parks: List[Dict[str, Any]] = []
    for group in data or []:
        if group.get("id") == DISNEY_GROUP_ID:
            parks = list(group.get("parks", []))
            break

    _park_catalog_cache = parks
    _park_catalog_cache_time = now
    # Keep the name cache in step so fetch_data() benefits from the same call.
    _park_names_cache = {p["id"]: p.get("name", str(p["id"])) for p in parks if "id" in p}
    _park_names_cache_time = now
    return parks


def _get_park_name(park_id: int) -> str:
    """Resolve park_id to display name via parks.json (cached)."""
    if park_id in _park_names_cache:
        return _park_names_cache[park_id]
    try:
        _fetch_disney_parks()
    except Exception as e:
        logger.warning("Failed to fetch park names: %s", e)
    return _park_names_cache.get(park_id, f"Park {park_id}")


def _paginate(options: List[Option], request: OptionsRequest) -> OptionsResult:
    """Apply the request's query filter and limit to an in-memory option list.

    Both catalogs are small enough to fetch whole, so filtering and paging
    happen here rather than upstream. ``total`` counts everything matching the
    query, not just the page returned.
    """
    query = (request.query or "").strip().lower()
    if query:
        options = [o for o in options if query in (o.label or "").lower()]

    total = len(options)
    limit = request.limit
    if limit is not None and limit >= 0:
        page = options[:limit]
    else:
        page = options
    return OptionsResult(
        options=page,
        has_more=len(page) < total,
        total=total,
    )


class DisneyParksTimesPlugin(PluginBase):
    """Disney park queue times from Queue-Times.com."""

    def __init__(self, manifest: Dict[str, Any]):
        super().__init__(manifest)
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time: float = 0
        self._cache_config: Optional[Dict[str, Any]] = None

    @property
    def plugin_id(self) -> str:
        return "disney_parks_times"

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors = []
        parks_config = config.get("parks", [])
        if not parks_config:
            errors.append("At least one park with rides is required")
        for i, entry in enumerate(parks_config):
            if not isinstance(entry, dict):
                errors.append(f"Park entry {i + 1} is invalid")
                continue
            ride_ids = entry.get("ride_ids") or []
            if not ride_ids:
                errors.append(f"Park entry {i + 1}: select at least one ride")
        return errors

    def get_options(self, request: OptionsRequest) -> OptionsResult:
        """Browse the Queue-Times catalog for the settings pickers.

        Serves both ``options_id``s the manifest declares: ``parks`` (every
        Disney park) and ``rides`` (the rides of the park chosen in the parent
        field). Reuses the same HTTP path and module-level cache as
        ``fetch_data`` so opening the dialog does not double the upstream load.
        """
        if request.options_id == "parks":
            options = self._park_options()
        elif request.options_id == "rides":
            options = self._ride_options(request)
        else:
            raise NotImplementedError(request.options_id)

        return _paginate(options, request)

    def _park_options(self) -> List[Option]:
        """Every Disney park, alphabetically."""
        try:
            parks = _fetch_disney_parks()
        except Exception as e:
            logger.warning("Queue-Times park catalog unavailable: %s", e)
            raise OptionsUnavailable("Could not reach Queue-Times.com to list parks") from e

        options = [
            Option(
                value=p["id"],
                label=(p.get("name") or str(p["id"])).strip(),
                description=(p.get("country") or "").strip() or None,
            )
            for p in parks
            if p.get("id") is not None
        ]
        options.sort(key=lambda o: o.label.lower())
        return options

    def _ride_options(self, request: OptionsRequest) -> List[Option]:
        """The rides of the park named in request.parent['park_id'].

        With no parent park chosen there is no sensible catalog to show, so the
        list is empty rather than every ride Disney operates worldwide.
        """
        raw_park_id = (request.parent or {}).get("park_id")
        if raw_park_id is None or raw_park_id == "":
            return []
        try:
            park_id = int(raw_park_id)
        except (TypeError, ValueError):
            return []

        try:
            resp = requests.get(
                f"{QUEUE_TIMES_BASE}/parks/{park_id}/queue_times.json",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Queue-Times ride catalog unavailable for park %s: %s", park_id, e)
            raise OptionsUnavailable("Could not reach Queue-Times.com to list rides") from e

        options: List[Option] = []
        for land in (data or {}).get("lands", []) or []:
            land_name = (land.get("name") or "").strip() or None
            for ride in land.get("rides", []) or []:
                rid = ride.get("id")
                if rid is None:
                    continue
                is_open = bool(ride.get("is_open"))
                wait = ride.get("wait_time", 0) or 0
                options.append(
                    Option(
                        value=rid,
                        label=(ride.get("name") or str(rid)).strip(),
                        group=land_name,
                        preview=f"{wait}m" if is_open else "closed",
                    )
                )
        return options

    def fetch_data(self) -> PluginResult:
        parks_config = self.config.get("parks", [])
        if not parks_config:
            return PluginResult(
                available=False,
                error="No parks configured. Add at least one park and select rides."
            )

        # Optional: use cached result if within TTL
        refresh = self.config.get("refresh_seconds", 300)
        now = time.time()
        if self._cache and (now - self._cache_time) < refresh and self._cache_config == self.config:
            lines = self._build_formatted_lines(self._cache)
            return PluginResult(
                available=True,
                data=self._cache,
                formatted_lines=lines,
            )

        parks_data: List[Dict[str, Any]] = []
        for entry in parks_config:
            park_id = entry.get("park_id")
            ride_ids = entry.get("ride_ids") or []
            # Per-ride custom display labels, keyed by ride id as a string ("2": "The Haunted House").
            # Old configs won't have this; treat missing/None as an empty map.
            custom_names = entry.get("custom_names") or {}
            if park_id is None or not ride_ids:
                continue
            try:
                park_id = int(park_id)
            except (TypeError, ValueError):
                continue
            ride_id_set = {int(r) for r in ride_ids if r is not None}
            try:
                resp = requests.get(
                    f"{QUEUE_TIMES_BASE}/parks/{park_id}/queue_times.json",
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("Queue-Times fetch failed for park %s: %s", park_id, e)
                park_name = _get_park_name(park_id)
                parks_data.append({
                    "park_id": park_id,
                    "park_name": park_name,
                    "rides": [{"ride_id": 0, "ride_name": "Unavailable", "ride_label": "Unavailable", "ride_abbr": "Unavail", "tiny_abbr": "Unavl", "custom_name": "", "wait_time": 0, "is_open": False, "status": "Error", "state_color": "{63}", "formatted": "{63}Unavl --  "}],  # Pad to FORMATTED_TILES (11)
                })
                continue

            park_name = _get_park_name(park_id)
            rides_out: List[Dict[str, Any]] = []
            for land in data.get("lands", []):
                for ride in land.get("rides", []):
                    rid = ride.get("id")
                    if rid not in ride_id_set:
                        continue
                    wait = ride.get("wait_time", 0) or 0
                    is_open = ride.get("is_open", False)
                    status = "Open" if is_open else "Closed"
                    name = (ride.get("name") or str(rid)).strip()
                    custom_name = (custom_names.get(str(rid)) or "").strip()
                    label = custom_name or name
                    ride_abbr = _abbreviate_ride_name(label)
                    tiny_abbr = _tiny_abbr(label)
                    state_color = COLOR_OPEN if is_open else COLOR_CLOSED
                    wait_str = f"{wait}m" if is_open else "--"
                    # No space between color and abbr so the board doesn't show a blank tile
                    base = f"{state_color}{tiny_abbr:<5} {wait_str}"
                    # Pad to fixed tile count so multiple formatted on same line align (color=1 + 5 + 1 + len(wait_str) tiles)
                    tile_count = 1 + 5 + 1 + len(wait_str)
                    pad = max(0, FORMATTED_TILES - tile_count)
                    formatted = base + (" " * pad)
                    rides_out.append({
                        "ride_id": rid,
                        "ride_name": name,
                        "ride_label": label,
                        "ride_abbr": ride_abbr,
                        "tiny_abbr": tiny_abbr,
                        "custom_name": custom_name,
                        "wait_time": wait,
                        "is_open": is_open,
                        "status": status,
                        "state_color": state_color,
                        "formatted": formatted,
                    })
            # Keep order of ride_ids from config
            order = {rid: i for i, rid in enumerate(ride_ids)}
            rides_out.sort(key=lambda r: order.get(r["ride_id"], 999))
            parks_data.append({
                "park_id": park_id,
                "park_name": park_name,
                "rides": rides_out,
            })

        if not parks_data:
            return PluginResult(
                available=False,
                error="No park data could be loaded. Check your park and ride selection."
            )

        result_data: Dict[str, Any] = {
            "parks": parks_data,
            "formatted": "Queue Times"[:22],
        }
        self._cache = result_data
        self._cache_time = time.time()
        self._cache_config = copy.deepcopy(self.config)
        lines = self._build_formatted_lines(result_data)
        return PluginResult(
            available=True,
            data=result_data,
            formatted_lines=lines,
        )

    def _build_formatted_lines(self, data: Dict[str, Any]) -> List[str]:
        """Build 6-line default display; include attribution."""
        lines: List[str] = []
        lines.append("DISNEY QUEUE TIMES".center(22)[:22])
        flat: List[tuple] = []  # (park_name, ride_abbr, wait_time, is_open)
        for park in data.get("parks", []):
            for ride in park.get("rides", []):
                flat.append((
                    ride.get("ride_abbr") or (ride.get("ride_name") or "")[:RIDE_ABBR_LEN],
                    ride.get("wait_time", 0),
                    ride.get("is_open", False),
                ))
        for rabbr, wait, is_open in flat[:4]:
            if is_open:
                line = f"{rabbr}: {wait}m"
            else:
                line = f"{rabbr}: Closed"
            lines.append(line[:22])
        while len(lines) < 5:
            lines.append("")
        lines.append("Queue-Times.com".ljust(22)[:22])  # Attribution
        return lines[:6]

    def get_formatted_display(self) -> Optional[List[str]]:
        if not self._cache:
            result = self.fetch_data()
            if not result.available:
                return None
        return self._build_formatted_lines(self._cache or {})

    def cleanup(self) -> None:
        self._cache = None
        self._cache_config = None
        logger.debug("%s cleanup", self.plugin_id)


Plugin = DisneyParksTimesPlugin
