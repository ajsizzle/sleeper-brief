#!/usr/bin/env python3
"""
sleeper_brief.py

Pulls the current state of one Sleeper league and writes two files the
morning brief can fetch:

  league.md    human-readable, names resolved, what the brief reads
  league.json  same data, machine-readable, for a future app

Standard library only. Configuration comes from two environment variables:

  LEAGUE_ID          the numeric league id from the Sleeper league URL
  SLEEPER_USERNAME   your Sleeper username (used to find "my" roster)

Both are stored as GitHub Actions secrets so they are masked in the public
workflow logs, and the script never prints them or the URLs it calls.

Output contains team names, rosters, and league activity only. No Sleeper
user IDs, usernames, display names, or the league ID are written to the
output files, since a user ID lets anyone list every league that person
plays in.

Sleeper's API is read-only and needs no key. Player IDs are resolved
through a trimmed local cache (players_trim.json) that is refreshed at
most once a day, per Sleeper's guidance for the 5MB players file.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://api.sleeper.app/v1"
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "players_trim.json"
OUT_MD = ROOT / "league.md"
OUT_JSON = ROOT / "league.json"

KEEP_POS = {"QB", "RB", "WR", "TE", "K", "DEF"}
ET = ZoneInfo("America/New_York")
WAIVER_TYPES = {0: "rolling priority", 1: "reverse standings", 2: "FAAB"}


# ---------------------------------------------------------------- helpers

def get(path):
    """GET a Sleeper endpoint and return parsed JSON (None on 404)."""
    req = urllib.request.Request(
        BASE + path, headers={"User-Agent": "sleeper-brief/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"Sleeper returned HTTP {e.code} for a {path.split('/')[1]} request") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Sleeper ({e.reason})") from None


def load_players(needed_ids):
    """Return {player_id: {name, pos, team, inj}} from cache, refreshing
    the cache if it is missing, older than 24h, or lacks an id we need."""
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text())
            age_h = (time.time() - cache.get("_fetched", 0)) / 3600
            players = cache.get("players", {})
            missing = [i for i in needed_ids if i not in players]
            if age_h < 24 and not missing:
                return players
        except (ValueError, KeyError):
            pass

    raw = get("/players/nfl") or {}
    players = {}
    for pid, p in raw.items():
        pos = p.get("position")
        if pos not in KEEP_POS:
            continue
        name = p.get("full_name") or " ".join(
            x for x in (p.get("first_name"), p.get("last_name")) if x
        ).strip() or pid
        players[pid] = {
            "name": name,
            "pos": pos,
            "team": p.get("team") or "FA",
            "inj": p.get("injury_status") or "",
        }
    CACHE.write_text(
        json.dumps({"_fetched": time.time(), "players": players},
                   separators=(",", ":"))
    )
    return players


SNOWFLAKE = re.compile(r"\d{15,}")


def assert_clean(text, sensitive):
    """Raise if any identifier or any 15+ digit number (Sleeper user and
    league IDs are 18-digit snowflakes; player IDs are under 6 digits)
    appears in the output. The message never repeats the value."""
    for value in sensitive:
        if value and len(str(value)) >= 3 and str(value) in text:
            raise RuntimeError("leak guard tripped: an identifier reached the output")
    if SNOWFLAKE.search(text):
        raise RuntimeError("leak guard tripped: a long numeric ID reached the output")


def fmt(pid, players):
    if pid in (None, "0", ""):
        return "(empty)"
    p = players.get(pid)
    if not p:
        return f"Unknown player {pid}"
    inj = f" [{p['inj']}]" if p["inj"] else ""
    return f"{p['name']} ({p['pos']}, {p['team']}){inj}"


def team_label(roster, users_by_id):
    """Team name only. Falls back to 'Team N' rather than the owner's
    display name so no personal handles land in the public output."""
    u = users_by_id.get(roster.get("owner_id"), {})
    team = (u.get("metadata") or {}).get("team_name") or f"Team {roster.get('roster_id')}"
    return team


def record(roster):
    s = roster.get("settings") or {}
    pts = s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100
    return s.get("wins", 0), s.get("losses", 0), s.get("ties", 0), pts


# ------------------------------------------------------------------ main

def main():
    league_id = os.environ.get("LEAGUE_ID")
    username = os.environ.get("SLEEPER_USERNAME")
    if not league_id or not username:
        sys.exit("Set LEAGUE_ID and SLEEPER_USERNAME environment variables.")

    state = get("/state/nfl") or {}
    week = int(state.get("week") or state.get("display_week") or 1)
    season = state.get("season", "")

    league = get(f"/league/{league_id}")
    if not league:
        sys.exit("League not found. Check the LEAGUE_ID secret.")
    rosters = get(f"/league/{league_id}/rosters") or []
    users = get(f"/league/{league_id}/users") or []
    matchups = get(f"/league/{league_id}/matchups/{week}") or []
    transactions = []
    for w in (week, week - 1):
        if w >= 1:
            transactions += get(f"/league/{league_id}/transactions/{w}") or []
    trending = get("/players/nfl/trending/add?lookback_hours=24&limit=25") or []

    me = get(f"/user/{username}")
    if not me:
        sys.exit("Sleeper user not found. Check the SLEEPER_USERNAME secret.")
    my_uid = me["user_id"]

    users_by_id = {u["user_id"]: u for u in users}
    rosters_by_id = {r["roster_id"]: r for r in rosters}

    # Which ids do we need names for?
    needed = set()
    for r in rosters:
        needed.update(r.get("players") or [])
    needed.update(t["player_id"] for t in trending)
    for t in transactions:
        needed.update((t.get("adds") or {}).keys())
        needed.update((t.get("drops") or {}).keys())
    needed.discard("0")
    players = load_players(needed)

    # My roster (owner or co-owner)
    mine = next(
        (r for r in rosters
         if r.get("owner_id") == my_uid or my_uid in (r.get("co_owners") or [])),
        None,
    )
    if not mine:
        sys.exit("No roster in this league belongs to that Sleeper user.")

    # Who is rostered where (for trending availability)
    rostered = {}
    for r in rosters:
        for pid in r.get("players") or []:
            rostered[pid] = r["roster_id"]

    # Matchup lookup
    matchup_by_roster = {m["roster_id"]: m for m in matchups}
    my_matchup = matchup_by_roster.get(mine["roster_id"])
    opponent = None
    if my_matchup and my_matchup.get("matchup_id") is not None:
        for m in matchups:
            if (m.get("matchup_id") == my_matchup["matchup_id"]
                    and m["roster_id"] != mine["roster_id"]):
                opponent = rosters_by_id.get(m["roster_id"])
                break

    slot_labels = [p for p in league.get("roster_positions", []) if p != "BN"]
    settings = league.get("settings") or {}
    waiver_type = WAIVER_TYPES.get(settings.get("waiver_type"), "unknown")
    faab = settings.get("waiver_budget")

    def starters_block(roster):
        starters = roster.get("starters") or []
        lines = []
        for i, pid in enumerate(starters):
            label = slot_labels[i] if i < len(slot_labels) else "SLOT"
            lines.append(f"- {label}: {fmt(pid, players)}")
        return lines

    def bench_list(roster):
        starters = set(roster.get("starters") or [])
        reserve = set(roster.get("reserve") or [])
        return [fmt(p, players) for p in (roster.get("players") or [])
                if p not in starters and p not in reserve]

    def ir_list(roster):
        return [fmt(p, players) for p in (roster.get("reserve") or [])]

    now = datetime.now(ET)
    md = []
    md.append("# Sleeper league state")
    md.append(
        f"Generated {now.strftime('%A %B %d, %Y %I:%M %p ET')} · "
        f"Season {season} · NFL week {week}"
    )
    md.append(
        f"League: {league.get('name')} · {league.get('total_rosters')} teams · "
        f"waivers: {waiver_type}"
        + (f" (budget ${faab})" if waiver_type == "FAAB" and faab is not None else "")
        + " · lineup: " + ", ".join(slot_labels)
    )
    md.append("")

    # ---- my team
    team = team_label(mine, users_by_id)
    w, l, t, pts = record(mine)
    ms = mine.get("settings") or {}
    md.append(f"## My team: {team} (roster {mine['roster_id']})")
    md.append(
        f"Record {w}-{l}" + (f"-{t}" if t else "") +
        f" · {pts:.1f} pts · waiver priority {ms.get('waiver_position', '?')}"
        + (f" · FAAB used ${ms.get('waiver_budget_used', 0)}" if waiver_type == "FAAB" else "")
    )
    md.append(f"### Starters as currently set (week {week})")
    md += starters_block(mine)
    md.append("### Bench")
    md += [f"- {x}" for x in bench_list(mine)] or ["- (none)"]
    ir = ir_list(mine)
    if ir:
        md.append("### IR")
        md += [f"- {x}" for x in ir]
    md.append("")

    # ---- matchup
    if opponent:
        oteam = team_label(opponent, users_by_id)
        ow, ol, ot, opts = record(opponent)
        md.append(f"## Week {week} matchup: vs {oteam} · {ow}-{ol}")
        md.append("### Their starters")
        md += starters_block(opponent)
        md.append("### Their bench")
        md += [f"- {x}" for x in bench_list(opponent)] or ["- (none)"]
    else:
        md.append(f"## Week {week} matchup: not available yet")
    md.append("")

    # ---- every team
    md.append("## All teams (by record)")
    ordered = sorted(rosters, key=lambda r: (-record(r)[0], -record(r)[3]))
    for r in ordered:
        tname = team_label(r, users_by_id)
        w, l, t, pts = record(r)
        rs = r.get("settings") or {}
        tag = " (me)" if r["roster_id"] == mine["roster_id"] else ""
        md.append(f"### {tname}{tag} · {w}-{l} · {pts:.1f} pts · waiver priority {rs.get('waiver_position', '?')}")
        md.append("Starters: " + "; ".join(fmt(p, players) for p in (r.get("starters") or [])))
        b = bench_list(r)
        md.append("Bench: " + ("; ".join(b) if b else "(none)"))
        irx = ir_list(r)
        if irx:
            md.append("IR: " + "; ".join(irx))
        md.append("")

    # ---- transactions
    md.append(f"## Transactions, weeks {max(1, week - 1)} to {week}, newest first")
    transactions.sort(key=lambda x: x.get("created", 0), reverse=True)
    tx_lines = []
    for tx in transactions:
        teams = ", ".join(
            team_label(rosters_by_id[rid], users_by_id)
            for rid in (tx.get("roster_ids") or []) if rid in rosters_by_id
        )
        adds = ", ".join(
            f"{fmt(pid, players)} to {team_label(rosters_by_id[rid], users_by_id)}"
            for pid, rid in (tx.get("adds") or {}).items() if rid in rosters_by_id
        )
        drops = ", ".join(
            f"{fmt(pid, players)} from {team_label(rosters_by_id[rid], users_by_id)}"
            for pid, rid in (tx.get("drops") or {}).items() if rid in rosters_by_id
        )
        bid = (tx.get("settings") or {}).get("waiver_bid")
        note = (tx.get("metadata") or {}).get("notes")
        when = datetime.fromtimestamp(tx.get("created", 0) / 1000, ET).strftime("%a %b %d %I:%M %p")
        line = f"- Week {tx.get('leg')} · {tx.get('type')} · {tx.get('status')} · {when} · {teams}"
        if adds:
            line += f" · added: {adds}"
        if drops:
            line += f" · dropped: {drops}"
        if bid is not None:
            line += f" · bid ${bid}"
        if tx.get("draft_picks"):
            line += f" · picks moved: {len(tx['draft_picks'])}"
        if note:
            line += f" · note: {note}"
        tx_lines.append(line)
    md += tx_lines or ["- none"]
    md.append("")

    # ---- trending adds
    md.append("## Trending adds across Sleeper, last 24 hours")
    for t in trending:
        pid = t["player_id"]
        rid = rostered.get(pid)
        if rid is None:
            avail = "AVAILABLE in my league"
        elif rid == mine["roster_id"]:
            avail = "on my roster"
        else:
            avail = f"rostered by {team_label(rosters_by_id[rid], users_by_id)}"
        md.append(f"- {fmt(pid, players)} · {t.get('count', 0):,} adds · {avail}")
    md.append("")
    md.append("Data from the Sleeper API (sleeper.com). Injury tags in brackets are Sleeper's designations and may lag official reports.")

    md_text = "\n".join(md) + "\n"

    # ---- json twin
    def roster_json(r):
        tname = team_label(r, users_by_id)
        w, l, t, pts = record(r)
        return {
            "roster_id": r["roster_id"], "team": tname,
            "record": {"wins": w, "losses": l, "ties": t, "points": pts},
            "waiver_position": (r.get("settings") or {}).get("waiver_position"),
            "faab_used": (r.get("settings") or {}).get("waiver_budget_used"),
            "starters": [fmt(p, players) for p in (r.get("starters") or [])],
            "bench": bench_list(r), "ir": ir_list(r),
        }

    json_text = json.dumps({
        "generated": now.isoformat(), "season": season, "week": week,
        "league": {"name": league.get("name"),
                   "waivers": waiver_type, "faab_budget": faab,
                   "lineup": slot_labels},
        "me": roster_json(mine),
        "opponent": roster_json(opponent) if opponent else None,
        "teams": [roster_json(r) for r in ordered],
        "transactions": [ln[2:] for ln in tx_lines],
        "trending_adds": [
            {"player": fmt(t["player_id"], players), "adds": t.get("count", 0),
             "rostered_by": rostered.get(t["player_id"])} for t in trending
        ],
    }, indent=2)

    # Leak guard: refuse to write anything that carries an identifier.
    sensitive = {league_id, username, my_uid} | set(users_by_id) | {
        u.get("display_name") for u in users if u.get("display_name")
    } | {u.get("username") for u in users if u.get("username")}
    assert_clean(md_text + json_text, sensitive)

    OUT_MD.write_text(md_text)
    OUT_JSON.write_text(json_text)
    print(f"Wrote {OUT_MD.name} and {OUT_JSON.name} for week {week}.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"Run failed: {e}")
