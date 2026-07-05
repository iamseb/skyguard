# SkyGuard — Clear Night Alert

A passive, once-a-day push that tells you when an upcoming night is worth setting
up for. No dashboard to check — silent unless a night clears the threshold, or
turn on `DAILY_DIGEST` for a short status **every** day (so you also know it's
running, and why nothing's on when it isn't).

It pulls the next few nights of cloud forecast for your configured location from
[Open-Meteo](https://open-meteo.com/), evaluates **only the hours of real
darkness** (nautical night), judges the sky **by cloud layer**, applies rain and
wind vetoes, and sends a single [Pushover](https://pushover.net/) notification if
a night looks usable.

## How it decides

- **Darkness**: an hour counts only if the sun is below −12° (nautical night),
  computed per hour. At high latitudes a midsummer night gives only a couple of
  dark hours, so the tool is naturally quiet around the solstice and more active
  as nights lengthen.
- **Cloud, by layer**: rather than one lumped "total cloud" number, an hour is
  **usable** when *low* cloud ≤ `LOW_CLOUD_MAX` (default 20%) and *mid* cloud ≤
  `MID_CLOUD_MAX` (default 50%). Low cloud is the real gate — it's opaque. *High*
  cloud (cirrus) is thin and partly transparent, so it is **not** gated by
  default (set `HIGH_CLOUD_MAX` to gate on it); it's reported, and flagged as
  "thin high cloud" when notable. This avoids the over-pessimism of tools that
  gate on total cloud, which inflates with harmless cirrus.
- **Rain is a hard veto**: rain ruins the gear, not just the session. A night is
  rejected outright if any hour in the **equipment-exposure window** (the gear is
  outside, default 21:00–09:00 — wider than the dark hours) has forecast rain
  above `PRECIP_AMOUNT_MAX` mm **or** a rain chance ≥ `PRECIP_PROB_MAX` %, in
  *either* model. (Probability is noisy — often a few % on dry nights, and some
  models provide none — so the % cap sits well above zero while the mm test
  catches actually-forecast rain.)
- **Wind (gusts are the spoiler)**: gusts shake the scope and trail subs far more
  than steady wind, so SkyGuard gates on gusts. Over the exposure window (worst
  value in either model), sustained wind above `WIND_SUSTAINED_WARN_KMH` (default
  25 km/h) or gusts above `WIND_GUST_WARN_KMH` (default 40 km/h) add a **⚠ breezy**
  flag to the push; gusts above `WIND_GUST_VETO_KMH` (default 55 km/h) **reject**
  the night. Values are km/h (Open-Meteo's default unit). The forecast is
  open-terrain 10 m wind, so a sheltered site is usually calmer — the defaults are
  deliberately generous. Set the veto very high to disable it.
- **Setup window (must be able to deploy + polar align before bed)**: you have to
  physically set up and polar align at the *start* of a session, before bed — so a
  night that only clears after you're asleep is useless. With `REQUIRE_SETUP_WINDOW`
  on (default), a night must have at least `SETUP_MIN_CLEAR_HOURS` hour(s) in the
  pre-bed **setup window** (`SETUP_WINDOW_START_HOUR`–`SETUP_WINDOW_END_HOUR`,
  default 21:00–23:00) that are **both clear and dark enough to polar align** — the
  sun at least `ALIGN_DEPRESSION`° below the horizon, so stars are
  visible/plate-solvable. At high latitudes in midsummer the *darkness* test is
  what fails: it simply isn't dark enough to align before bedtime, so SkyGuard
  stays silent (correctly — there's no point deploying). Once a **permanent pier**
  holds the mount aligned, set `REQUIRE_SETUP_WINDOW = False` — nightly alignment is
  then unnecessary and only the dark hours matter.
- **Confidence**: *high* when both models agree the hour is usable; *tentative*
  when only one model is available (a high-resolution model's horizon may be
  ~48h) or they disagree — tentative nights are flagged in the message.
- **Qualifies**: at least `MIN_USABLE_HOURS` usable hours **and** a continuous
  run of at least `MIN_CONTINUOUS_RUN` hours.
- **Moon**: reported (illumination % + phase) but not gated on — useful for
  narrowband imaging, which tolerates moonlight.
- **Daily digest (optional)**: by default SkyGuard is silent unless a night
  qualifies. Set `DAILY_DIGEST` and it sends a short status on **every** run — the
  clear-night alert when a night qualifies, otherwise a "nothing tonight, here's the
  outlook (and why)" summary with a one-line reason per night (🟢 usable / 🌧 rain /
  💨 too windy / 🌆 not dark enough to align before bed / ☁ too cloudy). Handy as a
  daily heartbeat that confirms the job is running.
- **Traffic-light title**: every notification's title leads with a light you can
  read from the lock screen — **🟢** a night qualifies (go), **🟡** the sky's clear
  but only the align-before-bed/darkness gate blocks it (weather's on your side,
  the season/timing isn't), **🔴** genuinely clouded / rained / blown out.

The push explains itself: per night it shows the dark window, how many hours are
usable, the best continuous run, the moon, the per-layer cloud (average + peak),
the wind (sustained + peak gust), and — for short nights — an hour-by-hour line
(`✓` both models agree usable, `~` only one does, `✗` not usable).

## Configure

**Location** — set as environment variables (recommended) or edit the defaults
in the `CONFIG` block of `main.py`:

- `LATITUDE`, `LONGITUDE` — decimal degrees
- `TIMEZONE` — IANA name, e.g. `Europe/London`
- `PRIMARY_MODEL`, `SECONDARY_MODEL` — Open-Meteo model ids (optional; the
  defaults suit the Netherlands — pick regional models from Open-Meteo's list)

**Thresholds** — `LOW_CLOUD_MAX`, `MID_CLOUD_MAX`, `HIGH_CLOUD_MAX`,
`MIN_USABLE_HOURS`, `MIN_CONTINUOUS_RUN`, `TWILIGHT_DEPRESSION`, `NIGHTS_AHEAD`,
`RESPECT_CURFEW` — all in the `CONFIG` block.

**Rain veto** — `EXPOSURE_START_HOUR` / `EXPOSURE_END_HOUR` (when the gear is
out), `PRECIP_AMOUNT_MAX` (mm), `PRECIP_PROB_MAX` (%).

**Setup window** — `REQUIRE_SETUP_WINDOW` (on/off), `SETUP_WINDOW_START_HOUR` /
`SETUP_WINDOW_END_HOUR` (your pre-bed deploy + align window), `SETUP_MIN_CLEAR_HOURS`,
`ALIGN_DEPRESSION` (how far below the horizon the sun must be to polar align).
`SETUP_WINDOW_END_HOUR_BY_WEEKDAY` overrides bedtime per weekday of the evening
(Mon=0 … Sun=6) for nights you stay up later — the default runs Fri/Sat nights to
01:00 (hours ≥24 mean past midnight, so 25 = 01:00). Empty the dict for a uniform
bedtime every night.

**Wind** — `WIND_SUSTAINED_WARN_KMH` / `WIND_GUST_WARN_KMH` (km/h; above these a
night is flagged **⚠ breezy** but still qualifies), `WIND_GUST_VETO_KMH` (gusts
above this reject the night; set very high to disable the veto).

**Daily digest** — `DAILY_DIGEST` (environment variable / Actions Variable): `1`
(or `true`/`yes`/`on`) to send a status every run; unset or `0` to stay silent
unless a night qualifies.

## Setup (GitHub Action)

1. **Pushover**: create an [application/API token](https://pushover.net/apps/build)
   and note your user key.
2. **Secrets** (repo → Settings → Secrets and variables → Actions → Secrets):
   - `PUSHOVER_TOKEN` — your application API token
   - `PUSHOVER_USER` — your user key
   - `LATITUDE`, `LONGITUDE`, `TIMEZONE` — your location. Stored as Secrets
     (encrypted, not publicly visible) so coordinates aren't exposed on a public
     repo. On a private repo you could use Variables instead.
3. **Variables** (optional; same page → *Variables* tab):
   - `DAILY_DIGEST` = `1` — send a short status every day (not just on clear
     nights), so you always know the job ran. Leave unset to stay silent unless a
     night qualifies. It's a Variable, not a Secret, because it isn't sensitive.
4. The workflow in `.github/workflows/clear-night-alert.yml` runs daily at 06:00
   UTC. Edit the `cron` line to change the time (cron is always UTC).

## Test it

Run a manual check without sending a push:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
LATITUDE=51.48 LONGITUDE=-0.0 TIMEZONE=Europe/London \
  DRY_RUN=1 .venv/bin/python main.py
```

It prints the per-night scores and the message it *would* send. Add `DAILY_DIGEST=1`
to preview the daily-digest message even when no night qualifies. You can also
trigger the workflow manually from the Actions tab ("Run workflow") with the
dry-run box ticked.

## Tuning notes

- Quiet all summer? Lower `MIN_USABLE_HOURS` / `MIN_CONTINUOUS_RUN`, or raise
  `LOW_CLOUD_MAX`.
- Stricter about haze? Set `HIGH_CLOUD_MAX` (e.g. 60) to also reject hours with
  heavy cirrus. Leave it `None` to tolerate high cloud (best for narrowband).
- Want astronomical darkness outside summer? Set `TWILIGHT_DEPRESSION = 18`.
- Want to stop counting dark hours after a certain time? Set `RESPECT_CURFEW =
  True` and `CURFEW_HOUR`. (Off by default — all dark hours are reported.)
- Windy site costing you subs? Lower `WIND_GUST_WARN_KMH` / `WIND_GUST_VETO_KMH`.
  Very sheltered? Raise them — the forecast is open-terrain 10 m wind.
- Want a daily heartbeat even when nothing's on? Set `DAILY_DIGEST = 1` (or the
  `DAILY_DIGEST` Actions Variable). Great for confirming the schedule is alive.
