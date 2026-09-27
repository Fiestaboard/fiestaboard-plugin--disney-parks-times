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
# Two general-purpose abbreviation lengths offered as template variables
# (ride_abbr / tiny_abbr). These are independent of any particular board --
# a template author picks whichever fits where they place it -- unlike the
# whole-board layout in _build_formatted_lines/_ride_line, which derives
# every width from the board actually being rendered.
RIDE_ABBR_LEN = 14  # Medium abbreviation for board display
TINY_ABBR_LEN = 5  # Very short abbreviation for compact display

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


_HEADER_VARIANTS = ("DISNEY QUEUE TIMES", "QUEUE TIMES", "WAIT TIMES")
_FOOTER_TEXT = "Queue-Times.com"


def _header_line(cols: int) -> str:
    """Widest header text that fits ``cols`` tiles, centered.

    Derived from the board rather than assuming a 22-wide layout: a Note
    (15 wide) cannot show "DISNEY QUEUE TIMES" (18) so it falls back to a
    shorter variant instead of truncating mid-word.
    """
    for variant in _HEADER_VARIANTS:
        if len(variant) <= cols:
            return variant.center(cols)
    return _HEADER_VARIANTS[-1][:cols]


def _footer_line(cols: int) -> Optional[str]:
    """Attribution line, or ``None`` when it cannot fit ``cols`` tiles at all."""
    if len(_FOOTER_TEXT) > cols:
        return None
    return _FOOTER_TEXT.ljust(cols)


def _ride_line(ride: Dict[str, Any], cols: int) -> str:
    """One ride's status line, reflowed to ``cols`` tiles.

    Prefers the full ride name/label and falls back to the medium (14-char)
    then tiny (5-char) abbreviation only as available width shrinks -- so a
    wider board shows a longer label, never the reverse. The result is
    always truncated to ``cols`` as a final guarantee.
    """
    is_open = bool(ride.get("is_open"))
    wait_time = ride.get("wait_time", 0) or 0
    wait_str = f"{wait_time}m" if is_open else "Closed"
    budget = max(0, cols - len(": ") - len(wait_str))

    label = (ride.get("ride_label") or ride.get("ride_name") or "").strip()
    chosen: Optional[str] = None
    for candidate in (label, ride.get("ride_abbr") or "", ride.get("tiny_abbr") or ""):
        if candidate and len(candidate) <= budget:
            chosen = candidate
            break
    if chosen is None:
        chosen = (ride.get("tiny_abbr") or "")[:budget]

    return f"{chosen}: {wait_str}"[:cols]


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
        # Keyed by board geometry (see _geometry_key), not just config equality.
        # PluginBase.get_data() already caches per geometry, but this plugin's
        # own inner TTL cache is also consulted directly by get_formatted_display()
        # (which bypasses that outer cache) -- without a geometry component here
        # too, a payload cached while rendering one board size could be handed
        # back verbatim for a different one within the refresh window
        # (FAILURE CLASS F6).
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_config: Optional[Dict[str, Any]] = None

    def _board_dims(self) -> "tuple[int, int]":
        """(rows, cols) for the board currently rendering.

        ``self.board`` is ``None`` outside a board-scoped render (unit tests,
        legacy callers) -- treated as a 22x6 Flagship so nothing here ever
        crashes for lack of a board.
        """
        board = self.board
        if board is None:
            return 6, 22
        return board.rows, board.cols

    def _geometry_key(self) -> str:
        """Cache key for the board currently rendering.

        Mirrors ``PluginBase._cache_key``: Flagship/Note are fixed-size so
        their device_type is a sufficient key, but note arrays all share
        device_type "note_array" while varying in size, so dimensions are
        folded in to avoid a 30x12 panel and a 120x3 array colliding.
        """
        board = self.board
        if board is None:
            return "_default"
        if board.device_type == "note_array":
            return f"note_array:{board.cols}x{board.rows}"
        return board.device_type

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

        rows, cols = self._board_dims()
        geo_key = self._geometry_key()

        # Optional: use cached result if within TTL, for THIS board geometry.
        refresh = self.config.get("refresh_seconds", 300)
        now = time.time()
        cached = self._cache.get(geo_key)
        cached_time = self._cache_time.get(geo_key, 0.0)
        if cached and (now - cached_time) < refresh and self._cache_config == self.config:
            lines = self._build_formatted_lines(cached, rows, cols)
            return PluginResult(
                available=True,
                data=cached,
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
                    "rides": [{"ride_id": 0, "ride_name": "Unavailable", "ride_label": "Unavailable", "ride_abbr": "Unavail", "tiny_abbr": "Unavl", "custom_name": "", "wait_time": 0, "is_open": False, "status": "Error", "state_color": "{63}", "formatted": "{63}Unavl --"}],
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
                    # No space between color and abbr so the board doesn't show a blank tile.
                    # Not padded to a fixed tile count: this per-ride field is a template
                    # variable a user may place on any board, so it must not assume a
                    # target board width (that assumption -- padding to fit two per
                    # 22-wide line -- was the bug; see _ride_line for the whole-board path).
                    formatted = f"{state_color}{tiny_abbr:<5} {wait_str}"
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
            "formatted": "Queue Times",
        }
        self._cache[geo_key] = result_data
        self._cache_time[geo_key] = now
        self._cache_config = copy.deepcopy(self.config)
        lines = self._build_formatted_lines(result_data, rows, cols)
        return PluginResult(
            available=True,
            data=result_data,
            formatted_lines=lines,
        )

    def _build_formatted_lines(self, data: Dict[str, Any], rows: int, cols: int) -> List[str]:
        """Board display lines sized to (rows, cols).

        Reflows rather than truncating: a taller board shows more rides
        (never a fixed count), a wider one shows longer per-ride labels (see
        _ride_line). Rows may come back shorter than the board -- never
        longer, never wider -- so this never pads with blank filler just to
        hit a target height.
        """
        rides: List[Dict[str, Any]] = []
        for park in data.get("parks", []):
            rides.extend(park.get("rides", []))

        header_rows = 1
        footer_text = _footer_line(cols)
        footer_rows = 0
        if footer_text is not None and (rows - header_rows - 1) >= 2:
            # Only spend a row on attribution when it leaves room for at
            # least two ride lines -- on a very short board (e.g. the
            # wide-short 120x3 array), content wins over attribution.
            footer_rows = 1

        available_for_rides = max(0, rows - header_rows - footer_rows)

        lines: List[str] = [_header_line(cols)]
        for ride in rides[:available_for_rides]:
            lines.append(_ride_line(ride, cols))
        if footer_rows:
            lines.append(footer_text)
        return lines[:rows]

    def get_formatted_display(self) -> Optional[List[str]]:
        geo_key = self._geometry_key()
        cached = self._cache.get(geo_key)
        if not cached:
            result = self.fetch_data()
            if not result.available:
                return None
            cached = self._cache.get(geo_key) or {}
        rows, cols = self._board_dims()
        return self._build_formatted_lines(cached, rows, cols)

    def cleanup(self) -> None:
        self._cache = {}
        self._cache_time = {}
        self._cache_config = None
        logger.debug("%s cleanup", self.plugin_id)


Plugin = DisneyParksTimesPlugin
