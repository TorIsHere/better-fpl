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

# Switch to current-season numbers once this many gameweeks have finished.
# Below this, a handful of games is too noisy to rank anyone on.
CURRENT_SEASON_MIN_GWS = 6

# Ignore players below this many minutes on the active basis — their ratios
# are dominated by sample noise.
MIN_MINUTES_FULL_SEASON = 450
MIN_MINUTES_PER_GW = 12  # scales the floor when using a partial season

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


def fetch_current_gw_history(finished_gws: list[int]) -> dict[int, list[tuple[int, int]]]:
    """Per-gameweek (minutes, points) for every player, from the live endpoints.

    One request per finished gameweek — at most 38 — rather than one per
    player, which would be several hundred.
    """
    history: dict[int, list[tuple[int, int]]] = {}
    for gw in finished_gws:
        print(f"  gameweek {gw}...")
        payload = json.loads(fetch(LIVE_URL.format(gw=gw)))
        for el in payload.get("elements", []):
            stats = el.get("stats", {})
            history.setdefault(el["id"], []).append(
                (int(stats.get("minutes", 0)), int(stats.get("total_points", 0)))
            )
    return history


def load_previous_season() -> tuple[dict[str, dict], dict[str, list[tuple[int, int]]]]:
    """Last season's totals (keyed by player code) and per-gameweek history."""
    print(f"Fetching {PREV_SEASON} archive...")
    raw = fetch(PREV_RAW_URL).decode("utf-8", errors="replace")
    totals: dict[str, dict] = {}
    code_to_id: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(raw)):
        totals[row["code"]] = row
        code_to_id[row["code"]] = row["id"]

    gw_raw = fetch(PREV_GW_URL).decode("utf-8", errors="replace")
    by_element: dict[str, list[tuple[int, int]]] = {}
    for row in csv.DictReader(io.StringIO(gw_raw)):
        try:
            by_element.setdefault(row["element"], []).append(
                (int(row["minutes"]), int(row["total_points"]))
            )
        except (ValueError, KeyError):
            continue

    # Re-key the history by player code so it survives the season's id reshuffle.
    history = {code: by_element.get(pid, []) for code, pid in code_to_id.items()}
    return totals, history


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

    use_current = len(finished) >= CURRENT_SEASON_MIN_GWS
    if use_current:
        basis = f"2026-27 season, gameweeks 1-{max(finished)}"
        print(f"Basis: current season ({len(finished)} gameweeks finished)")
        history_by_id = fetch_current_gw_history(finished)
        max_minutes = len(finished) * 90
        min_minutes = len(finished) * MIN_MINUTES_PER_GW
        prev_totals, prev_history = {}, {}
    else:
        basis = f"{PREV_SEASON} final season totals"
        print(
            f"Basis: {PREV_SEASON} ({len(finished)} gameweeks finished this season, "
            f"need {CURRENT_SEASON_MIN_GWS})"
        )
        prev_totals, prev_history = load_previous_season()
        history_by_id = {}
        max_minutes = 38 * 90
        min_minutes = MIN_MINUTES_FULL_SEASON

    rows: list[dict] = []
    unmatched = 0

    for el in bootstrap["elements"]:
        price = el["now_cost"] / 10.0
        if price <= 0:
            continue

        code = str(el["code"])
        if use_current:
            points = int(el.get("total_points", 0))
            minutes = int(el.get("minutes", 0))
            history = history_by_id.get(el["id"], [])
        else:
            prev = prev_totals.get(code)
            if not prev:
                unmatched += 1
                continue  # no top-flight history: new signing or promoted-club player
            points = int(prev["total_points"])
            minutes = int(prev["minutes"])
            history = prev_history.get(code, [])

        if minutes < min_minutes:
            continue

        share = min(1.0, minutes / max_minutes) if max_minutes else 0.0
        status = el.get("status", "a")
        chance = el.get("chance_of_playing_next_round")
        chance = float(chance) if chance is not None else None
        cons, cv = consistency_label(history)

        rows.append(
            {
                "name": el["web_name"],
                "team": teams.get(el["team"], "?"),
                "pos": POSITIONS.get(el["element_type"], "?"),
                "price": round(price, 1),
                "points": points,
                "minutes": minutes,
                "ratio": round(points / price, 2),
                "p90": round(points / (minutes / 90.0), 2),
                "consistency": cons,
                "cv": cv,
                "proj_min": projected_minutes(share, status, chance),
                "security": security_label(share, status, chance),
                "news": (el.get("news") or "")[:120],
                "overridden": False,
                "note": "",
            }
        )

    if unmatched:
        print(f"  ({unmatched} players skipped: no {PREV_SEASON} top-flight record)")

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
