"""Offline tests: API-shape assumptions plus the pure metric functions.

The fixture in tests/bootstrap.json is a real bootstrap-static payload saved
from the live API (2026-08-18). The shape tests exist because every one of
these fields was originally an *assumption* — see HANDOFF.md — and each has a
silent failure mode if the API changes. Run with:

    python3 -m unittest discover tests
"""

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build  # noqa: E402

FIXTURE = json.loads((ROOT / "tests" / "bootstrap.json").read_text(encoding="utf-8"))


class BootstrapShape(unittest.TestCase):
    """Each test guards one silent failure mode from the P0 table."""

    def test_now_cost_is_in_tenths(self):
        # A pre-divided source (one mirror did this) would put every price
        # off by 10x. Cheapest legal price is £4.0m, premiums under £20m.
        costs = [e["now_cost"] for e in FIXTURE["elements"]]
        self.assertGreaterEqual(min(costs), 38)
        self.assertLess(max(costs), 200)

    def test_element_type_matches_positions_map(self):
        # Unknown types land players in "?" and they vanish from the UI.
        api_map = {t["id"]: t["singular_name_short"] for t in FIXTURE["element_types"]}
        self.assertEqual(api_map, build.POSITIONS)
        for el in FIXTURE["elements"]:
            self.assertIn(el["element_type"], build.POSITIONS)

    def test_status_codes_are_known(self):
        # An unknown code falls through to the minutes-share bands, so an
        # injured player would read as Nailed.
        statuses = {e["status"] for e in FIXTURE["elements"]}
        self.assertTrue(statuses <= {"a", "d", "i", "s", "u"}, statuses)

    def test_chance_of_playing_is_number_or_null(self):
        for el in FIXTURE["elements"]:
            chance = el["chance_of_playing_next_round"]
            self.assertIsInstance(chance, (int, float, type(None)))

    def test_teams_have_stable_code(self):
        for team in FIXTURE["teams"]:
            self.assertTrue(team.get("code"), team["short_name"])

    def test_events_finished_is_bool(self):
        for ev in FIXTURE["events"]:
            self.assertIsInstance(ev["finished"], bool)

    def test_elements_have_stable_code(self):
        # Cross-season identity for transfer detection and the archive join.
        for el in FIXTURE["elements"]:
            self.assertTrue(el.get("code"))


class MetricFunctions(unittest.TestCase):
    def test_consistency_needs_five_full_games(self):
        history = [(90, 5)] * 4
        self.assertEqual(build.consistency_label(history), ("N/A", None))

    def test_consistency_ignores_cameos(self):
        # Four full games plus a cameo is still only four scoring samples.
        history = [(90, 5)] * 4 + [(10, 1)]
        self.assertEqual(build.consistency_label(history), ("N/A", None))

    def test_consistency_flat_returns_are_consistent(self):
        label, cv = build.consistency_label([(90, 5)] * 6)
        self.assertEqual(label, "Consistent")
        self.assertEqual(cv, 0.0)

    def test_consistency_spiky_returns_are_boombust(self):
        label, _ = build.consistency_label([(90, 2), (90, 15), (90, 1), (90, 12), (90, 2)])
        self.assertEqual(label, "Boom/bust")

    def test_security_status_beats_minutes_share(self):
        # A nailed minutes share must not mask the live injury feed.
        self.assertEqual(build.security_label(0.95, "i", None), "Injured")
        self.assertEqual(build.security_label(0.95, "s", None), "Suspended")
        self.assertEqual(build.security_label(0.95, "u", None), "Unavailable")
        self.assertEqual(build.security_label(0.95, "d", 75), "Doubt")

    def test_security_bands(self):
        self.assertEqual(build.security_label(0.85, "a", None), "Nailed")
        self.assertEqual(build.security_label(0.70, "a", None), "Solid starter")
        self.assertEqual(build.security_label(0.50, "a", None), "Rotation risk")
        self.assertEqual(build.security_label(0.10, "a", None), "Bench risk")

    def test_recent_share_windows_last_gameweeks(self):
        # 30 bench GWs then 8 full games: the season says bench, the window
        # says starter. The window must win.
        history = [(0, 0)] * 30 + [(90, 6)] * 8
        self.assertEqual(build.recent_share(history), 1.0)

    def test_recent_share_needs_enough_history(self):
        self.assertIsNone(build.recent_share([(90, 6)] * (build.MIN_RECENT_GWS - 1)))

    def test_trend_labels(self):
        self.assertEqual(build.trend_label(0.90, 0.40), "rising")
        self.assertEqual(build.trend_label(0.10, 0.80), "falling")
        self.assertEqual(build.trend_label(0.55, 0.50), "")
        self.assertEqual(build.trend_label(None, 0.50), "")

    def test_projected_minutes_discounts_doubt(self):
        self.assertEqual(build.projected_minutes(1.0, "a", None), 90)
        self.assertEqual(build.projected_minutes(1.0, "d", 75), 68)
        self.assertEqual(build.projected_minutes(1.0, "d", None), 45)
        self.assertEqual(build.projected_minutes(1.0, "i", None), 0)


def _gw(start=1, dc=0, ga=0, xga=0.0):
    return {"start": start, "dc": dc, "ga": ga, "xga": xga}


class DefconStats(unittest.TestCase):
    def test_goalkeepers_are_ineligible(self):
        self.assertEqual(build.defcon_stats([_gw(dc=12)] * 8, "GKP"), (None, None))

    def test_needs_minimum_starts(self):
        extras = [_gw(dc=12)] * (build.MIN_DEFCON_STARTS - 1)
        self.assertEqual(build.defcon_stats(extras, "DEF"), (None, None))

    def test_threshold_is_positional(self):
        # 11 actions is a hit for a defender (10+) but not a midfielder (12+).
        extras = [_gw(dc=11)] * 6
        self.assertEqual(build.defcon_stats(extras, "DEF"), (1.0, 11.0))
        self.assertEqual(build.defcon_stats(extras, "MID"), (0.0, 11.0))

    def test_nonzero_is_not_a_hit(self):
        # The gotcha that produced nonsense in an early analysis: a non-zero
        # count is not a threshold hit.
        extras = [_gw(dc=3)] * 6
        rate, avg = build.defcon_stats(extras, "FWD")
        self.assertEqual(rate, 0.0)
        self.assertEqual(avg, 3.0)

    def test_bench_games_do_not_dilute(self):
        extras = [_gw(start=0, dc=0)] * 4 + [_gw(dc=10)] * 4
        self.assertEqual(build.defcon_stats(extras, "DEF"), (1.0, 10.0))

    def test_windowed_to_recent_gameweeks(self):
        # A season of hits followed by a window of misses must read as misses.
        extras = [_gw(dc=14)] * 30 + [_gw(dc=2)] * build.RECENT_WINDOW
        rate, avg = build.defcon_stats(extras, "DEF")
        self.assertEqual(rate, 0.0)
        self.assertEqual(avg, 2.0)


class OverperfStats(unittest.TestCase):
    def test_needs_enough_history(self):
        self.assertEqual(
            build.overperf_stats([_gw()] * (build.MIN_RECENT_GWS - 1)), (None, "")
        )

    def test_hot_and_unlucky_labels(self):
        hot = [_gw(ga=1, xga=0.5)] * 8       # +4.0 over the window
        cold = [_gw(ga=0, xga=0.3)] * 8      # -2.4
        neutral = [_gw(ga=1, xga=0.9)] * 8   # +0.8
        self.assertEqual(build.overperf_stats(hot), (4.0, "hot"))
        self.assertEqual(build.overperf_stats(cold), (-2.4, "unlucky"))
        self.assertEqual(build.overperf_stats(neutral), (0.8, ""))

    def test_windowed_to_recent_gameweeks(self):
        # A hot autumn must not colour a normal spring.
        extras = [_gw(ga=2, xga=0.2)] * 20 + [_gw(ga=0, xga=0.0)] * build.RECENT_WINDOW
        self.assertEqual(build.overperf_stats(extras), (0.0, ""))


class FixtureOutlook(unittest.TestCase):
    TEAMS = [{"id": 1, "short_name": "ARS"}, {"id": 2, "short_name": "CHE"},
             {"id": 3, "short_name": "LEE"}]

    @staticmethod
    def _fx(event, h, a, hd, ad, finished=False):
        return {"event": event, "team_h": h, "team_a": a,
                "team_h_difficulty": hd, "team_a_difficulty": ad,
                "finished": finished}

    def test_home_and_away_difficulty_assignment(self):
        outlook = build.fixture_outlook([self._fx(1, 1, 2, 2, 5)], self.TEAMS)
        self.assertEqual(outlook[1], (2.0, ["CHE (H) 2"]))
        self.assertEqual(outlook[2], (5.0, ["ARS (A) 5"]))

    def test_windowed_by_gameweek_not_fixture_count(self):
        # A double gameweek weighs both matches; beyond the horizon is cut.
        fixtures = [self._fx(gw, 1, 2, 2, 2) for gw in range(1, 5)]
        fixtures.append(self._fx(4, 1, 3, 4, 4))            # DGW4
        fixtures.append(self._fx(build.FIXTURE_HORIZON + 1, 1, 3, 5, 5))
        avg, ops = build.fixture_outlook(fixtures, self.TEAMS)[1]
        self.assertEqual(len(ops), 5)
        self.assertEqual(avg, round((2 * 4 + 4) / 5, 1))

    def test_finished_fixtures_are_ignored(self):
        fixtures = [self._fx(1, 1, 2, 5, 5, finished=True), self._fx(2, 1, 2, 2, 3)]
        avg, ops = build.fixture_outlook(fixtures, self.TEAMS)[1]
        self.assertEqual((avg, ops), (2.0, ["CHE (H) 2"]))

    def test_empty_fixtures(self):
        self.assertEqual(build.fixture_outlook([], self.TEAMS), {})


class Overrides(unittest.TestCase):
    def _row(self, name="Doe", team="ARS"):
        return {
            "name": name, "team": team, "security": "Bench risk",
            "consistency": "N/A", "proj_min": 10, "overridden": False, "note": "",
        }

    def test_override_applies_and_marks(self):
        row = self._row()
        build.apply_overrides([row], [{"name": "Doe", "security": "Nailed", "note": "x"}])
        self.assertEqual(row["security"], "Nailed")
        self.assertTrue(row["overridden"])

    def test_ambiguous_override_is_skipped(self):
        rows = [self._row(team="ARS"), self._row(team="CHE")]
        build.apply_overrides(rows, [{"name": "Doe", "security": "Nailed"}])
        self.assertFalse(any(r["overridden"] for r in rows))

    def test_team_disambiguates(self):
        rows = [self._row(team="ARS"), self._row(team="CHE")]
        build.apply_overrides(rows, [{"name": "Doe", "team": "CHE", "security": "Nailed"}])
        self.assertEqual(rows[1]["security"], "Nailed")
        self.assertFalse(rows[0]["overridden"])

    def test_code_beats_name_ambiguity(self):
        rows = [self._row(team="ARS"), self._row(team="CHE")]
        rows[0]["code"], rows[1]["code"] = 111, 222
        build.apply_overrides(rows, [{"name": "Doe", "code": 222, "security": "Nailed"}])
        self.assertEqual(rows[1]["security"], "Nailed")
        self.assertFalse(rows[0]["overridden"])

    def test_unknown_code_is_skipped(self):
        row = self._row()
        row["code"] = 111
        build.apply_overrides([row], [{"code": 999, "security": "Nailed"}])
        self.assertFalse(row["overridden"])


class ContestedKeepers(unittest.TestCase):
    def _keeper(self, name, team, minutes, recent=None):
        return {
            "name": name, "team": team, "pos": "GKP", "minutes": minutes,
            "recent_share": recent, "security": "Nailed", "proj_min": 90,
            "contested": [],
        }

    def test_two_credible_keepers_are_contested(self):
        rows = [self._keeper("A", "TOT", 3000), self._keeper("B", "TOT", 1500)]
        build.mark_contested_keepers(rows)
        for row in rows:
            self.assertEqual(row["security"], "Contested")
            self.assertEqual(row["proj_min"], 45)

    def test_recent_starter_counts_without_season_minutes(self):
        # The keeper who took over in March has few season minutes but holds
        # the shirt — he is the rival that matters.
        rows = [self._keeper("A", "MUN", 3000), self._keeper("B", "MUN", 400, recent=80)]
        build.mark_contested_keepers(rows)
        self.assertEqual(rows[0]["security"], "Contested")

    def test_clear_number_one_is_untouched(self):
        rows = [self._keeper("A", "ARS", 3000), self._keeper("B", "ARS", 100)]
        build.mark_contested_keepers(rows)
        self.assertEqual(rows[0]["security"], "Nailed")


if __name__ == "__main__":
    unittest.main()
