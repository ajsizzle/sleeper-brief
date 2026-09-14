"""
test_sleeper_brief.py

Runs with the standard library only:  python -m unittest -v

Every test feeds the script fake Sleeper data with distinctive canary
values for the league ID, username, user IDs, and display names, then
asserts that none of them appear in league.md, league.json, stdout,
stderr, or any exception message. If a future edit leaks one of these,
the test suite fails and the workflow stops before anything is committed.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sleeper_brief", HERE / "sleeper_brief.py")
sb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sb)

# Canaries: values that must never appear anywhere observable.
LEAGUE_ID = "987654321098765432"
USERNAME = "canary_owner_zq"
MY_UID = "112233445566778899"
OTHER_UID = "998877665544332211"
MY_DISPLAY = "CanaryDisplayName"
OTHER_DISPLAY = "RivalDisplayName"
AVATAR = "cc12ec49965eb7856f84d71cf85306af"
CANARIES = [LEAGUE_ID, USERNAME, MY_UID, OTHER_UID, MY_DISPLAY, OTHER_DISPLAY, AVATAR]


def fake_api():
    players = {
        str(i): {"position": "RB" if i % 2 else "WR", "full_name": f"Player {i}",
                 "team": "KC", "injury_status": "Questionable" if i == 12 else None}
        for i in range(11, 40)
    }
    players["HOU"] = {"position": "DEF", "first_name": "Houston", "last_name": "Texans", "team": "HOU"}
    players["DET"] = {"position": "DEF", "first_name": "Detroit", "last_name": "Lions", "team": "DET"}
    players["500"] = {"position": "OL", "full_name": "Not Fantasy Relevant"}
    return {
        "/state/nfl": {"week": 3, "season": "2026"},
        f"/league/{LEAGUE_ID}": {
            "name": "Canary League", "total_rosters": 2,
            "settings": {"waiver_type": 2, "waiver_budget": 100},
            "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"],
        },
        f"/league/{LEAGUE_ID}/rosters": [
            {"roster_id": 1, "owner_id": MY_UID, "co_owners": None,
             "starters": ["11", "12", "13", "14", "15", "16", "17", "18", "HOU"],
             "players": ["11", "12", "13", "14", "15", "16", "17", "18", "HOU", "19"],
             "reserve": ["20"],
             "settings": {"wins": 2, "losses": 0, "fpts": 210, "fpts_decimal": 50,
                          "waiver_position": 4, "waiver_budget_used": 0}},
            {"roster_id": 2, "owner_id": OTHER_UID, "co_owners": None,
             "starters": ["21", "22", "0", "24", "25", "26", "27", "28", "DET"],
             "players": ["21", "22", "24", "25", "26", "27", "28", "DET", "30"],
             "reserve": [],
             "settings": {"wins": 0, "losses": 2, "fpts": 150, "fpts_decimal": 0,
                          "waiver_position": 1, "waiver_budget_used": 12}},
        ],
        f"/league/{LEAGUE_ID}/users": [
            {"user_id": MY_UID, "username": USERNAME, "display_name": MY_DISPLAY,
             "avatar": AVATAR, "metadata": {"team_name": "Lighthouse"}},
            {"user_id": OTHER_UID, "username": "rival_user", "display_name": OTHER_DISPLAY,
             "avatar": AVATAR, "metadata": {}},   # no team name: must render as "Team 2"
        ],
        f"/league/{LEAGUE_ID}/matchups/3": [
            {"roster_id": 1, "matchup_id": 7, "starters": [], "players": []},
            {"roster_id": 2, "matchup_id": 7, "starters": [], "players": []},
        ],
        f"/league/{LEAGUE_ID}/transactions/3": [
            {"type": "waiver", "status": "complete", "roster_ids": [2], "creator": OTHER_UID,
             "consenter_ids": [2], "adds": {"30": 2}, "drops": {"23": 2},
             "settings": {"waiver_bid": 12}, "metadata": None, "leg": 3,
             "created": 1757430000000, "draft_picks": []},
        ],
        f"/league/{LEAGUE_ID}/transactions/2": [
            {"type": "trade", "status": "complete", "roster_ids": [1, 2], "creator": MY_UID,
             "consenter_ids": [1, 2], "adds": {"19": 1, "21": 2}, "drops": {"19": 2, "21": 1},
             "settings": None, "metadata": {"notes": "processed"}, "leg": 2,
             "created": 1756800000000,
             "draft_picks": [{"season": "2027", "round": 3, "roster_id": 1,
                              "previous_owner_id": 1, "owner_id": 2}]},
        ],
        "/players/nfl/trending/add?lookback_hours=24&limit=25": [
            {"player_id": "35", "count": 4000},
            {"player_id": "30", "count": 900},
            {"player_id": "11", "count": 10},
        ],
        f"/user/{USERNAME}": {"user_id": MY_UID, "username": USERNAME,
                               "display_name": MY_DISPLAY, "avatar": AVATAR},
        "/players/nfl": players,
    }


class Harness(unittest.TestCase):
    """Runs the script in a temp dir against a fake API, capturing everything."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        sb.ROOT, sb.CACHE = root, root / "players_trim.json"
        sb.OUT_MD, sb.OUT_JSON = root / "league.md", root / "league.json"
        self.api = fake_api()
        self.calls = []
        os.environ["LEAGUE_ID"] = LEAGUE_ID
        os.environ["SLEEPER_USERNAME"] = USERNAME

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("LEAGUE_ID", None)
        os.environ.pop("SLEEPER_USERNAME", None)

    def run_script(self, getter=None):
        """Return (stdout, stderr, exception_or_None)."""
        def default(path):
            self.calls.append(path)
            return self.api.get(path)
        sb.get = getter or default
        out, err, exc = io.StringIO(), io.StringIO(), None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                sb.main()
            except BaseException as e:   # SystemExit included
                exc = e
        return out.getvalue(), err.getvalue(), exc

    def observable(self, out, err, exc):
        parts = [out, err, str(exc) if exc else "", repr(exc) if exc else ""]
        for f in (sb.OUT_MD, sb.OUT_JSON):
            if f.exists():
                parts.append(f.read_text())
        return "\n".join(parts)

    def assert_no_canaries(self, text):
        for c in CANARIES:
            self.assertNotIn(c, text, f"canary leaked: {c[:4]}...")
        self.assertIsNone(re.search(r"\d{15,}", text), "a long numeric ID leaked")


class TestOutputIsClean(Harness):

    def test_happy_path_writes_files_without_identifiers(self):
        out, err, exc = self.run_script()
        self.assertIsNone(exc, f"unexpected failure: {exc}")
        self.assertTrue(sb.OUT_MD.exists() and sb.OUT_JSON.exists())
        self.assert_no_canaries(self.observable(out, err, exc))

    def test_output_still_has_the_useful_stuff(self):
        self.run_script()
        md = sb.OUT_MD.read_text()
        self.assertIn("Lighthouse", md)               # my team name
        self.assertIn("Team 2", md)                   # nameless team, no display name
        self.assertIn("Player 12 (WR, KC) [Questionable]", md)
        self.assertIn("Houston Texans (DEF, HOU)", md)
        self.assertIn("(empty)", md)                  # empty starter slot handled
        self.assertIn("bid $12", md)
        self.assertIn("AVAILABLE in my league", md)
        self.assertIn("rostered by Team 2", md)
        self.assertIn("### IR", md)
        data = json.loads(sb.OUT_JSON.read_text())
        self.assertNotIn("id", data["league"])
        self.assertNotIn("owner", data["me"])
        self.assertTrue(all(isinstance(t, str) for t in data["transactions"]))

    def test_player_cache_holds_only_names(self):
        self.run_script()
        cache = sb.CACHE.read_text()
        self.assert_no_canaries(cache)
        self.assertNotIn("Not Fantasy Relevant", cache)   # non-fantasy positions dropped

    def test_cache_is_reused_within_24h(self):
        self.run_script()
        first = self.calls.count("/players/nfl")
        self.calls.clear()
        self.run_script()
        self.assertEqual(first, 1)
        self.assertEqual(self.calls.count("/players/nfl"), 0)


class TestFailuresDoNotLeak(Harness):

    def test_missing_env(self):
        os.environ.pop("LEAGUE_ID")
        out, err, exc = self.run_script()
        self.assertIsInstance(exc, SystemExit)
        self.assert_no_canaries(self.observable(out, err, exc))

    def test_league_not_found(self):
        self.api[f"/league/{LEAGUE_ID}"] = None
        out, err, exc = self.run_script()
        self.assertIsInstance(exc, SystemExit)
        self.assert_no_canaries(self.observable(out, err, exc))

    def test_user_not_found(self):
        self.api[f"/user/{USERNAME}"] = None
        out, err, exc = self.run_script()
        self.assertIsInstance(exc, SystemExit)
        self.assert_no_canaries(self.observable(out, err, exc))

    def test_user_not_in_league(self):
        self.api[f"/user/{USERNAME}"] = {"user_id": "555555555555555555"}
        out, err, exc = self.run_script()
        self.assertIsInstance(exc, SystemExit)
        self.assert_no_canaries(self.observable(out, err, exc))

    def test_http_500_message_has_no_url(self):
        # Patch urlopen so the real get() runs its error path.
        import urllib.request
        real = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
                urllib.error.HTTPError(sb.BASE + f"/league/{LEAGUE_ID}", 500, "Server Error", {}, None))
            SPEC.loader.exec_module(sb)   # reload pristine get()
            with self.assertRaises(RuntimeError) as cm:
                sb.get(f"/league/{LEAGUE_ID}")
        finally:
            urllib.request.urlopen = real
            SPEC.loader.exec_module(sb)
        self.assert_no_canaries(str(cm.exception))
        self.assertIn("HTTP 500", str(cm.exception))

    def test_network_error_message_has_no_url(self):
        import urllib.request
        real = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
                urllib.error.URLError("temporary failure in name resolution"))
            SPEC.loader.exec_module(sb)
            with self.assertRaises(RuntimeError) as cm:
                sb.get(f"/league/{LEAGUE_ID}/users")
        finally:
            urllib.request.urlopen = real
            SPEC.loader.exec_module(sb)
        self.assert_no_canaries(str(cm.exception))

    def test_handle_used_as_team_name_is_allowed(self):
        """A member whose team name equals their handle must not trip the guard."""
        self.api[f"/league/{LEAGUE_ID}/users"][1]["metadata"] = {"team_name": OTHER_DISPLAY}
        out, err, exc = self.run_script()
        self.assertIsNone(exc, f"unexpected failure: {exc}")
        self.assertIn(OTHER_DISPLAY, sb.OUT_MD.read_text())   # as a team name, on purpose
        # everything else stays clean
        text = self.observable(out, err, exc)
        for c in (LEAGUE_ID, USERNAME, MY_UID, OTHER_UID, MY_DISPLAY, AVATAR):
            self.assertNotIn(c, text)

    def test_handle_not_used_as_team_name_still_trips(self):
        """Same handle, but leaking through a different path, must still trip."""
        self.api[f"/league/{LEAGUE_ID}"]["name"] = f"League of {OTHER_DISPLAY}"
        out, err, exc = self.run_script()
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("member handle", str(exc))
        self.assertFalse(sb.OUT_MD.exists())

    def test_leak_guard_blocks_a_bad_edit(self):
        """Simulate a future bug that puts a user ID into a team name."""
        self.api[f"/league/{LEAGUE_ID}/users"][1]["metadata"] = {"team_name": f"Team {OTHER_UID}"}
        out, err, exc = self.run_script()
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("Sleeper user id", str(exc))
        self.assertFalse(sb.OUT_MD.exists(), "guard must fire before files are written")
        self.assert_no_canaries(self.observable(out, err, exc))


class TestRepoHygiene(unittest.TestCase):
    """Static checks on the files that will be committed."""

    def test_no_identifiers_in_source_or_workflow(self):
        for name in ("sleeper_brief.py", ".github/workflows/league-state.yml", "README.md"):
            text = (HERE / name).read_text()
            self.assertIsNone(re.search(r"\b\d{15,}\b", text), f"{name} contains a long numeric ID")
            self.assertNotIn("vars.LEAGUE_ID", text)
            self.assertNotIn("vars.SLEEPER_USERNAME", text)

    def test_workflow_uses_secrets_and_scoped_permissions(self):
        wf = (HERE / ".github/workflows/league-state.yml").read_text()
        self.assertIn("secrets.LEAGUE_ID", wf)
        self.assertIn("secrets.SLEEPER_USERNAME", wf)
        self.assertIn("permissions:\n  contents: write", wf)
        self.assertNotIn("pull_request", wf)   # no fork-triggered runs

    def test_script_never_prints_env_values(self):
        """No print() or sys.exit() line may interpolate the secrets."""
        for line in (HERE / "sleeper_brief.py").read_text().splitlines():
            if "print(" in line or "sys.exit(" in line:
                self.assertNotIn("{league_id", line, line)
                self.assertNotIn("{username", line, line)
                self.assertNotIn("{my_uid", line, line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
