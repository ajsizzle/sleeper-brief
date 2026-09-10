# sleeper-brief

Pulls the current state of one [Sleeper](https://sleeper.com) fantasy football
league once a day and commits two files that a morning brief can fetch:

| File | What it is |
| --- | --- |
| `league.md` | Human-readable. Player IDs resolved to names, positions, teams, injury tags. |
| `league.json` | The same data, machine-readable, for a future app. |

Each refresh covers your roster and lineup, the current week's matchup and the
opponent's starters, every team by record, waiver and FAAB position,
transactions for the current and previous week, and the trending adds across
Sleeper flagged by whether they are available in your league.

Standard library only. No dependencies, no API key — the Sleeper endpoints used
here are public and read-only.

## What is not in the output

The repository is public, so the script is built so that the committed files
carry league activity and nothing that identifies a person:

- Teams are labelled by team name, falling back to `Team N` — never by the
  owner's display name or handle.
- No Sleeper user IDs, usernames, display names, or avatar hashes are written.
- The league ID is never written. A user ID is enough to list every league that
  person plays in, which is why it stays out.

This is enforced rather than assumed. Before anything is written, `assert_clean`
scans the finished text for every known identifier and for any 15-digit-or-longer
number — the shape of a Sleeper snowflake ID — and aborts the run if one appears.
The error message never repeats the offending value.

`test_sleeper_brief.py` feeds the script fake data whose IDs and names are
distinctive canaries, then asserts that none of them reach `league.md`,
`league.json`, stdout, stderr, or any exception message. The workflow runs the
suite *before* the script, so an edit that starts leaking fails the job while the
working tree is still clean.

## Setup

The two configuration values are stored as repository secrets, so they are
masked in the public workflow logs.

Under **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
| --- | --- |
| `LEAGUE_ID` | The numeric league ID from your Sleeper league URL. |
| `SLEEPER_USERNAME` | Your Sleeper username, used to find which roster is yours. |

They must be **secrets**, not variables — repository variables are shown in
plain text in the logs.

Your league URL looks like `https://sleeper.com/leagues/<league id>/team`; the
league ID is the segment after `/leagues/`.

## Running it

The workflow runs daily at 11:00 UTC, and can be started by hand from the
**Actions** tab with **Run workflow**. It commits `league.md` and `league.json`
only when they have actually changed.

To run it locally:

```bash
LEAGUE_ID=... SLEEPER_USERNAME=... python3 sleeper_brief.py
```

And the tests:

```bash
python3 -m unittest -v test_sleeper_brief
```

Python 3.9 or newer, for `zoneinfo`.

## Notes

`players_trim.json` is a local cache of Sleeper's 5MB player file, trimmed to the
scoring positions and refreshed at most once every 24 hours, which is what
Sleeper asks of clients. It is cached between workflow runs rather than
committed, and is git-ignored.

Timestamps are US Eastern. Injury tags in brackets are Sleeper's own
designations and can lag official reports.
