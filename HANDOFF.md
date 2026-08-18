# Implementation handoff

Context for picking up work on this repo. Written after the initial build;
read this before changing `scripts/build.py`.

---

## What this is

A static FPL analysis board. `scripts/build.py` fetches data, computes
per-player metrics, and renders `scripts/template.html` into `index.html` with
the dataset embedded as a JSON blob. No backend, no runtime API calls. GitHub
Pages serves `index.html`; a daily Action rebuilds and commits it.

The board exists to answer one question: **which players are mispriced?** FPL
sets prices from raw season totals, so any metric that separates *quality* from
*availability* surfaces value the market has missed. Every column is in service
of that.

---

## ✅ Priority 0 — verified against the live API (2026-08-18)

The build has now run against the real `fantasy.premierleague.com` API
(previously it had only ever executed against a local fixture). Every
assumption in the original P0 table held exactly:

| Assumption | Verified |
|---|---|
| `element.now_cost` is in tenths (95 → £9.5) | ✅ range 40–155, Haaland at 155 = £15.5 |
| `element.element_type` maps 1/2/3/4 → GKP/DEF/MID/FWD | ✅ matches the API's own `element_types` |
| `element.status` uses `a`/`d`/`i`/`s`/`u` | ✅ exactly those five codes, no others |
| `element.chance_of_playing_next_round` is a number or `null` | ✅ int or null, never a string |
| `teams[].code` exists and is stable | ✅ present for all 20 clubs; the silent `id` fallback has been replaced with a loud `SystemExit` |
| `events[].finished` is a bool | ✅ real bool on all 38 events |
| `event/{gw}/live/` returns `elements[].stats.{minutes,total_points}` | ⚠️ **partially** — the endpoint responds, but pre-season it returns an empty `elements` list, so the per-element stats shape is unverifiable until GW1 finishes (deadline 2026-08-21). Re-check after GW1; it only *matters* from GW6 when `CURRENT_SEASON_MIN_GWS` flips the basis. |

A real payload is committed as `tests/bootstrap.json`, and
`tests/test_build.py` codifies each row of that table as a test plus unit
tests for the metric functions — all offline:

```bash
python3 -m unittest discover tests
FPL_FIXTURE=tests/bootstrap.json python3 scripts/build.py   # offline build
```

Also confirmed while verifying: `defensive_contribution`, `expected_goals`
and `expected_assists` all appear in the API's `element_stats` list and on
elements, and the archive `merged_gw.csv` carries `defensive_contribution`,
`starts`, `expected_goals` and `expected_assists` per gameweek — so backlog
items 1 and 2 are implementable exactly as specced.

### ~~Remaining P0: the Action may be IP-blocked~~ ✅ verified working

The workflow ran successfully on GitHub's runners on 2026-08-18 (push
trigger): all three fetches — bootstrap-static, fixtures, and the archive —
succeeded from the datacentre IP, and the run committed its own refresh.
FPL does rate-limit datacentre ranges, so a future block is still possible;
if a scheduled run fails on fetch, the fallback is documented in the README
(run locally, commit), and `fetch()` retries 3× with linear backoff.

---

## Current state

### Working and tested (against the live API and the committed fixture)

- Value ratio, points per 90, consistency (CV of per-GW points, 60+ min games)
- Minutes security computed over the **final 8 gameweeks**, not the season
- Trend arrows (▲▼) where recent share diverges from season share by >15pp
- Transfer detection — compares stable club codes across seasons, marks
  `moved_from`, renders the security badge dashed, excludes from the
  "secure minutes only" filter
- Contested goalkeepers — clubs with 2+ credible keepers, recency-aware
- Manual overrides from `data/overrides.yml`, applied after computation
- Season basis switch: last season's archive before GW6, live API after

### Row schema

Every row in `data/snapshot.json` and the embedded blob:

```
name          str    FPL web_name, e.g. "B.Fernandes"
code          int    stable cross-season FPL player code
team          str    short name, e.g. "MCI"
pos           str    GKP | DEF | MID | FWD
price         float  £m
points        int    total on the active basis
minutes       int    total on the active basis
ratio         float  points / price
p90           float  points / (minutes/90)
consistency   str    Consistent | Balanced | Boom/bust | N/A
cv            float  coefficient of variation, or null
proj_min      int    projected minutes per gameweek
security      str    Nailed | Solid starter | Rotation risk | Bench risk |
                     Contested | Doubt | Injured | Suspended | Unavailable
defcon_rate   float  share of recent starts hitting the DefCon threshold, or null
defcon_avg    float  mean defensive actions per start (recent window), or null
overperf      float  (goals+assists) − (xG+xA) over the recent window, or null
luck          str    "hot" | "unlucky" | ""
fix_avg       float  mean FDR over the next 5 gameweeks, or null
fix_ops       list   upcoming opponents, e.g. ["CHE (A) 4", ...]
trend         str    "rising" | "falling" | ""
season_share  int    % of season minutes
recent_share  int    % of last-8-GW minutes, or null
moved_from    str    previous club short name, or ""
contested     list   rival names (goalkeepers only)
news          str    live injury feed text
overridden    bool   a manual override touched this row
note          str    override note, shown on hover
```

Adding a field means touching `build_rows` (emit), `template.html` (`cols`
array + row template), and this doc.

### Tuning constants

All at the top of `build.py`: `RECENT_WINDOW=8`, `MIN_RECENT_GWS=5`,
`TREND_DELTA=0.15`, `GK_RIVAL_MINUTES=900`, `CURRENT_SEASON_MIN_GWS=6`,
`MIN_MINUTES_FULL_SEASON=450`, `SECURITY_BANDS`, `CONSISTENCY_BANDS`.
These were chosen by eyeballing distributions on one season of data. They are
defensible, not optimal. `RECENT_WINDOW` in particular is worth revisiting once
real in-season data exists — 8 may be too long in a congested run of fixtures.

---

## Backlog

Items 1-3 below were implemented on 2026-08-18 and are kept for the
reasoning; the acceptance checks all passed (Bijol 62%/10.8, Osula 0%/1.9,
23 risers pre-GW1). `defensive_contribution` in `event/{gw}/live/` stats
still needs a re-check once GW1 finishes — the pre-season payload is empty.

### ~~1. DefCon rate column~~ ✅ done (P1 — highest analytical value)

Defensive contribution points are worth ~2 per hit, so a defender converting at
60% banks roughly 46 points across a season. This is currently the single
biggest scoring source the board is blind to.

**Rules (verified against 2026/27 documentation):**
- Defenders: 2 pts at **10+** CBIT (clearances, blocks, interceptions, tackles)
- Midfielders and forwards: 2 pts at **12+** CBIRT (the same four plus ball
  recoveries)
- Capped at 2 per match. Goalkeepers ineligible.

**Data:** both `merged_gw.csv` and the live endpoints expose a
`defensive_contribution` field that is **already the correct position-adjusted
total** — CBIT for defenders, CBIRT for midfielders and forwards. This was
verified exhaustively against the component columns on one season: 9,733/9,733
defender rows and 16,597/16,597 midfielder-and-forward rows matched. Do not
recompute it from components; just compare it to the threshold.

**Emit:** `defcon_rate` (hits ÷ starts over the recency window) and
`defcon_avg` (mean actions per start). Surface `defcon_avg` prominently —
players averaging 8–9.9 are the interesting ones, sitting just under the
defender threshold where a small role change tips them into regular returns.

**Acceptance:** a Leeds defender (Bijol, ~62% hit rate, ~10.8 avg) and a winger
(Osula, ~0%, ~1.9 avg) land at opposite ends. Confirm `defensive_contribution`
is present in `event/{gw}/live/` stats — if it is not, derive from components
and branch on position.

### ~~2. Underlying-vs-actual column~~ ✅ done (P2)

Flags players whose returns were luck. `(goals + assists) − (xG + xA)` over the
recency window. Both sources expose `expected_goals` and `expected_assists`.

**Emit:** `overperf` (float) and a label — over +2.0 is "hot", under −1.0 is
"unlucky", between is neutral. Underperformers are buy candidates; extreme
overperformers are traps. Worked example from last season: one forward returned
5 goals from 2.41 expected in eight gameweeks (+3.59) and looked like the best
riser on the board on points alone.

### ~~3. Risers view~~ ✅ done (P2)

A saved screen rather than a new metric — the combination that found the good
picks. Filter: `trend == "rising"`, `recent_share >= 60`, `security` in
(Nailed, Solid starter), no injury news, `p90` above the positional median.
Sort by `p90`. Once tasks 1 and 2 land, show DefCon rate and overperformance
alongside so sustainability is visible at a glance.

Implement as a fifth tab beside the position tabs.

### ~~4. Fixture difficulty~~ ✅ done (P3)

Was the largest remaining gap. `fixture_outlook()` computes each club's mean
FDR over the next `FIXTURE_HORIZON` (5) gameweeks — windowed by gameweek, so
a double gameweek weighs both matches and a blank contributes nothing. Emits
`fix_avg` and `fix_ops` (opponent list for the hover). A failed fixtures
fetch degrades to an empty column rather than killing the build; offline it
reads `fixtures.json` beside the bootstrap fixture (a real payload is
committed at `tests/fixtures.json`).

### 5. Robustness (P3)

- ~~Override matching by `web_name` + optional `team` only.~~ Done — an
  optional `code:` key (the stable FPL player code) now wins over name
  matching, making entries permanent across transfers and name changes.
  Rows also carry `code` in the emitted schema for this reason.
- ~~**`teams[].code` fallback to `id` is wrong** and will corrupt transfer
  detection. Make it fail loudly instead.~~ Done — missing codes now abort
  the build.
- Consider committing `data/snapshot.json` history to a separate branch —
  it changes daily and will dominate the diff log.

---

## Gotchas discovered during the build

These cost real time. They are not obvious from the data.

1. **Third-party mirrors lag transfers by days.** The mirror used for the first
   prototype still had a player at his old club a week after a £75m move, and
   was missing new signings entirely. Prices, clubs and availability must come
   from the official API. The archive is only safe for *historical* per-gameweek
   numbers, which do not change retroactively.

2. **Minutes security does not transfer with the player.** A full season
   elsewhere proves durability, not that he has won a place in the new squad.
   This is why `moved_from` exists and why those players are excluded from the
   "secure" filter. 42 of 590 rostered players moved in one window.

3. **A season-wide minutes share describes where a player was, not where he
   is.** This was the single biggest source of wrong conclusions in the
   prototype and it broke in both directions: a forward who broke into the side
   in March read as "bench risk" on a 24% season share while holding a 75%
   share across the run-in, and a goalkeeper who *lost* his place read as
   nailed at 82% while sitting at 12% recently. Recency weighting changed the
   assessment for roughly 45% of the board. Any new availability metric must be
   windowed the same way.

4. **A single value ratio conflates quality with availability.** Season points
   ÷ price structurally punishes anyone who missed games. One midfielder ranked
   93rd of 142 on that ratio and 37th on points per 90 — a 56-place gap that
   was entirely injury absence, not quality. This is why both columns exist and
   why the README tells readers to compare them.

5. **`defensive_contribution` is a raw action count, not a points flag.** An
   early analysis read any non-zero value as a threshold hit and produced
   nonsense (forwards hitting DefCon in 6 of 6 starts). Compare to the
   threshold explicitly.

6. **Per-gameweek history must be sorted by gameweek.** CSV row order is not
   chronological. `load_previous_season` sorts explicitly; the recency window
   is meaningless otherwise, and it fails silently rather than erroring.

7. **The build refuses to write an empty board.** A failed fetch leaves the
   last good `index.html` in place rather than publishing a blank page. Keep
   this guard — it fired correctly during development when a fixture had
   mismatched player codes.

---

## Conventions

- Standard library only in `build.py`, except `pyyaml` for overrides (degrades
  gracefully with a warning if absent).
- The template is substituted with `.replace()` on `__DATA__`, `__BASIS__` and
  `__BUILT_AT__` — **not** `.format()` or f-strings, because the file is full
  of JS braces.
- All user-facing strings in the UI go through `esc()`. Player names include
  characters like `Ødegaard` and news text is free-form from the API.
- Comments explain *why*, not *what*. Several constants encode judgement calls
  that are not self-evident; those carry the reasoning inline.
