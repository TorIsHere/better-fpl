#!/usr/bin/env python3
"""
Build the FPL Transfer Value Board.

Pulls live prices, positions, availability and (once the season is underway)
current-season points from the official FPL API. Falls back to last season's
totals for the pre-season and early-season period, when the current season
has too few gameweeks to be meaningful.

Outputs:
    index.html          the site (embedded data, no runtime API calls)
    data/snapshot.json  the computed rows, for diffing / debugging

Usage:
    python scripts/build.py
    FPL_FIXTURE=tests/bootstrap.json python scripts/build.py   # offline test
"""

from __future__ import annotations

import datetime as dt
import csv
import io
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

ROOT = pathlib.Path(__file__).resolve().parent.parent

BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
LIVE_URL = "https://fantasy.premierleague.com/api/event/{gw}/live/"
FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"

# How many upcoming gameweeks the fixture-difficulty average looks across.
# Early season this outlook often matters more than any per-player metric,
# because every per-player number was earned against last season's fixtures.
FIXTURE_HORIZON = 5

# Last season's archive, used pre-season and as the consistency basis until
# enough of the current season has been played.
PREV_SEASON = "2025-26"
PREV_RAW_URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/"
    f"master/data/{PREV_SEASON}/players_raw.csv"
)
PREV_GW_URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/"
    f"master/data/{PREV_SEASON}/gws/merged_gw.csv"
)
PREV_TEAMS_URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/"
    f"master/data/{PREV_SEASON}/teams.csv"
)

# A goalkeeper needs this many minutes last season to count as a genuine rival
# for the shirt — one slot, so any club with two of these is a coin flip.
GK_RIVAL_MINUTES = 900

# Security is judged on the most recent gameweeks, not the whole season. A
# player who breaks into the side in March has a terrible season-wide minutes
# share and is nonetheless a starter; the reverse is true for someone who lost
# his place. Needs at least MIN_RECENT_GWS of history before it is trusted.
RECENT_WINDOW = 8
MIN_RECENT_GWS = 5
TREND_DELTA = 0.15  # share gap before a player is called rising or falling

# Switch to current-season numbers once this many gameweeks have finished.
# Below this, a handful of games is too noisy to rank anyone on.
CURRENT_SEASON_MIN_GWS = 6

# Ignore players below this many minutes on the active basis — their ratios
# are dominated by sample noise.
MIN_MINUTES_FULL_SEASON = 450
MIN_MINUTES_PER_GW = 12  # scales the floor when using a partial season

# Defensive contribution: 2 pts when the action count reaches the positional
# threshold, at most once per match. The `defensive_contribution` field from
# both the archive and the live endpoints is already the position-adjusted
# count (CBIT for defenders, CBIRT for midfielders and forwards) — verified
# exhaustively against the component columns on a full season. Do NOT
# recompute it from components, and do NOT treat non-zero as a hit; compare
# to the threshold. Goalkeepers are ineligible.
DEFCON_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12}
DEFCON_POINTS = 2
MIN_DEFCON_STARTS = 3  # below this many starts in the window the rate is noise

# Actual returns vs expected over the recent window. Above HOT the run is
# unsustainable (sell-high candidate); below COLD the player is finishing
# normally but unrewarded (buy-low candidate).
OVERPERF_HOT = 2.0
OVERPERF_COLD = -1.0

# Minutes share thresholds for the security label.
SECURITY_BANDS = [(0.80, "Nailed"), (0.60, "Solid starter"), (0.35, "Rotation risk")]

# Coefficient-of-variation thresholds for the consistency label.
CONSISTENCY_BANDS = [(0.67, "Consistent"), (0.80, "Balanced")]

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
USER_AGENT = "better-fpl-board/1.0 (+https://github.com/TorIsHere/better-fpl)"


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def fetch(url: str, retries: int = 3, backoff: float = 2.0) -> bytes:
    """GET a URL with a real User-Agent and simple retry.

    The FPL API rejects some default agents and occasionally rate-limits
    datacentre IPs, so retries matter more here than usual.
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as err:
            last_err = err
            if attempt < retries - 1:
                sleep = backoff * (attempt + 1)
                print(f"  fetch failed ({err}), retrying in {sleep:.0f}s", file=sys.stderr)
                time.sleep(sleep)
    raise SystemExit(f"FATAL: could not fetch {url}: {last_err}")


def load_bootstrap() -> dict:
    """Live game state, or a local fixture when FPL_FIXTURE is set."""
    fixture = os.environ.get("FPL_FIXTURE")
    if fixture:
        print(f"Using local fixture: {fixture}")
        return json.loads(pathlib.Path(fixture).read_text(encoding="utf-8"))
    print("Fetching bootstrap-static from the FPL API...")
    return json.loads(fetch(BOOTSTRAP_URL))


def _extras(starts, defcon, goals, assists, xg, xa) -> dict:
    """One gameweek of the secondary stats, normalised from either source.

    The archive serves numbers as strings and the live API serves expected
    stats as strings too, so everything goes through an explicit cast.
    """
    return {
        "start": int(float(starts or 0)),
        "dc": int(float(defcon or 0)),
        "ga": int(float(goals or 0)) + int(float(assists or 0)),
        "xga": float(xg or 0) + float(xa or 0),
    }


def fetch_current_gw_history(
    finished_gws: list[int],
) -> tuple[dict[int, list[tuple[int, int]]], dict[int, list[dict]]]:
    """Per-gameweek (minutes, points) plus secondary stats, from the live endpoints.

    One request per finished gameweek — at most 38 — rather than one per
    player, which would be several hundred.
    """
    history: dict[int, list[tuple[int, int]]] = {}
    extras: dict[int, list[dict]] = {}
    for gw in finished_gws:
        print(f"  gameweek {gw}...")
        payload = json.loads(fetch(LIVE_URL.format(gw=gw)))
        for el in payload.get("elements", []):
            stats = el.get("stats", {})
            history.setdefault(el["id"], []).append(
                (int(stats.get("minutes", 0)), int(stats.get("total_points", 0)))
            )
            extras.setdefault(el["id"], []).append(
                _extras(
                    stats.get("starts"),
                    stats.get("defensive_contribution"),
                    stats.get("goals_scored"),
                    stats.get("assists"),
                    stats.get("expected_goals"),
                    stats.get("expected_assists"),
                )
            )
    return history, extras


def load_previous_season() -> tuple[
    dict[str, dict],
    dict[str, list[tuple[int, int]]],
    dict[str, list[dict]],
    dict[str, str],
]:
    """Last season's totals and per-gameweek history, keyed by player code.

    Also returns a club-code -> short-name map covering last season, so a
    player who has since moved can be shown with the club he actually earned
    his minutes at.
    """
    print(f"Fetching {PREV_SEASON} archive...")
    raw = fetch(PREV_RAW_URL).decode("utf-8", errors="replace")
    totals: dict[str, dict] = {}
    code_to_id: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(raw)):
        totals[row["code"]] = row
        code_to_id[row["code"]] = row["id"]

    club_names: dict[str, str] = {}
    try:
        teams_raw = fetch(PREV_TEAMS_URL).decode("utf-8", errors="replace")
        for row in csv.DictReader(io.StringIO(teams_raw)):
            club_names[row["code"]] = row["short_name"]
    except SystemExit:
        print("WARNING: could not load previous-season teams", file=sys.stderr)

    gw_raw = fetch(PREV_GW_URL).decode("utf-8", errors="replace")
    by_element: dict[str, list[tuple[int, int, int, dict]]] = {}
    for row in csv.DictReader(io.StringIO(gw_raw)):
        try:
            by_element.setdefault(row["element"], []).append(
                (
                    int(row["GW"]),
                    int(row["minutes"]),
                    int(row["total_points"]),
                    _extras(
                        row.get("starts"),
                        row.get("defensive_contribution"),
                        row.get("goals_scored"),
                        row.get("assists"),
                        row.get("expected_goals"),
                        row.get("expected_assists"),
                    ),
                )
            )
        except (ValueError, KeyError):
            continue

    # Order by gameweek and drop the index — the recency window depends on
    # these being chronological, and CSV row order is not guaranteed to be.
    ordered = {
        eid: sorted(entries, key=lambda e: e[0]) for eid, entries in by_element.items()
    }

    # Re-key the history by player code so it survives the season's id reshuffle.
    history = {
        code: [(m, p) for _, m, p, _ in ordered.get(pid, [])]
        for code, pid in code_to_id.items()
    }
    extras = {
        code: [x for _, _, _, x in ordered.get(pid, [])]
        for code, pid in code_to_id.items()
    }
    return totals, history, extras, club_names


def fixture_outlook(
    fixtures: list[dict], teams: list[dict]
) -> dict[int, tuple[float | None, list[str]]]:
    """Mean FDR per club over the next FIXTURE_HORIZON gameweeks.

    Windowed by gameweek rather than by fixture count, so a double gameweek
    weighs both matches and a blank simply contributes nothing. Returns
    {team_id: (avg_difficulty, ["CHE (A) 4", ...])}.
    """
    short = {t["id"]: t["short_name"] for t in teams}
    upcoming: dict[int, list[tuple[int, int, str]]] = {}
    for f in fixtures:
        if f.get("finished") or f.get("event") is None:
            continue
        home, away = f["team_h"], f["team_a"]
        upcoming.setdefault(home, []).append(
            (f["event"], f["team_h_difficulty"], f"{short.get(away, '?')} (H)")
        )
        upcoming.setdefault(away, []).append(
            (f["event"], f["team_a_difficulty"], f"{short.get(home, '?')} (A)")
        )

    if not upcoming:
        return {}
    first_gw = min(ev for entries in upcoming.values() for ev, _, _ in entries)

    outlook: dict[int, tuple[float | None, list[str]]] = {}
    for team, entries in upcoming.items():
        window = sorted(e for e in entries if e[0] < first_gw + FIXTURE_HORIZON)
        if not window:
            outlook[team] = (None, [])
            continue
        avg = sum(diff for _, diff, _ in window) / len(window)
        outlook[team] = (round(avg, 1), [f"{opp} {diff}" for _, diff, opp in window])
    return outlook


def load_fixture_outlook(bootstrap: dict) -> dict[int, tuple[float | None, list[str]]]:
    """Fetch fixtures and compute the outlook; degrade to empty on failure.

    The board is still worth publishing without a difficulty column, so this
    must never kill the build the way a failed bootstrap fetch does.
    """
    fixture_env = os.environ.get("FPL_FIXTURE")
    try:
        if fixture_env:
            path = pathlib.Path(fixture_env).parent / "fixtures.json"
            if not path.exists():
                print(
                    "WARNING: no fixtures.json beside the bootstrap fixture — "
                    "difficulty column will be empty",
                    file=sys.stderr,
                )
                return {}
            fixtures = json.loads(path.read_text(encoding="utf-8"))
        else:
            print("Fetching fixtures...")
            fixtures = json.loads(fetch(FIXTURES_URL))
    except SystemExit:
        print(
            "WARNING: could not fetch fixtures — difficulty column will be empty",
            file=sys.stderr,
        )
        return {}
    return fixture_outlook(fixtures, bootstrap["teams"])


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def consistency_label(history: list[tuple[int, int]]) -> tuple[str, float | None]:
    """Spread of returns across games the player actually completed.

    Only counts appearances of 60+ minutes: a 5-minute cameo scoring 1 point
    says nothing about how reliably someone returns.
    """
    scores = [pts for mins, pts in history if mins >= 60]
    if len(scores) < 5:
        return "N/A", None
    mean = statistics.mean(scores)
    if mean <= 0:
        return "N/A", None
    cv = statistics.pstdev(scores) / mean
    for threshold, label in CONSISTENCY_BANDS:
        if cv <= threshold:
            return label, round(cv, 2)
    return "Boom/bust", round(cv, 2)


def security_label(share: float, status: str, chance: float | None) -> str:
    """Availability, from minutes share plus the official injury feed."""
    if status == "u":
        return "Unavailable"
    if status == "s":
        return "Suspended"
    if status == "i":
        return "Injured"
    if status == "d":
        return "Doubt"
    for threshold, label in SECURITY_BANDS:
        if share >= threshold:
            return label
    return "Bench risk"


def recent_share(history: list[tuple[int, int]]) -> float | None:
    """Minutes share over the most recent gameweeks only.

    A season-wide share describes where a player was, not where he is. Someone
    who forces his way into the side late reads as a bench player all season
    despite finishing it as a starter, and someone who lost his place reads as
    nailed. This window is what the security label should actually be built on.
    """
    if len(history) < MIN_RECENT_GWS:
        return None
    window = history[-RECENT_WINDOW:]
    return sum(mins for mins, _ in window) / (len(window) * 90)


def trend_label(recent: float | None, season: float) -> str:
    """Direction of travel between the recent window and the full season."""
    if recent is None:
        return ""
    if recent >= season + TREND_DELTA:
        return "rising"
    if recent <= season - TREND_DELTA:
        return "falling"
    return ""


def defcon_stats(extras: list[dict], pos: str) -> tuple[float | None, float | None]:
    """Hit rate and mean actions per start, over the recent window.

    The average is the more interesting number: a player sitting just under
    his threshold (8-9.9 for a defender) is one small role change away from
    banking 2 pts most weeks, and the market prices none of that.
    """
    threshold = DEFCON_THRESHOLD.get(pos)
    if threshold is None:
        return None, None
    started = [e for e in extras[-RECENT_WINDOW:] if e["start"]]
    if len(started) < MIN_DEFCON_STARTS:
        return None, None
    hits = sum(1 for e in started if e["dc"] >= threshold)
    avg = sum(e["dc"] for e in started) / len(started)
    return round(hits / len(started), 2), round(avg, 1)


def overperf_stats(extras: list[dict]) -> tuple[float | None, str]:
    """(goals + assists) − (xG + xA) over the recent window, plus a label.

    Positive means the returns outran the chances — points that came from
    finishing luck rather than repeatable output. Extreme overperformers look
    like the best players on the board on points alone; they are the traps.
    """
    if len(extras) < MIN_RECENT_GWS:
        return None, ""
    window = extras[-RECENT_WINDOW:]
    diff = sum(e["ga"] - e["xga"] for e in window)
    if diff > OVERPERF_HOT:
        label = "hot"
    elif diff < OVERPERF_COLD:
        label = "unlucky"
    else:
        label = ""
    return round(diff, 1), label


def mark_contested_keepers(rows: list[dict]) -> None:
    """Flag clubs carrying more than one credible goalkeeper.

    Only one keeper plays, so prior minutes tell you nothing about who wins
    the shirt. This is the situation that makes an inherited 'Nailed' label
    most misleading — a keeper can arrive off a full season elsewhere and
    still not start a single game.
    """
    by_club: dict[str, list[dict]] = {}
    for row in rows:
        if row["pos"] != "GKP":
            continue
        # A rival is anyone with a full season behind him OR anyone who held
        # the shirt recently. Judging on season minutes alone misses the
        # keeper who took over in March, who is often the likeliest starter.
        recent = row.get("recent_share")
        if row["minutes"] >= GK_RIVAL_MINUTES or (recent is not None and recent >= 50):
            by_club.setdefault(row["team"], []).append(row)

    for club, keepers in by_club.items():
        if len(keepers) < 2:
            continue
        names = sorted(r["name"] for r in keepers)
        for row in keepers:
            rivals = [n for n in names if n != row["name"]]
            row["contested"] = rivals
            row["security"] = "Contested"
            row["proj_min"] = int(round(row["proj_min"] / len(keepers)))
        print(f"  contested GK at {club}: {', '.join(names)}")


def projected_minutes(share: float, status: str, chance: float | None) -> int:
    """Expected minutes per gameweek, discounted by any current injury doubt."""
    proj = share * 90
    if status in ("i", "s", "u"):
        return 0
    if status == "d":
        proj *= (chance if chance is not None else 50) / 100
    return int(round(proj))


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------


def load_overrides() -> list[dict]:
    """Hand-written corrections that survive the nightly rebuild.

    The computed metrics are backward-looking: they cannot see a role change,
    a new manager, or current form. This is where your own read goes.
    """
    path = ROOT / "data" / "overrides.yml"
    if not path.exists():
        return []
    try:
        import yaml  # type: ignore
    except ImportError:
        print("WARNING: pyyaml not installed, skipping overrides", file=sys.stderr)
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = doc.get("overrides") or []
    print(f"Loaded {len(entries)} override(s)")
    return entries


def apply_overrides(rows: list[dict], overrides: list[dict]) -> None:
    """Overlay manual corrections, tracking which fields were touched."""
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        by_name.setdefault(row["name"].lower(), []).append(row)

    for entry in overrides:
        name = str(entry.get("name", "")).lower()
        team = entry.get("team")
        candidates = by_name.get(name, [])
        if team:
            candidates = [r for r in candidates if r["team"].upper() == str(team).upper()]
        if not candidates:
            print(f"WARNING: override for '{entry.get('name')}' matched no player", file=sys.stderr)
            continue
        if len(candidates) > 1:
            print(
                f"WARNING: override for '{entry.get('name')}' is ambiguous "
                f"({len(candidates)} matches) — add a 'team:' key",
                file=sys.stderr,
            )
            continue

        row = candidates[0]
        touched = []
        for field in ("security", "consistency", "proj_min"):
            if field in entry and entry[field] is not None:
                row[field] = entry[field]
                touched.append(field)
        if entry.get("note"):
            row["note"] = str(entry["note"])
        if touched or entry.get("note"):
            row["overridden"] = True
            print(f"  override: {row['name']} ({row['team']}) -> {', '.join(touched) or 'note only'}")


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_rows(bootstrap: dict) -> tuple[list[dict], dict]:
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    events = bootstrap.get("events", [])
    finished = [e["id"] for e in events if e.get("finished")]
    outlook = load_fixture_outlook(bootstrap)

    use_current = len(finished) >= CURRENT_SEASON_MIN_GWS
    if use_current:
        basis = f"2026-27 season, gameweeks 1-{max(finished)}"
        print(f"Basis: current season ({len(finished)} gameweeks finished)")
        history_by_id, extras_by_id = fetch_current_gw_history(finished)
        max_minutes = len(finished) * 90
        min_minutes = len(finished) * MIN_MINUTES_PER_GW
        prev_totals, prev_history, prev_extras, prev_clubs = {}, {}, {}, {}
    else:
        basis = f"{PREV_SEASON} final season totals"
        print(
            f"Basis: {PREV_SEASON} ({len(finished)} gameweeks finished this season, "
            f"need {CURRENT_SEASON_MIN_GWS})"
        )
        prev_totals, prev_history, prev_extras, prev_clubs = load_previous_season()
        history_by_id, extras_by_id = {}, {}
        max_minutes = 38 * 90
        min_minutes = MIN_MINUTES_FULL_SEASON

    # Stable club code per current team, for detecting transfers. The season-
    # local `id` is NOT a substitute — it is reshuffled every year, so falling
    # back to it would mark essentially every player as transferred. Better to
    # stop the build than publish that.
    missing_code = [t["short_name"] for t in bootstrap["teams"] if not t.get("code")]
    if missing_code:
        raise SystemExit(
            f"FATAL: teams missing stable 'code': {', '.join(missing_code)} — "
            "transfer detection would corrupt; refusing to build"
        )
    club_code = {t["id"]: str(t["code"]) for t in bootstrap["teams"]}

    rows: list[dict] = []
    unmatched = 0

    for el in bootstrap["elements"]:
        price = el["now_cost"] / 10.0
        if price <= 0:
            continue

        code = str(el["code"])
        moved_from = ""
        if use_current:
            points = int(el.get("total_points", 0))
            minutes = int(el.get("minutes", 0))
            history = history_by_id.get(el["id"], [])
            extras = extras_by_id.get(el["id"], [])
        else:
            prev = prev_totals.get(code)
            if not prev:
                unmatched += 1
                continue  # no top-flight history: new signing or promoted-club player
            points = int(prev["total_points"])
            minutes = int(prev["minutes"])
            history = prev_history.get(code, [])
            extras = prev_extras.get(code, [])
            # Minutes security is not portable. A player who racked up a full
            # season elsewhere tells you he is durable, not that he has won a
            # place in this squad.
            old_club = str(prev.get("team_code", ""))
            if old_club and old_club != club_code.get(el["team"], ""):
                moved_from = prev_clubs.get(old_club, "another club")

        if minutes < min_minutes:
            continue

        season_share = min(1.0, minutes / max_minutes) if max_minutes else 0.0
        recent = recent_share(history)
        # Judge availability on the recent window when there is enough of it.
        share = recent if recent is not None else season_share
        status = el.get("status", "a")
        chance = el.get("chance_of_playing_next_round")
        chance = float(chance) if chance is not None else None
        cons, cv = consistency_label(history)
        pos = POSITIONS.get(el["element_type"], "?")
        dc_rate, dc_avg = defcon_stats(extras, pos)
        overperf, luck = overperf_stats(extras)
        fix_avg, fix_ops = outlook.get(el["team"], (None, []))

        rows.append(
            {
                "name": el["web_name"],
                "team": teams.get(el["team"], "?"),
                "pos": pos,
                "price": round(price, 1),
                "points": points,
                "minutes": minutes,
                "ratio": round(points / price, 2),
                "p90": round(points / (minutes / 90.0), 2),
                "consistency": cons,
                "cv": cv,
                "proj_min": projected_minutes(share, status, chance),
                "security": security_label(share, status, chance),
                "defcon_rate": dc_rate,
                "defcon_avg": dc_avg,
                "overperf": overperf,
                "luck": luck,
                "fix_avg": fix_avg,
                "fix_ops": fix_ops,
                "trend": trend_label(recent, season_share),
                "season_share": round(season_share * 100),
                "recent_share": round(recent * 100) if recent is not None else None,
                "moved_from": moved_from,
                "contested": [],
                "news": (el.get("news") or "")[:120],
                "overridden": False,
                "note": "",
            }
        )

    if unmatched:
        print(f"  ({unmatched} players skipped: no {PREV_SEASON} top-flight record)")

    if not use_current:
        moved = [r for r in rows if r["moved_from"]]
        if moved:
            print(f"  {len(moved)} player(s) changed club — security marked as inherited")
        mark_contested_keepers(rows)

    meta = {
        "basis": basis,
        "use_current": use_current,
        "finished_gws": len(finished),
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    return rows, meta


def group_by_position(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["pos"], []).append(row)
    for key in grouped:
        grouped[key].sort(key=lambda r: -r["ratio"])
    return grouped


def render(grouped: dict[str, list[dict]], meta: dict) -> str:
    template = (ROOT / "scripts" / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(grouped, separators=(",", ":"), ensure_ascii=False)
    return (
        template.replace("__DATA__", payload)
        .replace("__BASIS__", meta["basis"])
        .replace("__BUILT_AT__", meta["built_at"])
    )


def main() -> int:
    bootstrap = load_bootstrap()
    rows, meta = build_rows(bootstrap)
    if not rows:
        raise SystemExit("FATAL: no rows produced — refusing to write an empty board")

    apply_overrides(rows, load_overrides())
    grouped = group_by_position(rows)

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "snapshot.json").write_text(
        json.dumps({"meta": meta, "players": grouped}, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    (ROOT / "index.html").write_text(render(grouped, meta), encoding="utf-8")

    total = sum(len(v) for v in grouped.values())
    print(f"\nWrote index.html — {total} players ({meta['basis']})")
    for pos in ("GKP", "DEF", "MID", "FWD"):
        print(f"  {pos}: {len(grouped.get(pos, []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
