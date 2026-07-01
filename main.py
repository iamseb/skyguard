#!/usr/bin/env python3
"""SkyGuard — clear-night forecast alert.

Runs once each morning, pulls the next few nights of cloud forecast for a
configured location from Open-Meteo (one primary model, one cross-check),
evaluates only the hours of real darkness (nautical night), and sends a single
Pushover push if a night looks usable. Silent otherwise.

Cloud is judged by LAYER, not as one lumped total. Low cloud is the real gate
for astrophotography — it's opaque. Mid cloud also blocks. High cloud (cirrus)
is thin and partly transparent, so by default it is reported but does NOT reject
an hour (narrowband tolerates it). This avoids the over-pessimism of tools that
gate on total cloud cover, which inflates with harmless high cirrus.

Set your location with the LATITUDE / LONGITUDE / TIMEZONE environment variables
(or edit the defaults below). Tune behaviour in the CONFIG block. Set DRY_RUN=1
to print the message to stdout instead of pushing.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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


# Observing site. Set via environment (recommended, so your location is not
# committed) or change the defaults. Defaults to the Royal Observatory,
# Greenwich.
LATITUDE = float(_env("LATITUDE", "51.4779"))
LONGITUDE = float(_env("LONGITUDE", "-0.0015"))
TIMEZONE = _env("TIMEZONE", "Europe/London")

# Darkness definition. 12 = nautical night. Use 18 for astronomical night.
# At high latitudes nautical night can be the only real darkness in midsummer.
TWILIGHT_DEPRESSION = 12.0

# How many upcoming nights to evaluate (tonight = night 1).
NIGHTS_AHEAD = 3

# Per-layer cloud thresholds. An hour is "usable" when low and mid cloud are at
# or below their caps. Low cloud is the dominant gate. High cloud is NOT gated
# by default (set HIGH_CLOUD_MAX to a number to gate on it too) but is reported,
# and flagged as "thin high cloud" in the push when its average is notable.
LOW_CLOUD_MAX = 20
MID_CLOUD_MAX = 50
HIGH_CLOUD_MAX: int | None = None
HIGH_CLOUD_NOTE = 40  # average high cloud above this gets a note in the push

# Precipitation veto. Rain on the kit is a hard no, so the night is rejected if
# ANY hour in the equipment-exposure window has forecast rain above
# PRECIP_AMOUNT_MAX mm OR a rain probability at/above PRECIP_PROB_MAX %, in
# EITHER model. The window is the hours the gear is physically outside — wider
# than the dark hours. NB: forecast probability is noisy (often a few % even on
# bone-dry nights, and some models give no probability at all), which is why the
# probability cap is well above zero; the mm test catches actually-forecast rain.
EXPOSURE_START_HOUR = 21   # gear goes out
EXPOSURE_END_HOUR = 9      # gear comes in (next morning)
PRECIP_AMOUNT_MAX = 0.0    # mm; any forecast rain above this vetoes the night
PRECIP_PROB_MAX = 30       # %; rain chance at/above this vetoes the night

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

# Forecast models. Primary first; high confidence requires both to agree. The
# default primary (KNMI HARMONIE) is a high-resolution model for the Netherlands;
# change it for other regions (see Open-Meteo's model list).
PRIMARY_MODEL = _env("PRIMARY_MODEL", "knmi_harmonie_arome_netherlands")
SECONDARY_MODEL = _env("SECONDARY_MODEL", "ecmwf_ifs025")

# Pull a little extra so the last night's dawn isn't truncated.
FORECAST_DAYS = NIGHTS_AHEAD + 1

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

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

    @property
    def max_precip_mm(self) -> float:
        vals = [v for v in self.precip_mm.values() if v is not None]
        return max(vals) if vals else 0.0

    @property
    def max_precip_prob(self) -> int | None:
        vals = [v for v in self.precip_prob.values() if v is not None]
        return max(vals) if vals else None

    @property
    def wet(self) -> bool:
        """True if either model forecasts rain above the amount or probability cap."""
        if self.max_precip_mm > PRECIP_AMOUNT_MAX:
            return True
        prob = self.max_precip_prob
        return prob is not None and prob >= PRECIP_PROB_MAX

    @staticmethod
    def _usable(layers: dict[str, int | None]) -> bool | None:
        """True/False if the model has data for this hour, else None.

        Gates on low and mid cloud; high cloud only if HIGH_CLOUD_MAX is set.
        """
        if all(layers.get(k) is None for k in LAYERS):
            return None
        low, mid, high = layers.get("low"), layers.get("mid"), layers.get("high")
        if low is not None and low > LOW_CLOUD_MAX:
            return False
        if mid is not None and mid > MID_CLOUD_MAX:
            return False
        if HIGH_CLOUD_MAX is not None and high is not None and high > HIGH_CLOUD_MAX:
            return False
        return True

    @property
    def usable_confident(self) -> bool:
        """Both models present and both say usable."""
        return self._usable(self.primary) is True and self._usable(self.secondary) is True

    @property
    def usable_any(self) -> bool:
        return self._usable(self.primary) is True or self._usable(self.secondary) is True

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
    exposure_window: str
    setup_window: str
    setup_usable_confident: int
    setup_usable_any: int
    setup_ok: bool
    setup_alignable: int          # window hours dark enough to align
    setup_align_from: datetime | None  # earliest clear + dark-enough moment

    @property
    def qualifies(self) -> bool:
        if self.rain_vetoed:
            return False
        if REQUIRE_SETUP_WINDOW and not self.setup_ok:
            return False  # can't deploy + polar align before bed → not actionable
        if (
            self.confident_usable_hours >= MIN_USABLE_HOURS
            and self.best_run_len >= MIN_CONTINUOUS_RUN
        ):
            return True
        return (
            self.any_usable_hours >= MIN_USABLE_HOURS
            and self.best_run_any_len >= MIN_CONTINUOUS_RUN
        )


# --------------------------------------------------------------------------- #
# Forecast fetch                                                               #
# --------------------------------------------------------------------------- #

def fetch_forecast() -> list[HourCloud]:
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
                  "precipitation,precipitation_probability",
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

    out: list[HourCloud] = []
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t).replace(tzinfo=TZ)
        out.append(HourCloud(
            time=dt,
            primary={l: primary_cloud[l][i] for l in LAYERS},
            secondary={l: secondary_cloud[l][i] for l in LAYERS},
            precip_mm={k: precip_mm[k][i] for k in precip_mm},
            precip_prob={k: precip_prob[k][i] for k in precip_prob},
        ))
    return out


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


def exposure_window(night: date) -> tuple[datetime, datetime]:
    """The hours the gear is physically outside: EXPOSURE_START_HOUR on the
    evening of `night` to EXPOSURE_END_HOUR the next morning."""
    start = datetime.combine(night, time(EXPOSURE_START_HOUR), tzinfo=TZ)
    end = datetime.combine(night + timedelta(days=1), time(EXPOSURE_END_HOUR), tzinfo=TZ)
    return start, end


def setup_window(night: date) -> tuple[datetime, datetime]:
    """The pre-bed window to set up and polar align, on the evening of `night`."""
    start = datetime.combine(night, time(SETUP_WINDOW_START_HOUR), tzinfo=TZ)
    end = datetime.combine(night, time(SETUP_WINDOW_END_HOUR), tzinfo=TZ)
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

    # Precipitation veto across the whole equipment-exposure window.
    rain_vetoed = any(h.wet for h in exposure_hours)
    rain_max_mm = max((h.max_precip_mm for h in exposure_hours), default=0.0)
    probs = [h.max_precip_prob for h in exposure_hours if h.max_precip_prob is not None]
    rain_max_prob = max(probs) if probs else None
    win_start, win_end = exposure_window(night)

    # Setup / polar-align window (pre-bed): need hours that are BOTH dark enough
    # to align AND clear, before bedtime. In midsummer the darkness test is what
    # makes a night fail — it's simply not dark enough to align before bed.
    alignable_setup = [h for h in setup_hours if is_alignable(h.time)]
    setup_conf, setup_any = _usable_counts(alignable_setup)
    setup_ok = setup_conf >= SETUP_MIN_CLEAR_HOURS or setup_any >= SETUP_MIN_CLEAR_HOURS
    clear_align_times = [h.time for h in alignable_setup if h.usable_any]
    setup_align_from = min(clear_align_times) if clear_align_times else None
    su_start, su_end = setup_window(night)

    times = [h.time for h in dark_hours]
    confident_flags = [h.usable_confident for h in dark_hours]
    any_flags = [h.usable_any for h in dark_hours]

    best_run_len, best_run_start = _longest_run(confident_flags, times)
    best_run_any_len, best_run_any_start = _longest_run(any_flags, times)

    def stats(layer: str) -> tuple[int, int] | None:
        vals = [h.layer(layer) for h in dark_hours if h.layer(layer) is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals)), max(vals)

    confident = sum(confident_flags)
    illum, label = moon_for(night)
    tentative = not (confident >= MIN_USABLE_HOURS and best_run_len >= MIN_CONTINUOUS_RUN)

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
        exposure_window=f"{win_start:%H:%M}–{win_end:%H:%M}",
        setup_window=f"{su_start:%H:%M}–{su_end:%H:%M}",
        setup_usable_confident=setup_conf,
        setup_usable_any=setup_any,
        setup_ok=setup_ok,
        setup_alignable=len(alignable_setup),
        setup_align_from=setup_align_from,
    )


# --------------------------------------------------------------------------- #
# Message + delivery                                                           #
# --------------------------------------------------------------------------- #

def _fmt_layer(stat: tuple[int, int] | None) -> str:
    if stat is None:
        return "?"
    avg, peak = stat
    return f"{avg}%" if avg == peak else f"{avg}% (peak {peak}%)"


def format_night(s: NightScore) -> str:
    day = s.night.strftime("%a %-d %b")
    window = f"{s.dark_start:%H:%M}–{s.dark_end:%H:%M}"
    total = len(s.dark_hours)

    if s.tentative:
        usable, run_len, run_start = s.any_usable_hours, s.best_run_any_len, s.best_run_any_start
        conf = "tentative (single model / models disagree)"
    else:
        usable, run_len, run_start = s.confident_usable_hours, s.best_run_len, s.best_run_start
        conf = "high (both models agree)"
    run_from = run_start.strftime("%H:%M") if run_start else "?"

    lines = [
        f"<b>{day}</b> · dark {window} · Moon {s.moon_illum}% ({s.moon_label})",
        f"{usable}/{total} h usable · best run {run_len}h from {run_from} · conf: {conf}",
    ]

    if REQUIRE_SETUP_WINDOW:
        frm = s.setup_align_from.strftime("%H:%M") if s.setup_align_from else "?"
        setup_tag = "" if s.setup_usable_confident >= SETUP_MIN_CLEAR_HOURS else " (tentative)"
        lines.append(f"✅ set up &amp; align from {frm} — dark + clear before bed{setup_tag}")

    cloud = (
        f"low {_fmt_layer(s.layer_stats['low'])} · "
        f"mid {_fmt_layer(s.layer_stats['mid'])} · "
        f"high {_fmt_layer(s.layer_stats['high'])}"
    )
    high = s.layer_stats["high"]
    if high is not None and high[0] >= HIGH_CLOUD_NOTE:
        cloud += " — thin high cloud (ok for narrowband)"
    lines.append(cloud)

    prob = f"{s.rain_max_prob}%" if s.rain_max_prob is not None else "n/a"
    lines.append(
        f"rain {s.exposure_window}: dry (max chance {prob}, max {s.rain_max_mm:.1f}mm)"
    )

    if total <= MAX_HOURLY_DETAIL:
        for h in s.dark_hours:
            mark = "✓" if h.usable_confident else ("~" if h.usable_any else "✗")
            lines.append(
                f"{h.time:%H:%M} · low {h.layer('low')}% "
                f"mid {h.layer('mid')}% high {h.layer('high')}% {mark}"
            )

    return "\n".join(lines)


def build_message(qualifying: list[NightScore]) -> tuple[str, str]:
    if len(qualifying) == 1:
        title = f"Clear night: {qualifying[0].night.strftime('%a %-d %b')}"
    else:
        title = f"{len(qualifying)} clear nights ahead"
    body = "\n\n".join(format_night(s) for s in qualifying)
    return title, body


def send_pushover(title: str, message: str) -> None:
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if os.environ.get("DRY_RUN") or not (token and user):
        if not (token and user):
            missing = ", ".join(
                n for n, v in (("PUSHOVER_TOKEN", token), ("PUSHOVER_USER", user)) if not v
            )
            print(f"[dry-run] {missing} not set — printing only.")
        print(f"--- {title} ---\n{message}")
        return
    resp = requests.post(
        PUSHOVER_URL,
        data={"token": token, "user": user, "title": title,
              "message": message, "html": 1, "priority": 0},
        timeout=30,
    )
    resp.raise_for_status()
    if resp.json().get("status") != 1:
        raise RuntimeError(f"Pushover rejected the message: {resp.text}")
    print(f"Pushover sent: {title}")


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def main() -> int:
    hours = fetch_forecast()
    if not hours:
        print("No forecast data returned.")
        return 0

    by_night: dict[date, list[HourCloud]] = {}
    for h in hours:
        if is_dark(h.time):
            by_night.setdefault(night_of(h.time), []).append(h)

    today = hours[0].time.date()
    scores: list[NightScore] = []
    for i in range(NIGHTS_AHEAD):
        night = today + timedelta(days=i)
        dark_hours = by_night.get(night)
        if not dark_hours:
            continue
        win_start, win_end = exposure_window(night)
        exposure_hours = [h for h in hours if win_start <= h.time < win_end]
        su_start, su_end = setup_window(night)
        setup_hours = [h for h in hours if su_start <= h.time < su_end]
        s = score_night(night, dark_hours, exposure_hours, setup_hours)
        if s is not None:
            scores.append(s)

    for s in scores:
        if s.qualifies:
            marker = "QUALIFIES"
        elif s.rain_vetoed:
            marker = "VETOED-rain"
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
            f"{s.rain_max_prob if s.rain_max_prob is not None else '-'}%, "
            f"moon {s.moon_illum}%"
        )

    qualifying = [s for s in scores if s.qualifies]
    if not qualifying:
        print("No qualifying nights — staying silent.")
        return 0

    title, message = build_message(qualifying)
    send_pushover(title, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
