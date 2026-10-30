#!/usr/bin/env python3
"""SkyGuard — clear-night forecast alert.

Runs each day at midday (the "outlook": tonight and any later night both
forecast models already cover) and again each evening
(the "tonight" update: a fresher look at just tonight). Pulls cloud forecast for
a configured location from Open-Meteo (one primary model, one cross-check),
evaluates only the hours of real darkness (nautical night), and sends a single
Pushover push if a night looks usable — with a chart of the night's cloud,
hour by hour, attached. Silent otherwise.

Each run's verdict per night is kept in a small history file, so the push can
say how the forecast for a night has moved since the previous run (🔴→🟢, low
cloud 60→15%, …) and warn when a night that looked good no longer does.

Cloud is judged by LAYER, not as one lumped total. Low cloud is the real gate
for astrophotography — it's opaque. Mid cloud also blocks. High cloud (cirrus)
is thin and partly transparent, so by default it is reported but does NOT reject
an hour (narrowband tolerates it). This avoids the over-pessimism of tools that
gate on total cloud cover, which inflates with harmless high cirrus. Heavy
forecast high cloud does, however, drop a night from 🟢 confident to 🟡
speculative — see the confidence tiers in CONFIG.

Cloud isn't the only thing between you and the stars. Fog (forecast visibility)
blocks an hour like low cloud does; mist, and dust or smoke aloft (aerosol
optical depth), dim it — the hour stays usable but drops out of the confident
tier. Dew risk, a strong jet stream (poor seeing) and when the Moon is up are
reported but don't change the verdict.

Set your location with the LATITUDE / LONGITUDE / TIMEZONE environment variables
(or edit the defaults below). Tune behaviour in the CONFIG block. Set DRY_RUN=1
to print the message to stdout instead of pushing.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from astral import LocationInfo, moon
from astral.sun import elevation

# --------------------------------------------------------------------------- #
# CONFIG — everything tunable lives here.                                      #
# --------------------------------------------------------------------------- #

def _env(name: str, default: str) -> str:
    """Env var value, falling back to default when unset OR empty (an unset
    GitHub Actions variable is passed through as an empty string)."""
    value = os.environ.get(name, "").strip()
    return value or default


def _env_bool(name: str, default: bool = False) -> bool:
    """Boolean env var — '1', 'true', 'yes', 'on' (case-insensitive) are True."""
    value = os.environ.get(name, "").strip().lower()
    return value in ("1", "true", "yes", "on") if value else default


# Observing site. Set via environment (recommended, so your location is not
# committed) or change the defaults. Defaults to the Royal Observatory,
# Greenwich.
LATITUDE = float(_env("LATITUDE", "51.4779"))
LONGITUDE = float(_env("LONGITUDE", "-0.0015"))
TIMEZONE = _env("TIMEZONE", "Europe/London")

# Darkness definition. 12 = nautical night. Use 18 for astronomical night.
# At high latitudes nautical night can be the only real darkness in midsummer.
TWILIGHT_DEPRESSION = 12.0

# How many upcoming nights to evaluate at most (tonight = night 1).
NIGHTS_AHEAD = 3

# Only score a night once BOTH models cover it — its whole dark window and the
# equipment-exposure window. The high-resolution primary model only reaches ~2½
# days ahead, so beyond that a verdict would rest on the global model alone and
# can swing a lot between runs. In practice the midday outlook then covers
# tonight and tomorrow, and the evening update tonight. Tonight is always scored,
# so an outage of one model shows up as a tentative verdict rather than silence.
REQUIRE_BOTH_MODELS = True

# Run mode. "outlook" (default, the midday run) scores the upcoming nights
# (see NIGHTS_AHEAD / REQUIRE_BOTH_MODELS). "tonight" (the evening run) re-scores just tonight with the freshest
# model runs and always shows the hour-by-hour picture.
RUN_MODE = _env("RUN_MODE", "outlook").lower()

# Local hours the two runs are meant for: the "outlook" at OUTLOOK_HOUR and the
# evening "tonight" update at EVENING_HOUR. The scheduler (cron) is in UTC, so
# the workflow fires two UTC slots per run to cover daylight saving and the one
# whose scheduled time lands on the local hour goes ahead; the other skips.
OUTLOOK_HOUR = 12
EVENING_HOUR = 18

# Forecast history. Every run records a compact verdict per night here, and the
# next run compares against it to report what changed. Pruned automatically.
# In GitHub Actions this file is carried between runs by the Actions cache.
STATE_FILE = Path(_env("STATE_FILE", ".skyguard/state.json"))
STATE_KEEP_RUNS = 6  # snapshots kept per night

# What counts as a change worth mentioning (smaller wobbles are ignored).
CHANGE_HOURS = 2       # usable hours
CHANGE_CLOUD_PTS = 15  # low / mid cloud average, percentage points
CHANGE_GUST_KMH = 10   # peak gust

# Daily digest. When True, SkyGuard sends a short status on EVERY run — the
# clear-night alert when a night qualifies, otherwise a "nothing tonight, here's
# the outlook (and why)" summary. When False (default) it stays silent unless a
# night qualifies. Enable per-deployment via the DAILY_DIGEST env/variable.
DAILY_DIGEST = _env_bool("DAILY_DIGEST", False)

# Per-layer cloud thresholds. An hour is "usable" when low and mid cloud are at
# or below their caps. Low cloud is the dominant gate. High cloud is NOT gated
# by default (set HIGH_CLOUD_MAX to a number to gate on it too) but is reported,
# and flagged as "thin high cloud" in the push when its average is notable.
LOW_CLOUD_MAX = 20
MID_CLOUD_MAX = 50
HIGH_CLOUD_MAX: int | None = None
HIGH_CLOUD_NOTE = 40  # average high cloud above this gets a note in the push

# Confidence tiers. A night that passes the rules above is 🟡 SPECULATIVE; it is
# 🟢 CONFIDENT only if both models also agree on a usable run of at least
# CONFIDENT_MIN_RUN hours with high cloud at or below HIGH_CLOUD_CONFIDENT_MAX in
# each model and no mist or aerosol haze (see below) (and, with the setup gate on, the setup window passes that test
# too). Thin cirrus is fine for narrowband, but a forecast of heavy high cloud
# can't tell thin cirrus from a thick frontal sheet — and such nights usually
# fail. Checked against a year of observed cloud near the site: confident nights
# gave a real session ~65% of the time, versus ~36% for the plain rules.
CONFIDENT_MIN_RUN = 2
HIGH_CLOUD_CONFIDENT_MAX = 60

# Precipitation veto. Rain on the kit is a hard no — there is no acceptable
# chance of it — so the night is rejected if ANY hour in the equipment-exposure
# window has forecast rain above PRECIP_AMOUNT_MAX mm OR a rain probability above
# PRECIP_PROB_MAX %, in EITHER model. The window is the hours the gear is
# physically outside — wider than the dark hours. It fails safe: an hour with no
# amount or no probability from any model (or missing from the forecast) counts
# as wet, because unknown is not dry. Not every model publishes a probability
# (KNMI HARMONIE doesn't; ECMWF does), so at least one configured model must,
# or every night is vetoed.
EXPOSURE_START_HOUR = 18   # gear goes out (or must stay dry from)
EXPOSURE_END_HOUR = 9      # gear comes in (next morning)
PRECIP_AMOUNT_MAX = 0.0    # mm; any forecast rain above this vetoes the night
PRECIP_PROB_MAX = 0        # %; any rain chance above this vetoes the night

# Wind. Gusts shake the scope and trail subs far more than steady wind, so gusts
# are what we gate on. A small refractor on a stiff pier in a sheltered garden
# tolerates a fair bit, and the forecast is open-terrain 10 m wind, so these are
# deliberately generous. Sustained/gusts above the WARN levels add a ⚠ to the
# push; gusts above the VETO level reject the night (imaging is pointless).
# Values are km/h (Open-Meteo's default wind unit), checked over the exposure
# window in either model. Set WIND_GUST_VETO_KMH very high to disable the veto.
WIND_SUSTAINED_WARN_KMH = 25
WIND_GUST_WARN_KMH = 40
WIND_GUST_VETO_KMH = 55

# Mist and fog. Water droplets near the ground don't show up as cloud: every
# layer can read 0% while mist hides all but the brightest stars. Forecast
# visibility catches it. KNMI HARMONIE publishes visibility but ECMWF doesn't
# (via Open-Meteo), so when no model gives visibility for an hour, a dew-point
# spread (temperature minus dew point) at or below MIST_SPREAD_C stands in for
# mist. Fog in either model makes an hour unusable, as low cloud would (a model
# without visibility can't vouch for the hour); mist keeps the hour usable but
# out of the confident tier (◐), like heavy high cloud. Model visibility is a
# rough guide: on 30 Oct 2026 a forecast of ~6 km came with a sky showing only
# the brightest stars, so the line is set well above that, erring towards hazy.
FOG_VISIBILITY_M = 1000
MIST_VISIBILITY_M = 20000
MIST_SPREAD_C = 1.5

# Dew. When the air cools to within DEW_SPREAD_C of its dew point, optics dew up
# (frost below 0 °C) — reported so you fit the heaters; it doesn't change the verdict.
DEW_SPREAD_C = 2.0

# Aerosols: Saharan dust, wildfire smoke and pollution haze dim the whole sky
# even when it's cloud-free. Aerosol optical depth (AOD, at 550 nm) comes from
# the CAMS forecast via Open-Meteo's air-quality API; clean air here is ~0.1.
# An hour above AOD_HAZE_MAX stays usable but drops out of the confident tier.
# This is a quality signal, not a safety one: if the fetch fails, it's skipped.
AOD_HAZE_MAX = 0.3
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# Seeing. A fast jet stream overhead (wind at 250 hPa, ~10 km up) means
# turbulent air and soft, bloated stars. Reported above this speed, km/h
# (~150 km/h ≈ 80 kn); it doesn't change the verdict. Only ECMWF publishes it.
JET_SEEING_KMH = 150

# A night qualifies when it has at least this many usable hours AND a continuous
# usable run of at least this length. At high latitudes a midsummer nautical
# night can be only ~2h, so keep these low or the tool stays silent all summer.
MIN_USABLE_HOURS = 2
MIN_CONTINUOUS_RUN = 2

# Setup / polar-align window — a HARD requirement in the tripod workflow. You
# must physically set up and polar align before bed, so a night is only worth
# deploying for if the sky is clear during this pre-bed window (clouds at setup
# time = no session, even if it clears later while you'd be asleep). When a
# permanent pier holds the mount aligned, set REQUIRE_SETUP_WINDOW = False —
# nightly setup/alignment is then unnecessary and only the dark hours matter.
REQUIRE_SETUP_WINDOW = True
SETUP_WINDOW_START_HOUR = 21   # earliest you'd start setting up
SETUP_WINDOW_END_HOUR = 23     # bedtime — setup + align must be done by here
SETUP_MIN_CLEAR_HOURS = 1      # clear+dark hours needed in the window to align

# Later bedtime on some nights. Bedtime (the setup-window end) isn't the same
# every night — on Friday and Saturday nights it can run an hour or two later.
# Map the weekday of the EVENING (Mon=0, Tue=1, … Fri=4, Sat=5, Sun=6) to a
# later end hour; nights not listed fall back to SETUP_WINDOW_END_HOUR. Hours ≥24
# mean past midnight, so 25 = 01:00 the next morning. Default: Fri/Sat nights run
# to 01:00. Empty the dict to give every night the same bedtime.
SETUP_WINDOW_END_HOUR_BY_WEEKDAY: dict[int, int] = {4: 25, 5: 25}  # Fri, Sat → 01:00

# Polar alignment also needs the sky dark enough to see/plate-solve stars, so a
# setup-window hour only counts if the sun is at least this far below the horizon
# (degrees) AND it's clear. At 52N in midsummer this is the binding constraint —
# it isn't dark enough to align before bedtime, so SkyGuard correctly stays
# silent. Lower (e.g. 6) = more optimistic (align in brighter twilight); higher
# (12) = wait for fuller darkness. Becomes moot once a permanent pier holds the
# mount aligned (REQUIRE_SETUP_WINDOW = False).
ALIGN_DEPRESSION = 10.0

# Optional curfew. By default ALL dark hours are reported. Set RESPECT_CURFEW =
# True to only count usable hours before CURFEW_HOUR.
RESPECT_CURFEW = False
CURFEW_HOUR = 23

# Show a per-hour cloud breakdown in the push when the dark window is at most
# this many hours; otherwise show a compact summary only.
MAX_HOURLY_DETAIL = 6

# Cloud chart. Each push carries a PNG of the charted night's forecast, hour by
# hour: one panel per cloud layer with both models drawn against that layer's
# limit, under a strip of the per-hour verdicts (✓ ◐ ~ ✗). It shows WHEN the
# cloud comes and goes, which the whole-night averages in the text can't. Needs
# matplotlib; if that's missing or drawing fails, the push goes out without it.
# A dry run saves the chart to CHART_FILE instead.
SEND_CHART = _env_bool("SEND_CHART", True)
CHART_FILE = Path(_env("CHART_FILE", ".skyguard/chart.png"))

# Forecast models. Primary first; high confidence requires both to agree. The
# default primary (KNMI HARMONIE) is a high-resolution model for the Netherlands;
# change it for other regions (see Open-Meteo's model list).
PRIMARY_MODEL = _env("PRIMARY_MODEL", "knmi_harmonie_arome_netherlands")
SECONDARY_MODEL = _env("SECONDARY_MODEL", "ecmwf_ifs025")

# Pull a little extra so the last night's dawn isn't truncated.
FORECAST_DAYS = NIGHTS_AHEAD + 1

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_MAX_CHARS = 1024  # Pushover's message length limit

TZ = ZoneInfo(TIMEZONE)
OBSERVER = LocationInfo("site", "", TIMEZONE, LATITUDE, LONGITUDE).observer

LAYERS = ("low", "mid", "high")


# --------------------------------------------------------------------------- #
# Data model                                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class HourCloud:
    time: datetime
    primary: dict[str, int | None]    # {"low": .., "mid": .., "high": ..}
    secondary: dict[str, int | None]
    precip_mm: dict[str, float | None]  # {"primary": .., "secondary": ..}
    precip_prob: dict[str, int | None]  # {"primary": .., "secondary": ..}
    wind_kmh: dict[str, float | None]   # {"primary": .., "secondary": ..}
    gust_kmh: dict[str, float | None]   # {"primary": .., "secondary": ..}
    # Haze, dew and seeing — each keyed "primary"/"secondary" like the above.
    visibility_m: dict[str, float | None] = field(default_factory=dict)
    temp_c: dict[str, float | None] = field(default_factory=dict)
    dew_point_c: dict[str, float | None] = field(default_factory=dict)
    jet_kmh: dict[str, float | None] = field(default_factory=dict)
    aod: float | None = None  # aerosol optical depth (CAMS), None if unavailable

    @property
    def min_visibility_m(self) -> float | None:
        vals = [v for v in self.visibility_m.values() if v is not None]
        return min(vals) if vals else None

    @property
    def spread_c(self) -> float | None:
        """Smallest temperature − dew point gap across the models."""
        gaps = [self.temp_c[k] - self.dew_point_c[k] for k in self.temp_c
                if self.temp_c[k] is not None and self.dew_point_c.get(k) is not None]
        return min(gaps) if gaps else None

    @property
    def min_temp_c(self) -> float | None:
        vals = [v for v in self.temp_c.values() if v is not None]
        return min(vals) if vals else None

    @property
    def max_jet_kmh(self) -> float | None:
        vals = [v for v in self.jet_kmh.values() if v is not None]
        return max(vals) if vals else None

    def fogged(self, src: str) -> bool:
        """True when model `src` forecasts fog for this hour."""
        vis = self.visibility_m.get(src)
        return vis is not None and vis < FOG_VISIBILITY_M

    @property
    def fog(self) -> bool:
        return any(self.fogged(k) for k in self.visibility_m)

    @property
    def misty(self) -> bool:
        """Visibility below the mist line — or, with no visibility from any
        model, the air within MIST_SPREAD_C of its dew point."""
        vis = self.min_visibility_m
        if vis is not None:
            return vis < MIST_VISIBILITY_M
        spread = self.spread_c
        return spread is not None and spread <= MIST_SPREAD_C

    @property
    def aerosol_hazy(self) -> bool:
        return self.aod is not None and self.aod > AOD_HAZE_MAX

    @property
    def hazy(self) -> bool:
        """Mist or aerosol: the hour is usable but not confident."""
        return self.misty or self.aerosol_hazy

    @property
    def max_precip_mm(self) -> float:
        vals = [v for v in self.precip_mm.values() if v is not None]
        return max(vals) if vals else 0.0

    @property
    def max_precip_prob(self) -> int | None:
        vals = [v for v in self.precip_prob.values() if v is not None]
        return max(vals) if vals else None

    @property
    def max_wind_kmh(self) -> float:
        vals = [v for v in self.wind_kmh.values() if v is not None]
        return max(vals) if vals else 0.0

    @property
    def max_gust_kmh(self) -> float:
        vals = [v for v in self.gust_kmh.values() if v is not None]
        return max(vals) if vals else 0.0

    @property
    def rain_unknown(self) -> bool:
        """True if no model gives a rain amount, or none gives a probability."""
        return (all(v is None for v in self.precip_mm.values())
                or all(v is None for v in self.precip_prob.values()))

    @property
    def wet(self) -> bool:
        """True if either model forecasts rain above the amount or probability
        cap — or if the rain forecast is incomplete (fail safe)."""
        if self.rain_unknown or self.max_precip_mm > PRECIP_AMOUNT_MAX:
            return True
        return self.max_precip_prob > PRECIP_PROB_MAX

    def _usable(self, src: str, strict: bool = False) -> bool | None:
        """True/False if model `src` ("primary"/"secondary") has data for this
        hour, else None.

        Gates on low and mid cloud and on fog in either model (only one may
        forecast visibility, so the other can't vouch for the hour); high cloud only if
        HIGH_CLOUD_MAX is set. When `strict` (the confident tier), high cloud is
        capped at HIGH_CLOUD_CONFIDENT_MAX and the hour mustn't be hazy.
        """
        layers = getattr(self, src)
        if all(layers.get(k) is None for k in LAYERS):
            return None
        if self.fog:
            return False
        if strict and self.hazy:
            return False
        low, mid, high = layers.get("low"), layers.get("mid"), layers.get("high")
        if low is not None and low > LOW_CLOUD_MAX:
            return False
        if mid is not None and mid > MID_CLOUD_MAX:
            return False
        high_max = HIGH_CLOUD_MAX
        if strict:
            high_max = min(HIGH_CLOUD_CONFIDENT_MAX, HIGH_CLOUD_MAX or HIGH_CLOUD_CONFIDENT_MAX)
        if high_max is not None and high is not None and high > high_max:
            return False
        return True

    @property
    def usable_confident(self) -> bool:
        """Both models present and both say usable."""
        return self._usable("primary") is True and self._usable("secondary") is True

    @property
    def usable_any(self) -> bool:
        return self._usable("primary") is True or self._usable("secondary") is True

    @property
    def usable_strict(self) -> bool:
        """Confident tier: both models usable with high cloud capped, no haze."""
        return (self._usable("primary", strict=True) is True
                and self._usable("secondary", strict=True) is True)

    @property
    def usable_any_strict(self) -> bool:
        return (self._usable("primary", strict=True) is True
                or self._usable("secondary", strict=True) is True)

    @property
    def dimmed_by(self) -> str | None:
        """What keeps a usable hour out of the confident tier, in words."""
        if self.misty:
            return "mist"
        if self.aerosol_hazy:
            return "dust or smoke haze"
        if self.usable_confident and not self.usable_strict:
            return "heavy high cloud"
        return None

    def layer(self, name: str) -> int | None:
        """Representative value for a layer: primary if present, else secondary."""
        v = self.primary.get(name)
        return v if v is not None else self.secondary.get(name)


@dataclass
class NightScore:
    night: date
    dark_hours: list[HourCloud]
    dark_start: datetime
    dark_end: datetime
    confident_usable_hours: int
    any_usable_hours: int
    best_run_len: int
    best_run_start: datetime | None
    best_run_any_len: int
    best_run_any_start: datetime | None
    layer_stats: dict[str, tuple[int, int] | None]  # layer -> (avg, peak)
    moon_illum: int
    moon_label: str
    tentative: bool
    rain_vetoed: bool
    rain_max_mm: float
    rain_max_prob: int | None
    rain_unknown_hours: int       # window hours with no full rain forecast
    wind_max_kmh: int
    gust_max_kmh: int
    windy_vetoed: bool
    exposure_window: str
    setup_window: str
    setup_usable_confident: int
    setup_usable_any: int
    setup_ok: bool
    setup_alignable: int          # window hours dark enough to align
    setup_align_from: datetime | None  # earliest clear + dark-enough moment
    strict_run_len: int           # both models agree AND high cloud ≤ confident cap
    setup_strict_ok: bool         # setup window passes with the high-cloud cap
    mist_from: datetime | None    # first misty dark hour
    fog_from: datetime | None     # first foggy dark hour
    min_visibility_m: float | None
    min_spread_c: float | None    # smallest temperature − dew point gap
    min_temp_c: float | None
    aod_max: float | None         # peak aerosol optical depth (None: no CAMS data)
    jet_max_kmh: int | None       # peak 250 hPa wind
    moon_up: list[tuple[datetime, datetime]]  # spans with the Moon up, within dark hours
    dimmed_by: str | None         # what most often keeps usable hours from the confident tier

    @property
    def sky_ok(self) -> bool:
        """The dark-hours cloud/run criteria pass — ignoring rain, wind and the
        setup gate. i.e. the sky itself is good enough to image."""
        if (
            self.confident_usable_hours >= MIN_USABLE_HOURS
            and self.best_run_len >= MIN_CONTINUOUS_RUN
        ):
            return True
        return (
            self.any_usable_hours >= MIN_USABLE_HOURS
            and self.best_run_any_len >= MIN_CONTINUOUS_RUN
        )

    @property
    def qualifies(self) -> bool:
        if self.rain_vetoed or self.windy_vetoed:
            return False
        if REQUIRE_SETUP_WINDOW and not self.setup_ok:
            return False  # can't deploy + polar align before bed → not actionable
        return self.sky_ok

    @property
    def confident(self) -> bool:
        """Qualifies AND both models agree on a run with modest high cloud."""
        if not self.qualifies or self.strict_run_len < CONFIDENT_MIN_RUN:
            return False
        return self.setup_strict_ok or not REQUIRE_SETUP_WINDOW

    @property
    def light(self) -> str:
        """Traffic light for this night: 🟢 confident go · 🟡 qualifies but
        speculative · 🔴 cloud / rain / wind / can't align before bed."""
        if self.confident:
            return "🟢"
        if self.qualifies:
            return "🟡"
        return "🔴"

    @property
    def speculative_reason(self) -> str:
        """Why a qualifying night is only 🟡."""
        if not _both_models_cover(self.dark_hours):
            return "only one model available"
        if self.best_run_len < CONFIDENT_MIN_RUN:
            return "models disagree"
        cause = self.dimmed_by or "heavy high cloud"
        if self.strict_run_len < CONFIDENT_MIN_RUN:
            return cause
        return f"{cause} at setup time"

    @property
    def usable_hours(self) -> int:
        return self.any_usable_hours if self.tentative else self.confident_usable_hours

    @property
    def run(self) -> tuple[int, datetime | None]:
        """(length, start) of the best usable run at this night's confidence."""
        if self.tentative:
            return self.best_run_any_len, self.best_run_any_start
        return self.best_run_len, self.best_run_start


# --------------------------------------------------------------------------- #
# Forecast fetch                                                               #
# --------------------------------------------------------------------------- #

def fetch_forecast() -> list[HourCloud]:
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
                  "precipitation,precipitation_probability,"
                  "wind_speed_10m,wind_gusts_10m,"
                  "visibility,temperature_2m,dew_point_2m,wind_speed_250hPa",
        "models": f"{PRIMARY_MODEL},{SECONDARY_MODEL}",
        "timezone": TIMEZONE,
        "forecast_days": FORECAST_DAYS,
    }
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=30)
    resp.raise_for_status()
    hourly = resp.json()["hourly"]
    times = hourly["time"]

    def series(field: str, model: str) -> list:
        # With multiple models Open-Meteo suffixes the key with the model id;
        # fall back to the unsuffixed name for a single-model response.
        return (
            hourly.get(f"{field}_{model}")
            or hourly.get(field)
            or [None] * len(times)
        )

    primary_cloud = {l: series(f"cloud_cover_{l}", PRIMARY_MODEL) for l in LAYERS}
    secondary_cloud = {l: series(f"cloud_cover_{l}", SECONDARY_MODEL) for l in LAYERS}
    precip_mm = {"primary": series("precipitation", PRIMARY_MODEL),
                 "secondary": series("precipitation", SECONDARY_MODEL)}
    precip_prob = {"primary": series("precipitation_probability", PRIMARY_MODEL),
                   "secondary": series("precipitation_probability", SECONDARY_MODEL)}
    wind_kmh = {"primary": series("wind_speed_10m", PRIMARY_MODEL),
                "secondary": series("wind_speed_10m", SECONDARY_MODEL)}
    gust_kmh = {"primary": series("wind_gusts_10m", PRIMARY_MODEL),
                "secondary": series("wind_gusts_10m", SECONDARY_MODEL)}

    def both(field: str) -> dict[str, list]:
        return {"primary": series(field, PRIMARY_MODEL),
                "secondary": series(field, SECONDARY_MODEL)}

    visibility, temp, dew, jet = (both(f) for f in (
        "visibility", "temperature_2m", "dew_point_2m", "wind_speed_250hPa"))
    aod = fetch_aerosol()

    out: list[HourCloud] = []
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t).replace(tzinfo=TZ)
        out.append(HourCloud(
            time=dt,
            primary={l: primary_cloud[l][i] for l in LAYERS},
            secondary={l: secondary_cloud[l][i] for l in LAYERS},
            precip_mm={k: precip_mm[k][i] for k in precip_mm},
            precip_prob={k: precip_prob[k][i] for k in precip_prob},
            wind_kmh={k: wind_kmh[k][i] for k in wind_kmh},
            gust_kmh={k: gust_kmh[k][i] for k in gust_kmh},
            visibility_m={k: visibility[k][i] for k in visibility},
            temp_c={k: temp[k][i] for k in temp},
            dew_point_c={k: dew[k][i] for k in dew},
            jet_kmh={k: jet[k][i] for k in jet},
            aod=aod.get(t),
        ))
    return out


def fetch_aerosol() -> dict[str, float | None]:
    """Aerosol optical depth by local hour (Open-Meteo's ISO time string), from
    the CAMS forecast. Best effort: on any failure the haze check is skipped."""
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "aerosol_optical_depth",
        "timezone": TIMEZONE,
        "forecast_days": FORECAST_DAYS,
    }
    try:
        resp = requests.get(AIR_QUALITY_URL, params=params, timeout=30)
        resp.raise_for_status()
        hourly = resp.json()["hourly"]
        return dict(zip(hourly["time"], hourly["aerosol_optical_depth"]))
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"[aerosol] no CAMS forecast ({e!r}) — skipping the haze check.")
        return {}


# --------------------------------------------------------------------------- #
# Night scoring                                                                #
# --------------------------------------------------------------------------- #

def is_dark(dt: datetime) -> bool:
    """True when the sun is below the twilight depression at `dt`. Computing
    elevation per hour avoids the date-boundary ambiguity of dusk/dawn pairing
    near the solstice."""
    return elevation(OBSERVER, dt) <= -TWILIGHT_DEPRESSION


def night_of(dt: datetime) -> date:
    """The calendar date of the evening a dark hour belongs to. Shifting back
    12h groups post-midnight hours with the preceding evening."""
    return (dt - timedelta(hours=12)).date()


def is_alignable(dt: datetime) -> bool:
    """True when the sky is dark enough at `dt` to polar align (see/solve stars)."""
    return elevation(OBSERVER, dt) <= -ALIGN_DEPRESSION


def _longest_run(flags: list[bool], times: list[datetime]) -> tuple[int, datetime | None]:
    best_len = cur_len = 0
    best_start = cur_start = None
    for flag, t in zip(flags, times):
        if flag:
            if cur_len == 0:
                cur_start = t
            cur_len += 1
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
        else:
            cur_len, cur_start = 0, None
    return best_len, best_start


def moon_for(night: date) -> tuple[int, str]:
    phase = moon.phase(night)  # 0..27.99
    illum = round((1 - math.cos(2 * math.pi * phase / 27.99)) / 2 * 100)
    labels = [
        (1.84, "new"), (5.53, "waxing crescent"), (9.22, "first quarter"),
        (12.91, "waxing gibbous"), (16.61, "full"), (20.30, "waning gibbous"),
        (23.99, "last quarter"), (26.15, "waning crescent"),
    ]
    label = "new"
    for threshold, name in labels:
        if phase < threshold:
            label = name
            break
    return illum, label


def moon_up(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """The spans between `start` and `end` with the Moon above the horizon, to
    the nearest 10 minutes. astral's moon.elevation ignores the time zone of
    the time it's given (its sun.elevation doesn't), so it's passed UTC."""
    spans: list[tuple[datetime, datetime]] = []
    t, up_since = start, None
    while t <= end:
        up = moon.elevation(OBSERVER, t.astimezone(timezone.utc)) > 0
        if up and up_since is None:
            up_since = t
        elif not up and up_since is not None:
            spans.append((up_since, t))
            up_since = None
        t += timedelta(minutes=10)
    if up_since is not None:
        spans.append((up_since, end))
    return spans


def exposure_window(night: date) -> tuple[datetime, datetime]:
    """The hours the gear is physically outside: EXPOSURE_START_HOUR on the
    evening of `night` to EXPOSURE_END_HOUR the next morning."""
    start = datetime.combine(night, time(EXPOSURE_START_HOUR), tzinfo=TZ)
    end = datetime.combine(night + timedelta(days=1), time(EXPOSURE_END_HOUR), tzinfo=TZ)
    return start, end


def setup_end_hour(night: date) -> int:
    """Bedtime (setup-window end) for the evening of `night`, applying any
    per-weekday override — e.g. a later cut-off on Fri/Sat nights. May be ≥24
    to mean past midnight (25 = 01:00 next morning)."""
    return SETUP_WINDOW_END_HOUR_BY_WEEKDAY.get(night.weekday(), SETUP_WINDOW_END_HOUR)


def setup_window(night: date) -> tuple[datetime, datetime]:
    """The pre-bed window to set up and polar align, on the evening of `night`.
    The end (bedtime) can fall past midnight on nights with a later cut-off."""
    start = datetime.combine(night, time(SETUP_WINDOW_START_HOUR), tzinfo=TZ)
    day_offset, end_hour = divmod(setup_end_hour(night), 24)
    end = datetime.combine(night + timedelta(days=day_offset), time(end_hour), tzinfo=TZ)
    return start, end


def _usable_counts(hours: list[HourCloud]) -> tuple[int, int]:
    """(confident-usable hours, any-usable hours) over a list of hours."""
    return (
        sum(h.usable_confident for h in hours),
        sum(h.usable_any for h in hours),
    )


def score_night(
    night: date,
    dark_hours: list[HourCloud],
    exposure_hours: list[HourCloud],
    setup_hours: list[HourCloud],
) -> NightScore | None:
    if RESPECT_CURFEW:
        dark_hours = [h for h in dark_hours if h.time.hour < 6 or h.time.hour < CURFEW_HOUR]
    if not dark_hours:
        return None

    # Precipitation veto across the whole equipment-exposure window. Hours the
    # forecast doesn't reach, or that lack an amount or probability, are unknown
    # and veto the night just like forecast rain.
    win_start, win_end = exposure_window(night)
    window_hours = int((win_end - win_start).total_seconds() // 3600)
    rain_unknown_hours = (sum(h.rain_unknown for h in exposure_hours)
                          + max(0, window_hours - len(exposure_hours)))
    rain_vetoed = rain_unknown_hours > 0 or any(h.wet for h in exposure_hours)
    rain_max_mm = max((h.max_precip_mm for h in exposure_hours), default=0.0)
    probs = [h.max_precip_prob for h in exposure_hours if h.max_precip_prob is not None]
    rain_max_prob = max(probs) if probs else None

    # Wind over the exposure window — gusts are the spoiler, so we veto on gusts.
    wind_max = round(max((h.max_wind_kmh for h in exposure_hours), default=0.0))
    gust_max = round(max((h.max_gust_kmh for h in exposure_hours), default=0.0))
    windy_vetoed = gust_max > WIND_GUST_VETO_KMH

    # Setup / polar-align window (pre-bed): need hours that are BOTH dark enough
    # to align AND clear, before bedtime. In midsummer the darkness test is what
    # makes a night fail — it's simply not dark enough to align before bed.
    alignable_setup = [h for h in setup_hours if is_alignable(h.time)]
    setup_conf, setup_any = _usable_counts(alignable_setup)
    setup_ok = setup_conf >= SETUP_MIN_CLEAR_HOURS or setup_any >= SETUP_MIN_CLEAR_HOURS
    setup_strict_ok = sum(h.usable_any_strict for h in alignable_setup) >= SETUP_MIN_CLEAR_HOURS
    clear_align_times = [h.time for h in alignable_setup if h.usable_any]
    setup_align_from = min(clear_align_times) if clear_align_times else None
    su_start, su_end = setup_window(night)

    times = [h.time for h in dark_hours]
    confident_flags = [h.usable_confident for h in dark_hours]
    any_flags = [h.usable_any for h in dark_hours]

    best_run_len, best_run_start = _longest_run(confident_flags, times)
    best_run_any_len, best_run_any_start = _longest_run(any_flags, times)
    strict_run_len, _ = _longest_run([h.usable_strict for h in dark_hours], times)

    def stats(layer: str) -> tuple[int, int] | None:
        vals = [h.layer(layer) for h in dark_hours if h.layer(layer) is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals)), max(vals)

    confident = sum(confident_flags)
    illum, label = moon_for(night)
    tentative = not (confident >= MIN_USABLE_HOURS and best_run_len >= MIN_CONTINUOUS_RUN)

    # Haze, dew and seeing over the dark hours.
    def least(vals):
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None

    def most(vals):
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    misty = [h.time for h in dark_hours if h.misty]
    foggy = [h.time for h in dark_hours if h.fog]
    jet = most(h.max_jet_kmh for h in dark_hours)
    causes = [h.dimmed_by for h in dark_hours if h.usable_any and h.dimmed_by]
    dimmed_by = max(sorted(set(causes)), key=causes.count) if causes else None

    return NightScore(
        night=night,
        dark_hours=dark_hours,
        dark_start=times[0],
        dark_end=times[-1] + timedelta(hours=1),
        confident_usable_hours=confident,
        any_usable_hours=sum(any_flags),
        best_run_len=best_run_len,
        best_run_start=best_run_start,
        best_run_any_len=best_run_any_len,
        best_run_any_start=best_run_any_start,
        layer_stats={l: stats(l) for l in LAYERS},
        moon_illum=illum,
        moon_label=label,
        tentative=tentative,
        rain_vetoed=rain_vetoed,
        rain_max_mm=rain_max_mm,
        rain_max_prob=rain_max_prob,
        rain_unknown_hours=rain_unknown_hours,
        wind_max_kmh=wind_max,
        gust_max_kmh=gust_max,
        windy_vetoed=windy_vetoed,
        exposure_window=f"{win_start:%H:%M}–{win_end:%H:%M}",
        setup_window=f"{su_start:%H:%M}–{su_end:%H:%M}",
        setup_usable_confident=setup_conf,
        setup_usable_any=setup_any,
        setup_ok=setup_ok,
        setup_alignable=len(alignable_setup),
        setup_align_from=setup_align_from,
        strict_run_len=strict_run_len,
        setup_strict_ok=setup_strict_ok,
        mist_from=misty[0] if misty else None,
        fog_from=foggy[0] if foggy else None,
        min_visibility_m=least(h.min_visibility_m for h in dark_hours),
        min_spread_c=least(h.spread_c for h in dark_hours),
        min_temp_c=least(h.min_temp_c for h in dark_hours),
        aod_max=most(h.aod for h in dark_hours),
        jet_max_kmh=round(jet) if jet is not None else None,
        moon_up=moon_up(times[0], times[-1] + timedelta(hours=1)),
        dimmed_by=dimmed_by,
    )


# --------------------------------------------------------------------------- #
# Forecast history — what changed since the last run                          #
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict, today: date) -> None:
    """Write the history, dropping nights that are over and old snapshots."""
    keep_from = (today - timedelta(days=1)).isoformat()
    state["nights"] = {
        night: runs[-STATE_KEEP_RUNS:]
        for night, runs in state.get("nights", {}).items()
        if night >= keep_from
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))


def snapshot(s: NightScore, at: datetime) -> dict:
    """The compact per-night record kept in the history."""
    low, mid = s.layer_stats["low"], s.layer_stats["mid"]
    return {
        "at": at.isoformat(timespec="minutes"),
        "mode": RUN_MODE,
        "light": s.light,
        "usable": s.usable_hours,
        "low": low[0] if low else None,
        "mid": mid[0] if mid else None,
        "rain_mm": round(s.rain_max_mm, 1),
        "rain_pct": s.rain_max_prob,
        "gust": s.gust_max_kmh,
    }


def _when(stamp: str, now: datetime) -> str:
    """'this morning' / 'midday' / 'yesterday evening' / 'Tue 18:00' for a
    history stamp."""
    t = datetime.fromisoformat(stamp).astimezone(TZ)
    part = ("morning" if t.hour < 11 else "midday" if t.hour < 14
            else "afternoon" if t.hour < 17 else "evening")
    days = (now.date() - t.date()).days
    if days == 0:
        return part if part == "midday" else f"this {part}"
    if days == 1:
        return f"yesterday {part}"
    return f"{t:%a %H:%M}"


def _delta(label: str, old, new, threshold: int, unit: str = "") -> str | None:
    if old is None or new is None or abs(new - old) < threshold:
        return None
    return f"{label} {old}→{new}{unit}"


def describe_change(history: list[dict], cur: dict, now: datetime) -> str | None:
    """One line on how this night's forecast has moved since the previous run,
    or None when there's no earlier run to compare with."""
    if not history:
        return None
    prev = history[-1]
    moved = [d for d in (
        _delta("usable", prev.get("usable"), cur["usable"], CHANGE_HOURS, "h"),
        _delta("low", prev.get("low"), cur["low"], CHANGE_CLOUD_PTS, "%"),
        _delta("mid", prev.get("mid"), cur["mid"], CHANGE_CLOUD_PTS, "%"),
        _delta("gusts", prev.get("gust"), cur["gust"], CHANGE_GUST_KMH),
    ) if d]
    old_mm, new_mm = prev.get("rain_mm"), cur["rain_mm"]
    if old_mm is not None and (old_mm > PRECIP_AMOUNT_MAX) != (new_mm > PRECIP_AMOUNT_MAX):
        moved.append(f"rain {old_mm:.1f}→{new_mm:.1f}mm")
    old_pct, new_pct = prev.get("rain_pct"), cur["rain_pct"]
    if (old_pct is not None and new_pct is not None
            and (old_pct > PRECIP_PROB_MAX) != (new_pct > PRECIP_PROB_MAX)):
        moved.append(f"rain chance {old_pct}→{new_pct}%")

    trend = "".join(h["light"] for h in history[-3:]) + "→" + cur["light"]
    when = _when(prev["at"], now)
    if prev["light"] == cur["light"] and not moved:
        return f"↻ {trend} unchanged since {when}"
    return f"↻ {trend} since {when}" + (" · " + " · ".join(moved) if moved else "")


# --------------------------------------------------------------------------- #
# Message + delivery                                                           #
# --------------------------------------------------------------------------- #

def _fmt_layer(stat: tuple[int, int] | None) -> str:
    if stat is None:
        return "?"
    avg, peak = stat
    return f"{avg}%" if avg == peak else f"{avg}% (peak {peak}%)"


def _mark(h: HourCloud) -> str:
    """✓ both models usable, high cloud modest, no haze · ◐ both usable but
    hazy (heavy high cloud, mist, or dust or smoke) · ~ only one model usable ·
    ✗ neither (cloud or fog)."""
    if h.usable_strict:
        return "✓"
    if h.usable_confident:
        return "◐"
    return "~" if h.usable_any else "✗"


def hour_strip(s: NightScore) -> str:
    """The whole dark window hour by hour, compactly: 21✓ 22✓ 23~ 00✗ …"""
    return " ".join(f"{h.time:%H}{_mark(h)}" for h in s.dark_hours)


def format_night(s: NightScore, change: str | None = None, strip: bool = False) -> str:
    """Full per-night block. Truthful for any night, not just qualifying ones
    (the evening update shows tonight whatever the verdict). `strip` adds the
    compact hour-by-hour line when the night is too long for per-hour detail."""
    day = s.night.strftime("%a %-d %b")
    window = f"{s.dark_start:%H:%M}–{s.dark_end:%H:%M}"
    total = len(s.dark_hours)

    if s.light == "🟢":
        conf = "🟢 confident"
    elif s.light == "🟡":
        conf = f"🟡 speculative — {s.speculative_reason}"
    else:
        conf = ("tentative (single model / models disagree)" if s.tentative
                else "high (both models agree)")
    run_len, run_start = s.run
    run = f"best run {run_len}h from {run_start:%H:%M}" if run_len else "no usable run"

    lines = [
        f"<b>{day}</b> · dark {window} · Moon {s.moon_illum}% "
        f"({s.moon_label}, {_moon_when(s)})",
        f"{s.usable_hours}/{total} h usable · {run} · conf: {conf}",
    ]

    if REQUIRE_SETUP_WINDOW:
        if s.setup_ok:
            frm = s.setup_align_from.strftime("%H:%M") if s.setup_align_from else "?"
            setup_tag = "" if s.setup_usable_confident >= SETUP_MIN_CLEAR_HOURS else " (tentative)"
            lines.append(f"✅ set up &amp; align from {frm} — dark + clear before bed{setup_tag}")
        elif s.setup_alignable == 0:
            lines.append(f"🌆 not dark enough to align before bed ({s.setup_window})")
        else:
            lines.append(f"☁ no clear hour to set up &amp; align ({s.setup_window})")

    cloud = (
        f"low {_fmt_layer(s.layer_stats['low'])} · "
        f"mid {_fmt_layer(s.layer_stats['mid'])} · "
        f"high {_fmt_layer(s.layer_stats['high'])}"
    )
    high = s.layer_stats["high"]
    if high is not None and high[0] > HIGH_CLOUD_CONFIDENT_MAX:
        cloud += " — heavy high cloud (may be thick)"
    elif high is not None and high[0] >= HIGH_CLOUD_NOTE:
        cloud += " — some high cloud (ok for narrowband)"
    lines.append(cloud)
    lines += _air_lines(s)

    if s.rain_vetoed:
        lines.append(f"🌧 rain {s.exposure_window}: {_rain_detail(s)}")
    else:
        lines.append(f"rain {s.exposure_window}: dry (0% chance, 0.0mm)")

    wind_line = f"wind: {s.wind_max_kmh} km/h, gusts {s.gust_max_kmh} km/h"
    if s.windy_vetoed:
        wind_line += " 💨 too windy"
    elif s.gust_max_kmh > WIND_GUST_WARN_KMH or s.wind_max_kmh > WIND_SUSTAINED_WARN_KMH:
        wind_line += " ⚠ breezy"
    lines.append(wind_line)

    if total <= MAX_HOURLY_DETAIL:
        for h in s.dark_hours:
            lines.append(
                f"{h.time:%H:%M} · low {h.layer('low')}% "
                f"mid {h.layer('mid')}% high {h.layer('high')}% {_mark(h)}"
            )
    elif strip:
        lines.append(hour_strip(s))

    if change:
        lines.append(change)
    return "\n".join(lines)


def _moon_when(s: NightScore) -> str:
    """When the Moon is up during the dark hours: 'up from 20:10', 'down all night'."""
    if not s.moon_up:
        return "down all night"
    parts = []
    for rise, set_ in s.moon_up:
        if rise <= s.dark_start and set_ >= s.dark_end:
            return "up all night"
        if rise <= s.dark_start:
            parts.append(f"sets {set_:%H:%M}")
        elif set_ >= s.dark_end:
            parts.append(f"rises {rise:%H:%M}")
        else:
            parts.append(f"up {rise:%H:%M}–{set_:%H:%M}")
    return ", ".join(parts)


def _air_lines(s: NightScore) -> list[str]:
    """Mist, fog, dust or smoke, dew and seeing — only the ones that apply."""
    lines = []
    haze = []
    if s.fog_from and s.mist_from and s.mist_from < s.fog_from:
        haze.append(f"mist from {s.mist_from:%H:%M}, fog from {s.fog_from:%H:%M}")
    elif s.fog_from:
        haze.append(f"fog from {s.fog_from:%H:%M}")
    elif s.mist_from:
        haze.append(f"mist from {s.mist_from:%H:%M}")
    if haze:
        if s.min_visibility_m is not None:
            haze[0] += f" (visibility down to {s.min_visibility_m / 1000:.1f} km)"
        else:
            haze[0] += " (air near its dew point)"
    if s.aod_max is not None and s.aod_max > AOD_HAZE_MAX:
        haze.append(f"dust or smoke haze (AOD up to {s.aod_max:.2f})")
    if haze:
        lines.append("🌫 " + " · ".join(haze))
    if s.min_spread_c is not None and s.min_spread_c <= DEW_SPREAD_C:
        frost = s.min_temp_c is not None and s.min_temp_c <= 0
        lines.append(f"{'❄ frost' if frost else '💧 dew'} likely — heaters on")
    if s.jet_max_kmh is not None and s.jet_max_kmh > JET_SEEING_KMH:
        lines.append(f"🌀 jet stream {s.jet_max_kmh} km/h overhead — seeing likely poor")
    return lines


LIGHT_RANK = {"🔴": 0, "🟡": 1, "🟢": 2}


def _rain_detail(s: NightScore) -> str:
    """Why the rain veto fired: the forecast chance/amount, plus any hours the
    forecast doesn't fully cover (treated as wet)."""
    prob = f"{s.rain_max_prob}%" if s.rain_max_prob is not None else "n/a"
    detail = f"chance up to {prob}, max {s.rain_max_mm:.1f}mm"
    if s.rain_unknown_hours:
        detail += f", no rain forecast for {s.rain_unknown_hours}h (treated as wet)"
    return detail


def _reason(s: NightScore) -> str:
    """One-line human verdict for the daily digest."""
    if s.qualifies:
        run_len, run_start = s.run
        frm = run_start.strftime("%H:%M") if run_start else "?"
        summary = f"usable {s.usable_hours}h · best run {run_len}h from {frm}"
        if s.confident:
            return f"🟢 {summary}"
        return f"🟡 maybe ({s.speculative_reason}) · {summary}"
    if s.rain_vetoed:
        return f"🌧 rain in the window ({_rain_detail(s)})"
    if s.windy_vetoed:
        return f"💨 too windy (gusts {s.gust_max_kmh} km/h)"
    if REQUIRE_SETUP_WINDOW and not s.setup_ok:
        if s.setup_alignable == 0:
            return "🌆 not dark enough to align before bed"
        return f"☁ {'foggy' if _foggy(s) else 'cloudy'} during the setup window"
    low = s.layer_stats["low"]
    fog = f", fog from {s.fog_from:%H:%M}" if s.fog_from else ""
    return f"☁ {_short_reason(s)} (low {low[0] if low else '?'}%{fog})"


def _outlook_line(s: NightScore, change: str | None) -> str:
    """Short per-night entry: date, moon, verdict and what changed."""
    day = s.night.strftime("%a %-d %b")
    windy = s.gust_max_kmh > WIND_GUST_WARN_KMH and not s.windy_vetoed
    extra = f" · 💨 gusts {s.gust_max_kmh}" if windy else ""
    line = f"<b>{day}</b> · Moon {s.moon_illum}%{extra}\n{_reason(s)}"
    return f"{line}\n{change}" if change else line


def _nights_title(nights: list[NightScore], one: str, many: str) -> str:
    if len(nights) == 1:
        return f"{one}: {nights[0].night.strftime('%a %-d %b')}"
    return many.format(n=len(nights))


def build_message(scores: list[NightScore], changes: dict[date, str | None]) -> tuple[str, str]:
    """Outlook alert: 🟢/🟡 nights in full, the rest as short lines."""
    go = [s for s in scores if s.qualifies]
    sure = [s for s in go if s.confident]
    if sure:
        title = _nights_title(sure, "🟢 Clear night", "🟢 {n} clear nights ahead")
        if len(go) > len(sure):
            title += f" (+{len(go) - len(sure)} maybe)"
    else:
        title = _nights_title(go, "🟡 Maybe", "🟡 {n} possible nights")
    parts = [format_night(s, changes.get(s.night)) for s in go]
    parts += [_outlook_line(s, changes.get(s.night)) for s in scores if not s.qualifies]
    return title, "\n\n".join(parts)


def build_digest(
    scores: list[NightScore],
    changes: dict[date, str | None],
    downgraded: list[NightScore],
) -> tuple[str, str]:
    """Nothing qualifies: a one-line reason per night."""
    if downgraded:
        days = ", ".join(s.night.strftime("%a") for s in downgraded)
        title = f"🔴 SkyGuard: {days} now off"
    else:
        title = f"🔴 SkyGuard: nothing on (next {len(scores)})"
    lines = ["No night qualifies right now — outlook:"]
    lines += [_outlook_line(s, changes.get(s.night)) for s in scores]
    return title, "\n\n".join(lines)


def _short_reason(s: NightScore) -> str:
    """Why a 🔴 night is off, in a few words for the title."""
    if s.rain_vetoed:
        return "rain forecast"
    if s.windy_vetoed:
        return "too windy"
    if REQUIRE_SETUP_WINDOW and not s.setup_ok:
        if s.setup_alignable == 0:
            return "not dark enough to align before bed"
        return "foggy before bed" if _foggy(s) else "cloudy before bed"
    return "foggy" if _foggy(s) else "too cloudy"


def _foggy(s: NightScore) -> bool:
    """Fog, not cloud, is what rules the night out: fog is forecast and the
    low cloud stays within its limit on average."""
    low = s.layer_stats["low"]
    return bool(s.fog_from) and low is not None and low[0] <= LOW_CLOUD_MAX


def build_tonight(s: NightScore, change: str | None, prev_light: str | None) -> tuple[str, str]:
    """Evening update: go / maybe / no-go for tonight, with the hour-by-hour
    picture and how it has moved since the last run."""
    if s.light == "🟢":
        if REQUIRE_SETUP_WINDOW and s.setup_align_from:
            title = f"🟢 Tonight's on — set up from {s.setup_align_from:%H:%M}"
        else:
            start = s.run[1]
            title = f"🟢 Tonight's on — clear from {start:%H:%M}" if start else "🟢 Tonight's on"
    elif s.light == "🟡":
        title = f"🟡 Tonight's a maybe — {s.speculative_reason}"
    else:
        title = f"🔴 Tonight's off — {_short_reason(s)}"
    if prev_light and prev_light != s.light:
        title += f" (was {prev_light})"
    return title, format_night(s, change, strip=True)


# Chart styling, after a phone weather app at night: a deep sky gradient,
# translucent rounded cards, smooth curves with soft fills, and a plain-words
# headline. Model colours were checked for colour-blind separation against the
# card surface; each hour's verdict has an icon as well as a colour. Icons are
# Meteocons by Bas Milius (line style, MIT licence; see icons/LICENSE), trimmed
# to their artwork and rasterised to PNG so drawing needs no SVG renderer.
CHART_DPI = 150
CHART_WIDTH = 7.2                       # inches → 1080 px
CHART_SKY = ("#0b1330", "#27345f")      # gradient, top → bottom
CHART_CARD = (1, 1, 1, 0.075)
CHART_CARD_EDGE = (1, 1, 1, 0.09)
CHART_INK = "#ffffff"
CHART_SOFT = (1, 1, 1, 0.72)
CHART_FAINT = (1, 1, 1, 0.5)
CHART_GRID = (1, 1, 1, 0.12)
CHART_TWILIGHT = (0, 0, 0, 0.22)
CHART_SETUP_BAND = (1, 1, 1, 0.05)
CHART_MODELS = ((PRIMARY_MODEL, "primary", "#2a93dc"),
                (SECONDARY_MODEL, "secondary", "#e25c84"))
MARK_COLOURS = {"✓": "#30d158", "◐": "#ffd60a", "~": "#ff9f0a", "✗": "#ff453a"}
MARK_WORDS = (("✓", "clear"), ("◐", "hazy"), ("~", "models differ"), ("✗", "cloudy"),
              ("fog", "fog"))
ICONS = {"✓": "clear-night", "◐": "haze-night", "~": "partly-cloudy-night",
         "✗": "overcast", "fog": "fog",                              # hourly verdicts
         "moon": "clear-night", "cloud": "cloudy", "mist": "mist"}  # card labels
ICON_DIR = Path(__file__).resolve().parent / "icons"
VIS_SCALE_KM = 30  # visibility card: 0 to this, clearer air pinned at the top
MODEL_NAMES = {
    "knmi_harmonie_arome_netherlands": "KNMI HARMONIE",
    "ecmwf_ifs025": "ECMWF IFS",
}
FONT_DIR = Path(__file__).resolve().parent / "fonts"


def render_chart(s: NightScore, hours: list[HourCloud]) -> bytes | None:
    """PNG of the night's cloud forecast hour by hour, or None if it can't be
    drawn (the push then goes out as text only)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        print("[chart] matplotlib not installed — sending without a chart.")
        return None
    try:
        # The symbol fallback has no Medium weight; that's expected, not news.
        logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
        for ttf in FONT_DIR.glob("*.ttf"):
            font_manager.fontManager.addfont(str(ttf))
        # Inter for text; DejaVu Sans covers any symbol Inter lacks.
        plt.rcParams.update({"font.family": ["Inter", "DejaVu Sans"]})
        return _draw_chart(plt, s, hours)
    except Exception as e:  # a broken chart must never cost the alert
        print(f"[chart] failed ({e!r}) — sending without a chart.")
        return None


def _headline(s: NightScore) -> tuple[str, str]:
    """('Clear 19:00–23:00', 'Confident go · set up from 21:00 · bed 01:00 · …')."""
    if s.qualifies:
        run_len, run_start = s.run
        run_end = run_start + timedelta(hours=run_len)
        # A 🟡 night dimmed by mist or haze isn't "clear", whatever the cloud says.
        reason = "" if s.confident else s.speculative_reason
        word = ("Misty" if reason.startswith("mist")
                else "Hazy" if reason.startswith(("dust", "heavy high cloud")) else "Clear")
        if run_start <= s.dark_start and run_end >= s.dark_end:
            head = f"{word} all night"
        elif run_end >= s.dark_end:
            head = f"{word} from {run_start:%H:%M}"
        else:
            head = f"{word} {run_start:%H:%M}–{run_end:%H:%M}"
    else:
        head = _short_reason(s).capitalize()
    verdict = {"🟢": "Confident go", "🟡": f"Maybe: {s.speculative_reason}",
               "🔴": "Not tonight"}[s.light]
    parts = [verdict]
    if s.rain_vetoed:
        prob = f", {s.rain_max_prob}% chance" if s.rain_max_prob is not None else ""
        parts.append(f"rain up to {s.rain_max_mm:.1f} mm{prob}")
    elif s.windy_vetoed:
        parts.append(f"gusts {s.gust_max_kmh} km/h")
    if REQUIRE_SETUP_WINDOW:
        if s.qualifies and s.setup_align_from:
            parts.append(f"set up from {s.setup_align_from:%H:%M}")
        parts.append(f"bed {setup_window(s.night)[1]:%H:%M}")
    parts.append(f"moon {s.moon_illum}%")
    return head, " · ".join(parts)


def _monotone(xs: list[float], ys: list[float], per: int = 16) -> tuple[list[float], list[float]]:
    """Dense points on a monotone cubic (Fritsch–Carlson) through (xs, ys): a
    smooth curve that never overshoots, so cloud stays within 0–100% and a flat
    run stays flat."""
    n = len(xs)
    if n < 2:
        return xs, ys
    d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    m = [d[0]] + [0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2
                  for i in range(1, n - 1)] + [d[-1]]
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        if a * a + b * b > 9:
            t = 3 / math.sqrt(a * a + b * b)
            m[i], m[i + 1] = t * a * d[i], t * b * d[i]
    out_x, out_y = [], []
    for i in range(n - 1):
        h = xs[i + 1] - xs[i]
        for k in range(per):
            t = k / per
            h00, h10 = 2 * t**3 - 3 * t**2 + 1, t**3 - 2 * t**2 + t
            h01, h11 = -2 * t**3 + 3 * t**2, t**3 - t**2
            out_x.append(xs[i] + t * h)
            out_y.append(h00 * ys[i] + h10 * h * m[i] + h01 * ys[i + 1] + h11 * h * m[i + 1])
    return out_x + [xs[-1]], out_y + [ys[-1]]


@functools.cache
def _icon_image(name: str):
    import matplotlib.image as mpimg
    return mpimg.imread(ICON_DIR / f"{name}.png")


def _icon_size(mark: str, size: float) -> tuple[float, float]:
    """(width, height) in inches of `mark`'s icon at `size`. Icons are trimmed
    to their artwork and scaled to the same area (size²), so a wide cloud and
    a round moon carry the same visual weight."""
    h_px, w_px = _icon_image(ICONS[mark]).shape[:2]
    k = size / math.sqrt(w_px * h_px)
    return w_px * k, h_px * k


def _draw_icon(fig, mark: str, x: float, y: float, size: float, muted: bool = False) -> None:
    """The icon for an hour's verdict (✓ ◐ ~ ✗, or "fog") or a card label
    ("moon", "cloud", "mist"), centred on (x, y) inches. `muted` tints it to
    the faint label ink."""
    img = _icon_image(ICONS[mark])
    if muted:
        img = img.copy()
        img[..., :3] = 1.0
        img[..., 3] *= CHART_FAINT[3]
    w, h = _icon_size(mark, size)
    W, H = fig.get_size_inches()
    ax = fig.add_axes(((x - w / 2) / W, (y - h / 2) / H, w / W, h / H))
    ax.imshow(img, interpolation="lanczos")
    ax.set_axis_off()


def _draw_chart(plt, s: NightScore, hours: list[HourCloud]) -> bytes | None:
    import io

    from matplotlib.colors import to_rgb
    from matplotlib.lines import Line2D
    from matplotlib.patches import FancyBboxPatch, Polygon

    # Span the equipment-exposure window, widened if darkness starts earlier
    # (deep winter) so every dark hour is on the chart.
    win_start, win_end = exposure_window(s.night)
    start, end = min(win_start, s.dark_start), max(win_end, s.dark_end)
    span = [h for h in hours if start <= h.time <= end]
    if len(span) < 2:
        return None
    x_of = lambda t: (t - start).total_seconds() / 3600  # noqa: E731
    xs = [x_of(h.time) for h in span]
    su_start, su_end = setup_window(s.night)

    high_limit = HIGH_CLOUD_MAX if HIGH_CLOUD_MAX is not None else HIGH_CLOUD_CONFIDENT_MAX
    # (key, title, limit on the 0–100 scale, card height, y labels)
    cards = [("low", f"LOW CLOUD · LIMIT {LOW_CLOUD_MAX}%", LOW_CLOUD_MAX, 1.75, ("0%", "100%")),
             ("mid", f"MID CLOUD · LIMIT {MID_CLOUD_MAX}%", MID_CLOUD_MAX, 1.1, ("0%", "100%")),
             ("high", f"HIGH CLOUD · CIRRUS · LIMIT {high_limit}%", high_limit, 1.3,
              ("0%", "100%"))]
    # Visibility, for mist and fog, on the same 0–100 scale: 0 to VIS_SCALE_KM,
    # anything clearer pinned at the top. Only drawn when a model publishes it.
    if any(v is not None for h in span for v in h.visibility_m.values()):
        cards.append(("visibility", f"VISIBILITY · MIST BELOW {MIST_VISIBILITY_M // 1000} KM",
                      MIST_VISIBILITY_M / 1000 / VIS_SCALE_KM * 100, 1.1,
                      ("0 km", f"{VIS_SCALE_KM}+ km")))

    def value(h: HourCloud, src: str, key: str) -> float | None:
        if key == "visibility":
            vis = h.visibility_m.get(src)
            return None if vis is None else min(100.0, vis / 1000 / VIS_SCALE_KM * 100)
        return getattr(h, src).get(key)

    # Layout in inches, top down.
    W, M, P, GAP, R = CHART_WIDTH, 0.22, 0.16, 0.11, 0.16
    HEADER, HOURLY, FOOTER = 0.98, 1.26, 0.36
    H = M + HEADER + GAP + HOURLY + sum(c[3] + GAP for c in cards) + FOOTER
    fig = plt.figure(figsize=(W, H), dpi=CHART_DPI)
    inch = fig.dpi_scale_trans

    sky = fig.add_axes((0, 0, 1, 1), zorder=-10)
    top, bottom = to_rgb(CHART_SKY[0]), to_rgb(CHART_SKY[1])
    sky.imshow([[[top[c] + (bottom[c] - top[c]) * i / 255 for c in range(3)]]
                for i in range(256)], aspect="auto", extent=(0, 1, 0, 1))
    sky.set_axis_off()

    def text(x, y, s_, **kw):
        kw.setdefault("color", CHART_INK)
        return fig.text(x, y, s_, transform=inch, **kw)

    def card(y_top: float, height: float) -> tuple[float, float, float, float]:
        x, w, y = M, W - 2 * M, y_top - height
        fig.add_artist(FancyBboxPatch((x, y), w, height, transform=inch,
                                      boxstyle=f"round,pad=0,rounding_size={R}",
                                      facecolor=CHART_CARD, edgecolor=CHART_CARD_EDGE, lw=0.5))
        return x, y, w, height

    def card_label(x, y_top, w, left, right=None, icon=None):
        tx = x + P
        if icon:
            w, _ = _icon_size(icon, 0.15)
            _draw_icon(fig, icon, tx + w / 2, y_top - P - 0.057, 0.15, muted=True)
            tx += w + 0.07
        text(tx, y_top - P, left, fontsize=8, fontweight="medium", color=CHART_FAINT,
             va="top")
        if right:
            text(x + w - P, y_top - P, right, fontsize=8, fontweight="medium",
                 color=CHART_FAINT, va="top", ha="right")

    # Header: the night, the verdict in plain words, and the plan.
    y = H - M
    head, sub = _headline(s)
    text(M + 0.03, y - 0.02, f"{s.night:%A %-d %B}".upper(), fontsize=9,
         fontweight="medium", color=CHART_SOFT, va="top")
    text(M + 0.02, y - 0.22, head, fontsize=22, fontweight="medium", va="top")
    text(M + 0.03, y - 0.72, sub, fontsize=10, color=CHART_SOFT, va="top")
    y -= HEADER + GAP

    # Hourly verdicts across the dark window: an icon per hour, and under the
    # icons a bar coloured by verdict for the go / no-go at a glance.
    cx, cy, cw, ch = card(y, HOURLY)
    card_label(cx, y, cw, "HOURLY", icon="moon")
    renderer = fig.canvas.get_renderer()
    # An unusable hour lost to fog rather than cloud gets the fog icon.
    icons = ["fog" if _mark(h) == "✗" and h.fog else _mark(h) for h in s.dark_hours]
    lx = cx + cw - P
    for mark, word in reversed(MARK_WORDS):  # the key, right-aligned in the label row
        if mark == "fog" and "fog" not in icons:
            continue
        t = text(lx, y - P, word, fontsize=8, fontweight="medium", color=CHART_FAINT,
                 va="top", ha="right")
        lx -= t.get_window_extent(renderer).width / CHART_DPI + 0.06
        w, _ = _icon_size(mark, 0.16)
        _draw_icon(fig, mark, lx - w / 2, y - P - 0.057, 0.16)
        lx -= w + 0.16
    n = len(s.dark_hours)
    col = (cw - 2 * P) / n
    size = min(0.34, col * 0.62)
    for i, (h, icon) in enumerate(zip(s.dark_hours, icons)):
        x = cx + P + col * (i + 0.5)
        mark = _mark(h)
        text(x, cy + ch - 0.47, f"{h.time:%H}", fontsize=9.5, fontweight="medium",
             ha="center", va="center")
        _draw_icon(fig, icon, x, cy + ch - 0.76, size)
        fig.add_artist(Line2D([x - col * 0.38, x + col * 0.38], [cy + ch - 1.01] * 2,
                              transform=inch, color=MARK_COLOURS[mark], lw=2,
                              solid_capstyle="round"))
        if REQUIRE_SETUP_WINDOW:
            cue = ("set up" if h.time == su_start else "bed" if h.time == su_end else None)
            if cue:
                text(x, cy + ch - 1.08, cue, fontsize=7, color=CHART_SOFT, ha="center",
                     va="top")
    y -= HOURLY + GAP

    # One card per cloud layer, then visibility: both models as smooth curves.
    for i, (key, label, limit, height, (y_lo, y_hi)) in enumerate(cards):
        cx, cy, cw, ch = card(y, height)
        card_label(cx, y, cw, label, icon="mist" if key == "visibility" else "cloud")
        ax = fig.add_axes(((cx + P) / W, (cy + 0.28) / H,
                           (cw - 2 * P - 0.46) / W, (ch - 0.28 - 0.42) / H))
        ax.set_facecolor("none")
        for side in ax.spines.values():
            side.set_visible(False)
        ax.set_xlim(xs[0], xs[-1])
        ax.set_ylim(-2, 104)
        ax.yaxis.tick_right()
        ax.set_yticks([0, 50, 100], [y_lo, "", y_hi])  # midline unlabelled
        ax.tick_params(colors=CHART_FAINT, labelsize=8, length=0, pad=4)
        ax.grid(axis="y", color=CHART_GRID, lw=0.5)
        ax.set_axisbelow(True)
        ticks = [(x, h.time) for x, h in zip(xs, span) if h.time.hour % 3 == 0]
        ax.set_xticks([x for x, _ in ticks], [f"{t:%H:%M}" for _, t in ticks])
        # Twilight either side of the dark window, and the set-up-to-bed window.
        ax.axvspan(xs[0], x_of(s.dark_start), color=CHART_TWILIGHT, lw=0, zorder=0)
        ax.axvspan(x_of(s.dark_end), xs[-1], color=CHART_TWILIGHT, lw=0, zorder=0)
        if REQUIRE_SETUP_WINDOW:
            ax.axvspan(x_of(su_start), x_of(su_end), color=CHART_SETUP_BAND, lw=0, zorder=0)
        ax.axhline(limit, color=CHART_SOFT, lw=0.9, linestyle=(0, (0.1, 2.4)),
                   dash_capstyle="round", zorder=2)

        for _, src, colour in CHART_MODELS:
            pts = [(x, value(h, src, key)) for x, h in zip(xs, span)]
            runs, cur = [], []
            for x, v in pts:  # split at gaps in a model's coverage
                if v is None:
                    if cur:
                        runs.append(cur)
                    cur = []
                else:
                    cur.append((x, float(v)))
            if cur:
                runs.append(cur)
            for run in runs:
                px, py = _monotone([p[0] for p in run], [p[1] for p in run])
                rgb = to_rgb(colour)
                fill = ax.imshow([[(*rgb, 0.0)], [(*rgb, 0.24)]], aspect="auto",
                                 extent=(px[0], px[-1], 0, 100), origin="lower",
                                 interpolation="bicubic", zorder=1)
                clip = Polygon(list(zip(px, py)) + [(px[-1], 0), (px[0], 0)],
                               closed=True, facecolor="none", edgecolor="none")
                ax.add_patch(clip)
                fill.set_clip_path(clip)
                ax.plot(px, py, color=colour, lw=1.5, solid_capstyle="round",
                        solid_joinstyle="round", zorder=3)
        ax.set_xlim(xs[0], xs[-1])
        ax.set_ylim(-2, 104)
        if i == 0:
            # Model key, right-aligned in the first card's label row: a short
            # line in each model's colour, as drawn in the charts.
            lx = cx + cw - P
            for model, _, colour in reversed(CHART_MODELS):
                t = text(lx, y - P, MODEL_NAMES.get(model, model), fontsize=8,
                         fontweight="medium", color=CHART_SOFT, va="top", ha="right")
                lx -= t.get_window_extent(renderer).width / CHART_DPI + 0.07
                fig.add_artist(Line2D([lx - 0.2, lx], [y - P - 0.057] * 2, transform=inch,
                                      color=colour, lw=1.5, solid_capstyle="round"))
                lx -= 0.38
        y -= height + GAP

    foot = "Shaded: twilight · lighter band: set up to bed"
    text(M + 0.03, 0.12, foot, fontsize=7.5, color=CHART_FAINT, va="bottom")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=CHART_DPI)
    plt.close(fig)
    return buf.getvalue()


def _fit(message: str) -> str:
    """Trim to Pushover's length limit at a line break, so no tag is cut."""
    if len(message) <= PUSHOVER_MAX_CHARS:
        return message
    cut = message.rfind("\n", 0, PUSHOVER_MAX_CHARS - 2)
    return message[:cut if cut > 0 else PUSHOVER_MAX_CHARS - 2] + "\n…"


def send_pushover(title: str, message: str, chart: bytes | None = None) -> None:
    message = _fit(message)
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if os.environ.get("DRY_RUN") or not (token and user):
        if not (token and user):
            missing = ", ".join(
                n for n, v in (("PUSHOVER_TOKEN", token), ("PUSHOVER_USER", user)) if not v
            )
            print(f"[dry-run] {missing} not set — printing only.")
        print(f"--- {title} ---\n{message}")
        if chart:
            CHART_FILE.parent.mkdir(parents=True, exist_ok=True)
            CHART_FILE.write_bytes(chart)
            print(f"[dry-run] chart saved to {CHART_FILE}")
        return
    resp = requests.post(
        PUSHOVER_URL,
        data={"token": token, "user": user, "title": title,
              "message": message, "html": 1, "priority": 0},
        files={"attachment": ("skyguard.png", chart, "image/png")} if chart else None,
        timeout=30,
    )
    resp.raise_for_status()
    if resp.json().get("status") != 1:
        raise RuntimeError(f"Pushover rejected the message: {resp.text}")
    print(f"Pushover sent: {title}")


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def slot_due(local_hour: int) -> bool:
    """True when the cron slot that fired lands on `local_hour` local today.
    The workflow fires two UTC slots per run so that one of them is that local
    hour in both summer and winter time; this picks it.
    Uses the SCHEDULED slot (not the clock), so a late-starting run still
    counts. Manual and local runs (no SCHEDULE) always go ahead."""
    fields = os.environ.get("SCHEDULE", "").split()
    if len(fields) < 2 or not (fields[0].isdigit() and fields[1].isdigit()):
        return True
    slot = datetime.now(timezone.utc).replace(
        hour=int(fields[1]), minute=int(fields[0]), second=0, microsecond=0
    )
    return slot.astimezone(TZ).hour == local_hour


def _both_models_cover(hours: list[HourCloud]) -> bool:
    """True when both models have cloud data for every one of these hours."""
    return all(
        h.primary.get("low") is not None and h.secondary.get("low") is not None
        for h in hours
    )


def score_nights(hours: list[HourCloud], count: int) -> list[NightScore]:
    """Score up to `count` nights starting with tonight. With
    REQUIRE_BOTH_MODELS, stop at the first later night that both models don't
    yet fully cover."""
    by_night: dict[date, list[HourCloud]] = {}
    for h in hours:
        if is_dark(h.time):
            by_night.setdefault(night_of(h.time), []).append(h)

    today = hours[0].time.date()
    scores: list[NightScore] = []
    for i in range(count):
        night = today + timedelta(days=i)
        dark_hours = by_night.get(night)
        if not dark_hours:
            continue
        win_start, win_end = exposure_window(night)
        exposure_hours = [h for h in hours if win_start <= h.time < win_end]
        su_start, su_end = setup_window(night)
        setup_hours = [h for h in hours if su_start <= h.time < su_end]
        if (REQUIRE_BOTH_MODELS and i > 0
                and not _both_models_cover(dark_hours + exposure_hours)):
            print(f"[beyond both models] {night}: not scored yet")
            break  # later nights are further out still
        s = score_night(night, dark_hours, exposure_hours, setup_hours)
        if s is not None:
            scores.append(s)
    return scores


def log_scores(scores: list[NightScore]) -> None:
    for s in scores:
        if s.qualifies:
            marker = "GO" if s.confident else "MAYBE"
        elif s.rain_vetoed:
            marker = "VETOED-rain"
        elif s.windy_vetoed:
            marker = "VETOED-wind"
        elif REQUIRE_SETUP_WINDOW and not s.setup_ok:
            marker = "NO-ALIGN-BEFORE-BED" if s.setup_alignable == 0 else "SETUP-CLOUDY"
        else:
            marker = "skip"
        low = s.layer_stats["low"]
        print(
            f"[{marker}] {s.night}: usable confident {s.confident_usable_hours}h "
            f"(run {s.best_run_len}), any {s.any_usable_hours}h "
            f"(run {s.best_run_any_len}), setup align-hrs {s.setup_alignable} "
            f"clear {s.setup_usable_any}h, "
            f"low cloud avg {low[0] if low else '?'}%, "
            f"rain max {s.rain_max_mm:.1f}mm/"
            f"{s.rain_max_prob if s.rain_max_prob is not None else '-'}%"
            f"{f' ({s.rain_unknown_hours}h unknown)' if s.rain_unknown_hours else ''}, "
            f"wind {s.wind_max_kmh}/gust {s.gust_max_kmh}km/h, "
            f"vis min {f'{s.min_visibility_m / 1000:.1f}km' if s.min_visibility_m is not None else '-'}"
            f"{f' mist {s.mist_from:%H:%M}' if s.mist_from else ''}"
            f"{f' fog {s.fog_from:%H:%M}' if s.fog_from else ''}, "
            f"dew spread {f'{s.min_spread_c:.1f}' if s.min_spread_c is not None else '-'}C, "
            f"AOD {f'{s.aod_max:.2f}' if s.aod_max is not None else '-'}, "
            f"jet {s.jet_max_kmh if s.jet_max_kmh is not None else '-'}km/h, "
            f"moon {s.moon_illum}%"
        )


def main() -> int:
    if RUN_MODE not in ("outlook", "tonight"):
        print(f"Unknown RUN_MODE {RUN_MODE!r} — use 'outlook' or 'tonight'.")
        return 1
    run_hour = EVENING_HOUR if RUN_MODE == "tonight" else OUTLOOK_HOUR
    if not slot_due(run_hour):
        print(f"This slot isn't {run_hour}:00 local today (daylight-saving twin) — skipping.")
        return 0

    hours = fetch_forecast()
    if not hours:
        print("No forecast data returned.")
        return 0

    scores = score_nights(hours, 1 if RUN_MODE == "tonight" else NIGHTS_AHEAD)
    log_scores(scores)
    if not scores:
        print("No dark hours to score.")
        return 0

    # Compare each night with the previous run's verdict, then record this one.
    now = datetime.now(TZ)
    state = load_state()
    history = state.setdefault("nights", {})
    changes: dict[date, str | None] = {}
    prev_light: dict[date, str | None] = {}
    for s in scores:
        runs = history.setdefault(s.night.isoformat(), [])
        snap = snapshot(s, now)
        changes[s.night] = describe_change(runs, snap, now)
        prev_light[s.night] = runs[-1]["light"] if runs else None
        runs.append(snap)
        if changes[s.night]:
            print(f"  {s.night}: {changes[s.night]}")
    # A night whose light has dropped since the last run (🟢→🟡, 🟡→🔴, …) is
    # worth a push even when the digest is off — you may have planned around it.
    downgraded = [
        s for s in scores
        if prev_light[s.night] in LIGHT_RANK
        and LIGHT_RANK[prev_light[s.night]] > LIGHT_RANK[s.light]
    ]

    # The push carries one chart: the night its title is about.
    message: tuple[str, str] | None = None
    charted: NightScore | None = None
    if RUN_MODE == "tonight":
        tonight = scores[0]
        if tonight.qualifies or downgraded or DAILY_DIGEST:
            message = build_tonight(tonight, changes[tonight.night], prev_light[tonight.night])
            charted = tonight
    elif any(s.qualifies for s in scores):
        message = build_message(scores, changes)
        charted = (next((s for s in scores if s.confident), None)
                   or next(s for s in scores if s.qualifies))
    elif downgraded or DAILY_DIGEST:
        message = build_digest(scores, changes, downgraded)
        charted = downgraded[0] if downgraded else scores[0]

    if message:
        chart = render_chart(charted, hours) if SEND_CHART and charted else None
        send_pushover(*message, chart)
    else:
        print("No qualifying nights and nothing downgraded — staying silent.")

    if os.environ.get("DRY_RUN"):
        print("[dry-run] forecast history not updated.")
    else:
        save_state(state, now.date())
    return 0


if __name__ == "__main__":
    sys.exit(main())
