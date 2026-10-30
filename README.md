# SkyGuard — Clear Night Alert

A passive push that tells you when an upcoming night is worth setting up for —
a midday **outlook** for tonight and tomorrow, then an **evening update** at 18:00
that re-checks just tonight with the freshest model runs. No dashboard to check — silent unless a night clears the threshold, or
turn on `DAILY_DIGEST` for a short status **every** day (so you also know it's
running, and why nothing's on when it isn't).

It pulls the coming nights' cloud forecast for your configured location from
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
  cloud (cirrus) is thin and partly transparent, so it doesn't stop a night
  qualifying (set `HIGH_CLOUD_MAX` to make it) — but it does decide how
  *confident* the verdict is (see **Confidence tiers**). This avoids the
  over-pessimism of tools that gate on total cloud, which inflates with harmless
  cirrus.
- **Rain is a hard veto, with zero tolerance**: rain ruins the gear, not just the
  session. A night is rejected outright if any hour in the **equipment-exposure
  window** (the gear is outside, default 18:00–09:00 — wider than the dark hours)
  has forecast rain above `PRECIP_AMOUNT_MAX` mm (default 0.0) **or** a rain
  chance above `PRECIP_PROB_MAX` % (default 0), in *either* model. It fails safe:
  an hour with no rain amount or no rain probability from any model counts as
  wet. Not every model publishes a probability (KNMI HARMONIE doesn't; ECMWF
  does), so at least one configured model must, or every night is vetoed.
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
- **Only nights both models cover**: the high-resolution primary model reaches
  only ~2½ days ahead. Beyond that a verdict would rest on the global model alone
  and can swing a lot from one run to the next, so with `REQUIRE_BOTH_MODELS` on
  (default) a later night is scored only once both models cover its whole dark
  and equipment-exposure windows. In practice: tonight + tomorrow at midday,
  tonight in the evening. Tonight is always scored, so if one model is down you
  get a tentative verdict rather than silence. `NIGHTS_AHEAD` is the upper limit.
- **Confidence tiers**: a night that passes every rule is **🟢 confident** only
  if both models *agree* on a usable run of at least `CONFIDENT_MIN_RUN` hours
  (default 2) with high cloud at or below `HIGH_CLOUD_CONFIDENT_MAX` (default 60%)
  in each — and, with the setup gate on, the setup window passes the same test.
  Otherwise it's **🟡 speculative**, with the reason given (models disagree /
  heavy high cloud). Why high cloud matters here even for narrowband: thin cirrus
  is fine to image through, but a forecast of *heavy* high cloud can't tell thin
  cirrus from a thick frontal sheet, and those nights mostly fail. Checked against
  a year of hourly observed cloud and rain from a national weather station near
  the test site (align-before-bed gate on): 🟢 nights gave a real session (≥3h
  continuous clear, dry) **65%** of the time, 🟡 nights only **9%**, versus 36%
  for all qualifying nights together.
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
- **Evening update (tonight)**: forecasts sharpen as the night gets closer — the
  high-resolution model re-runs hourly, so by 18:00 it is ~6 hours fresher than
  the midday outlook. The evening run re-scores **tonight only** and gives a
  go / no-go: "🟢 Tonight's on — set up from 21:00", "🔴 Tonight's off — rain
  forecast (was 🟢)", with an hour-by-hour strip for the whole dark window
  (`21✓ 22◐ 23~ 00✗ …`). It follows the same rules as the midday run: it pushes
  when tonight is 🟢 or 🟡, when tonight's light has dropped since the last run,
  or every time if `DAILY_DIGEST` is on.
- **Forecast history (what changed)**: every run records a compact verdict per
  night (light, usable hours, low/mid cloud, rain, gusts), and the next run
  compares against it. Each night in the push then carries a line such as
  `↻ 🔴🟡→🟢 since yesterday evening · usable 1→6h · low 60→15%`, or
  `↻ 🟢🟢→🟢 unchanged since midday` — a forecast that holds steady across
  several runs is one you can trust more. Small wobbles are ignored
  (`CHANGE_HOURS`, `CHANGE_CLOUD_PTS`, `CHANGE_GUST_KMH`). A night whose light has
  dropped (🟢→🟡, 🟡→🔴, …) **always** triggers a push, even with the digest off, so
  you're never left planning around a stale forecast.
- **Traffic-light title**: every notification's title leads with a light you can
  read from the lock screen — **🟢** a confident go, **🟡** a speculative maybe,
  **🔴** off (cloud, rain, wind, or no clear + dark hour to align before bed).

The push explains itself: per night it shows the dark window, how many hours are
usable, the best continuous run, the moon, the per-layer cloud (average + peak),
the wind (sustained + peak gust), and — for short nights — an hour-by-hour line
(`✓` both models agree usable with modest high cloud, `◐` both agree but under
heavy high cloud, `~` only one model says usable, `✗` not usable).

**Cloud chart.** Each push also carries an image of the night's forecast, so you
can see *when* the cloud comes and goes rather than reconcile whole-night
averages. It opens with the verdict in plain words ("Clear 19:00–23:00"), then
an icon per dark hour (moon and stars = clear, moon over haze = heavy cirrus,
moon behind a cloud = the models disagree, cloud = not usable) above a bar in
the verdict's colour, then one card per layer — low, mid, high — with both
models drawn as curves against that layer's limit. Twilight is shaded, and the
set-up-to-bed window is lightly banded. The outlook charts the night its title
is about. The chart needs matplotlib and uses the bundled Inter font
(`fonts/`, SIL Open Font License); if it can't be drawn, the push goes out as
text only.

## Configure

**Location** — set as environment variables (recommended) or edit the defaults
in the `CONFIG` block of `main.py`:

- `LATITUDE`, `LONGITUDE` — decimal degrees
- `TIMEZONE` — IANA name, e.g. `Europe/London`
- `PRIMARY_MODEL`, `SECONDARY_MODEL` — Open-Meteo model ids (optional; the
  defaults suit the Netherlands — pick regional models from Open-Meteo's list)

**Thresholds** — `LOW_CLOUD_MAX`, `MID_CLOUD_MAX`, `HIGH_CLOUD_MAX`,
`CONFIDENT_MIN_RUN`, `HIGH_CLOUD_CONFIDENT_MAX`, `MIN_USABLE_HOURS`, `MIN_CONTINUOUS_RUN`, `TWILIGHT_DEPRESSION`, `NIGHTS_AHEAD`, `REQUIRE_BOTH_MODELS`,
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
unless a night qualifies (or a 🟢 night drops).

**Run mode** — `RUN_MODE` (environment): `outlook` (default; the next
nights both models cover, up to `NIGHTS_AHEAD`) or `tonight` (the evening update).
`OUTLOOK_HOUR` and `EVENING_HOUR` (in `CONFIG`, defaults 12 and 18) are the local
hours the two runs are meant for.

**History** — `STATE_FILE` (environment; default `.skyguard/state.json`) is where
the per-night verdicts are kept between runs. Pruned automatically to the last
`STATE_KEEP_RUNS` runs of nights that haven't happened yet.

**Chart** — `SEND_CHART` (environment): `0` to send text-only pushes (default on).
`CHART_FILE` (environment; default `.skyguard/chart.png`) is where a dry run
saves the chart instead of sending it.

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
4. The workflow in `.github/workflows/clear-night-alert.yml` runs the **outlook**
   at 12:00 local and the **evening update** at 18:00 local. Cron is always UTC
   and ignores daylight saving, so each run has *two* cron lines (10:00/11:00 UTC
   for the outlook, 16:00/17:00 UTC for the evening — the local hour in Central
   European summer and winter time); the script runs whichever one lands on
   `OUTLOOK_HOUR` / `EVENING_HOUR` local that day and skips the other. For another
   timezone or hour, set each pair to the UTC hours that are that local hour in
   summer and in winter.
5. **History** is carried between runs in the GitHub Actions cache (restored at the
   start of each run, saved at the end) — nothing is committed to the repo. If the
   cache is ever lost, the next run simply has nothing to compare against and
   starts a fresh history.

## Test it

Run a manual check without sending a push:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
LATITUDE=51.48 LONGITUDE=-0.0 TIMEZONE=Europe/London \
  DRY_RUN=1 .venv/bin/python main.py
```

It prints the per-night scores and the message it *would* send, and saves the
chart to `.skyguard/chart.png`. Add `DAILY_DIGEST=1`
to preview the daily-digest message even when no night qualifies, and
`RUN_MODE=tonight` to preview the evening update. A dry run leaves the history
untouched; run without `DRY_RUN` (and without Pushover credentials, so it only
prints) to record a run and see the `↻` change lines on the next one. You can
also trigger the workflow manually from the Actions tab ("Run workflow"), picking
the mode and ticking the dry-run box.

## Tuning notes

- Quiet all summer? Lower `MIN_USABLE_HOURS` / `MIN_CONTINUOUS_RUN`, or raise
  `LOW_CLOUD_MAX`.
- Too many 🟡 maybes? Set `HIGH_CLOUD_MAX` (e.g. 60) to reject heavy-high-cloud
  hours outright, so those nights go 🔴 instead. Want more 🟢 nights at a lower hit
  rate? Raise `HIGH_CLOUD_CONFIDENT_MAX` or lower `CONFIDENT_MIN_RUN`. Tweaking
  `LOW_CLOUD_MAX` / `MID_CLOUD_MAX` changed the hit rate very little in testing —
  model agreement and high cloud are the levers that matter.
- Want astronomical darkness outside summer? Set `TWILIGHT_DEPRESSION = 18`.
- Want to stop counting dark hours after a certain time? Set `RESPECT_CURFEW =
  True` and `CURFEW_HOUR`. (Off by default — all dark hours are reported.)
- Windy site costing you subs? Lower `WIND_GUST_WARN_KMH` / `WIND_GUST_VETO_KMH`.
  Very sheltered? Raise them — the forecast is open-terrain 10 m wind.
- Want a daily heartbeat even when nothing's on? Set `DAILY_DIGEST = 1` (or the
  `DAILY_DIGEST` Actions Variable). Great for confirming the schedule is alive.
