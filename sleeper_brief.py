#!/usr/bin/env python3
"""
sleeper_brief.py

Pulls the current state of one Sleeper league and writes two files the
morning brief can fetch:

  league.md     human-readable, names resolved, what the brief reads
  league.json   same data, machine-readable, for a future app
  scorecard.md  season-to-date record of how the team and the process
                performed week by week: result, points, optimal lineup,
                points left on the bench, waiver outcomes, trades
  history/week-NN-lineup.md  the league file as it stood on Sunday
                morning before the 1:00 pm lock, one per week, so there
                is a record of what was actually started

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
OUT_SCORE = ROOT / "scorecard.md"
HISTORY = ROOT / "history"

KEEP_POS = {"QB", "RB", "WR", "TE", "K", "DEF"}
ET = ZoneInfo("America/New_York")
WAIVER_TYPES = {0: "rolling priority", 1: "reverse standings", 2: "FAAB"}
FLEX_ELIGIBLE = {
    "FLEX": {"RB", "WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "REC_FLEX": {"WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
}


def now_et():
    """Wrapped so tests can pin the clock."""
    return datetime.now(ET)


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
    appears in the output. `sensitive` maps a category label to a set of
    values. The message names the category only, never the value."""
    for category, values in sensitive.items():
        for value in values:
            if value and len(str(value)) >= 3 and str(value) in text:
                raise RuntimeError(f"leak guard tripped: {category} reached the output")
    if SNOWFLAKE.search(text):
        raise RuntimeError("leak guard tripped: a long numeric ID reached the output")


def optimal_points(slot_labels, player_ids, points_by_id, players):
    """Best possible score from a roster given the lineup slots.
    Fixed slots are filled greedily by position, then flex slots from
    what remains. Exact for a single flex, near-exact otherwise."""
    pool = {pid: points_by_id.get(pid, 0.0) for pid in player_ids}
    pos_of = {pid: (players.get(pid) or {}).get("pos") for pid in pool}
    used, total = set(), 0.0
    fixed = [x for x in slot_labels if x not in FLEX_ELIGIBLE]
    flex = [x for x in slot_labels if x in FLEX_ELIGIBLE]
    for slot in fixed:
        best = max((pid for pid in pool if pid not in used and pos_of[pid] == slot),
                   key=lambda pid: pool[pid], default=None)
        if best:
            used.add(best)
            total += pool[best]
    for slot in flex:
        elig = FLEX_ELIGIBLE[slot]
        best = max((pid for pid in pool if pid not in used and pos_of[pid] in elig),
                   key=lambda pid: pool[pid], default=None)
        if best:
            used.add(best)
            total += pool[best]
    return round(total, 2)


def build_scorecard(league_id, week, mine, rosters_by_id, users_by_id, slot_labels,
                    players, waiver_type):
    """Season-to-date weekly record for my team. Re-derived from the API on
    every run, so it needs no stored state and self-heals if a week was
    missed. Returns (markdown, rows)."""
    my_rid = mine["roster_id"]
    n_teams = len(rosters_by_id)
    today = now_et()
    rows = []
    for w in range(1, week + 1):
        # The current week only counts once its games are over.
        if w == week and today.strftime("%a") not in ("Tue", "Wed"):
            continue
        matchups = get(f"/league/{league_id}/matchups/{w}") or []
        by_rid = {m["roster_id"]: m for m in matchups}
        me = by_rid.get(my_rid)
        if not me or not (me.get("points") or 0):
            continue
        opp = next((m for m in matchups
                    if m.get("matchup_id") == me.get("matchup_id")
                    and m["roster_id"] != my_rid), None)
        my_pts = round(me.get("points") or 0, 2)
        opp_pts = round((opp or {}).get("points") or 0, 2)
        result = "W" if my_pts > opp_pts else "L" if my_pts < opp_pts else "T"
        opt = optimal_points(slot_labels, me.get("players") or [],
                             me.get("players_points") or {}, players)
        left = round(max(opt - my_pts, 0), 2)
        all_pts = sorted((m.get("points") or 0 for m in matchups), reverse=True)
        rank = all_pts.index(me.get("points") or 0) + 1 if all_pts else 0
        opp_name = team_label(rosters_by_id[opp["roster_id"]], users_by_id) \
            if opp and opp["roster_id"] in rosters_by_id else "bye"

        won = lost = 0
        spent = 0
        trades = 0
        for tx in get(f"/league/{league_id}/transactions/{w}") or []:
            if my_rid not in (tx.get("roster_ids") or []):
                continue
            if tx.get("type") == "waiver":
                if tx.get("status") == "complete":
                    won += 1
                    spent += (tx.get("settings") or {}).get("waiver_bid") or 0
                else:
                    lost += 1
            elif tx.get("type") == "trade" and tx.get("status") == "complete":
                trades += 1
        rows.append({
            "week": w, "result": result, "my": my_pts, "opp": opp_pts,
            "opp_name": opp_name, "optimal": opt, "left": left,
            "rank": rank, "won": won, "lost": lost, "spent": spent, "trades": trades,
        })

    md = ["# Scorecard: season to date",
          f"Updated {today.strftime('%A %B %d, %Y %I:%M %p ET')}. Rebuilt from Sleeper every run.",
          ""]
    if not rows:
        md.append("No completed weeks yet.")
        return "\n".join(md) + "\n", rows

    wins = sum(r["result"] == "W" for r in rows)
    losses = sum(r["result"] == "L" for r in rows)
    ties = sum(r["result"] == "T" for r in rows)
    tot_my = sum(r["my"] for r in rows)
    tot_opt = sum(r["optimal"] for r in rows)
    eff = round(100 * tot_my / tot_opt, 1) if tot_opt else 0
    claims = sum(r["won"] for r in rows) + sum(r["lost"] for r in rows)
    hit = f"{sum(r['won'] for r in rows)}/{claims}" if claims else "0/0"
    md.append(f"Record {wins}-{losses}" + (f"-{ties}" if ties else "")
              + f" · {tot_my:.1f} pts scored · {tot_opt:.1f} optimal"
              + f" · lineup efficiency {eff}%"
              + f" · avg left on bench {sum(r['left'] for r in rows) / len(rows):.1f}"
              + f" · waiver claims won {hit}"
              + (f" · FAAB spent ${sum(r['spent'] for r in rows)}" if waiver_type == "FAAB" else "")
              + f" · trades {sum(r['trades'] for r in rows)}")
    md.append("")
    md.append(f"| Week | Result | Me | Opponent | Optimal | Left on bench | Pts rank (of {n_teams}) | Waivers won/lost | Trades |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        md.append(f"| {r['week']} | {r['result']} vs {r['opp_name']} | {r['my']} | {r['opp']} | "
                  f"{r['optimal']} | {r['left']} | {r['rank']} | {r['won']}/{r['lost']}"
                  + (f" (${r['spent']})" if waiver_type == "FAAB" and r['won'] else "")
                  + f" | {r['trades']} |")
    md.append("")
    md.append("Left on bench is optimal minus actual: the cost of start/sit calls that week. "
              "Lineup efficiency is actual divided by optimal for the season. "
              "Points rank is where my score landed among all teams that week.")
    return "\n".join(md) + "\n", rows


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

    now = now_et()
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

    score_text, _ = build_scorecard(league_id, week, mine, rosters_by_id, users_by_id,
                                    slot_labels, players, waiver_type)

    # Leak guard: refuse to write anything that carries an identifier.
    # Display names are excluded when a member has made their team name
    # identical to their handle; that string is going in the file as a
    # team name by their own choice, and the guard must not block the run.
    team_names_lower = {
        ((u.get("metadata") or {}).get("team_name") or "").lower() for u in users
    }
    handles = set()
    for u in users:
        for key in ("display_name", "username"):
            h = u.get(key)
            if h and h.lower() not in team_names_lower:
                handles.add(h)
    sensitive = {
        "the league id": {league_id},
        "the configured user secret": {username},
        "a Sleeper user id": {my_uid} | set(users_by_id),
        "a member handle": handles,
    }
    assert_clean(md_text + json_text + score_text, sensitive)

    OUT_MD.write_text(md_text)
    OUT_JSON.write_text(json_text)
    OUT_SCORE.write_text(score_text)

    # Sunday-morning snapshot: the last look at the lineup before 1:00 pm lock.
    if now.strftime("%a") == "Sun" and now.hour < 13:
        HISTORY.mkdir(exist_ok=True)
        (HISTORY / f"week-{week:02d}-lineup.md").write_text(md_text)

    print(f"Wrote {OUT_MD.name}, {OUT_JSON.name}, and {OUT_SCORE.name} for week {week}.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"Run failed: {e}")
