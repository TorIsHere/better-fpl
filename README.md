# better-fpl

A Fantasy Premier League transfer value board that rebuilds itself daily from
the official FPL API. Static site, no backend — everything is baked into
`index.html` at build time.

**Live at:** https://torishere.github.io/better-fpl/

## What it shows

| Column | What it measures |
|---|---|
| **Pts / £m** | Season points ÷ current price. Good for spotting value, but it punishes anyone who missed games regardless of how well they played. |
| **Pts / 90** | Points per 90 minutes played. Measures quality *independent of availability*. |
| **Consistency** | Spread of returns week to week, from the coefficient of variation across games of 60+ minutes. Consistent / Balanced / Boom-bust. |
| **Proj min** | Expected minutes per gameweek, from minutes share, discounted by any current injury doubt. |
| **Security** | Judged on the **final 8 gameweeks**, not the season as a whole. Nailed (80%+ of recent available minutes) · Solid (60-80%) · Rotation risk (35-60%) · Bench risk (under 35%) · Contested, or the live injury status. |
| **DefCon** | Mean defensive actions per start over the recent window, with the share of starts hitting the 2-point threshold (10+ for defenders, 12+ for midfielders/forwards). Gold highlights players averaging just under their threshold — one role tweak from regular returns. Worth ~2 pts a hit, this is the biggest scoring source raw price ignores. |
| **Luck** | Goal involvements minus expected (xG + xA) over the recent window. Above +2 the player is running hot and due regression; below −1 he is finishing normally but unrewarded — the buy-low signal. |
| **Fix (5)** | Average fixture difficulty (2 easiest → 5 hardest) over the club's next five gameweeks — hover for the run of opponents. Early season this outweighs any per-player number, because those were all earned against last season's fixtures. |
| **▲ ▼** | Minutes rising or falling against the player's season average — hover for both figures. |
| **↷ moved** | Changed club since last season, so the security label was earned *somewhere else*. |

The **★ Risers** tab is a saved screen, not a metric: minutes trending up,
60%+ recent share, secure starting spot, no injury news, and above the
positional median on Pts / 90 — the combination that surfaced last season's
good picks before their prices moved. DefCon and Luck sit alongside so you
can judge whether the rise is sustainable.

**A season-wide minutes share describes where a player was, not where he is.**
Someone who forces his way into the side in March reads as a bench player all
season despite finishing it as a starter; someone who lost his place reads as
nailed. Security is therefore computed over the final 8 gameweeks and marked
▲ or ▼ where that diverges from the season figure. It changes the assessment
for roughly 45% of the board — Osula goes from a 24% season share ("bench
risk") to 75% across the closing weeks, and Vicario goes the other way, from
82% to 12%.

**Minutes security does not transfer with the player.** Someone who played
3,000 minutes elsewhere last season has proved he is durable, not that he has
won a place in his new squad. Those players are marked `↷ moved` and their
security badge is drawn dashed and faded; they are also excluded from the
"secure minutes only" filter unless you override them. Goalkeepers get a
harder check: any club carrying two keepers with 900+ prior minutes has both
marked **Contested**, because only one plays and prior minutes settle nothing.
Dubravka arriving at Spurs off a full season at Burnley is exactly this case —
he looks nailed and may not start a game.

**Read the two value columns together.** A big gap between them is the
interesting signal: a player ranking low on Pts / £m but high on Pts / 90 was
good when he played and is probably underpriced, because FPL prices off raw
totals. That gap is exactly what a single value ratio hides.

## Setup

```bash
gh repo clone TorIsHere/better-fpl
cd better-fpl
pip install pyyaml
python scripts/build.py     # writes index.html
```

Enable GitHub Pages once:

```bash
gh api repos/TorIsHere/better-fpl/pages -X POST \
  -f "source[branch]=main" -f "source[path]=/"
```

After that the workflow in `.github/workflows/update.yml` runs daily at
06:15 UTC, rebuilds the board and commits only if something changed. You can
also trigger it by hand with `gh workflow run "Update board"`.

## Overrides

The computed metrics are backward-looking. They know last season's numbers
and the live injury feed, and nothing else — not a role change, not a new
manager, not pre-season form. Two real cases this season:

- **Reece James** looked like a rotation risk on 1,957 minutes. He has since
  moved into the double pivot under Alonso's 3-4-2-1, which carries a far
  lower sprint load than the wing-back role that drove his hamstring history.
- **Ødegaard** scored 74 points in an injury-hit season and looked like the
  worst value in the game. He still finished joint-top for Arsenal assists in
  those minutes, then started all five World Cup games and took player of the
  match in the Community Shield.

Edit `data/overrides.yml` to encode that kind of judgement. It is re-applied
on every rebuild, so your read survives the nightly refresh:

```yaml
overrides:
  - name: Ødegaard
    team: ARS          # only needed when the short name is ambiguous
    security: Nailed
    proj_min: 80
    note: "Fit post-World Cup, POTM in the Community Shield"
```

Overridable fields are `security`, `proj_min`, `consistency` and `note`.
Anything omitted keeps its computed value. Overridden players get a violet
dot in the table; hover it to read the note. Pushing a change to this file
triggers an immediate rebuild.

## Data sources and the season switch

Prices, positions, teams and injury news always come live from
`fantasy.premierleague.com/api/bootstrap-static/`.

Points, minutes and consistency switch basis automatically:

- **Before gameweek 6** — last season's final totals, from the
  [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League)
  archive. A handful of games is too small a sample to rank anyone on.
- **From gameweek 6** — the current season, pulled from the FPL API's
  per-gameweek live endpoints (one request per finished gameweek, not one per
  player). No third-party dependency from this point on.

Players below 450 minutes on the active basis are excluded — their ratios are
mostly sample noise. New signings with no top-flight record won't appear until
the board switches to current-season data.

## Notes

- Third-party mirrors lag transfers. During the last window one had Bruno
  Guimarães still at Newcastle several days after a £75m move. Prices and
  clubs here come straight from the official API to avoid that.
- The build refuses to write an empty board, so a failed fetch leaves the last
  good `index.html` in place rather than publishing a blank page.
- If the FPL API starts rejecting the Actions runner's IP, run
  `python scripts/build.py` locally and commit — the script is identical.
- `data/snapshot.json` is committed alongside the site so you can diff what
  changed between days.

## Working on this

See [HANDOFF.md](HANDOFF.md) before changing `scripts/build.py` — it covers the
current state, unverified API assumptions, the backlog, and the non-obvious
gotchas found while building it.

## Testing offline

```bash
FPL_FIXTURE=path/to/bootstrap.json python scripts/build.py
```

Skips the API and reads a saved `bootstrap-static` payload instead.
