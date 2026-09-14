# sleeper-brief

A one-script data pipe, about 400 lines. Three times a day, ahead of each brief run, a GitHub Actions cron pulls the current state of your Sleeper league, resolves player IDs to names, and commits the refreshed files. The brief fetches that file first, then does its research.

No server, no keys, no cost. Sleeper's API is public and read-only.

## What it produces

`league.md`, rewritten every run:

- Your roster as it is right now, split into starters as currently set, bench, and IR, with Sleeper's injury tags
- This week's opponent, their starters and bench
- Every other team, ordered by record, with waiver priority
- All waiver claims, pickups, and trades from this week and last, with FAAB bids and failure notes
- The 25 most added players across Sleeper in the last 24 hours, each marked available in your league, on your roster, or rostered by whom

`league.json` holds the same data for a future app. `players_trim.json` is the ID-to-name cache, refreshed at most once a day.

## Setup, about 15 minutes

1. **Create the repo.** Public, named `sleeper-brief`, under `~/Developer/` so it commits as your personal account. Public matters: the brief fetches the raw file URL, and raw URLs on private repos need a token the scheduled task can't hold. See "What is and isn't exposed" below before you decide.

2. **Add the files** from this folder: `sleeper_brief.py`, `test_sleeper_brief.py`, `.github/workflows/league-state.yml`, and this README, plus a `.gitignore` for `__pycache__` and `.DS_Store`. Commit and push.

3. **Add two repository secrets.** Repo Settings, then Secrets and variables, then Actions, then the Secrets tab (not Variables; secrets are masked in the public workflow logs, variables are not):
   - `LEAGUE_ID`: the long number in your league URL in the Sleeper app
   - `SLEEPER_USERNAME`: your Sleeper username, not your display name

4. **Run it once by hand.** Actions tab, select `league-state`, click Run workflow. The first run pulls the 5MB players file and takes about a minute. When it finishes, `league.md` should be in the repo with real names in it. Open it and confirm your roster is the one under "My team."

5. **Point the brief at it.** In the scheduled task prompt in Cowork, replace the roster line with the block below and leave everything else as is.

## The line for the brief prompt

```
League data: before any research, fetch
https://raw.githubusercontent.com/ajsizzle/sleeper-brief/main/league.md
That file is the source of truth for my current roster, my opponent this week, every other team's roster, this week's waiver and trade activity, and which trending players are actually available in my league. Use it instead of any roster written in this prompt. Include my opponent's starters in the news search. When suggesting waiver targets, only name players the file marks as available. When suggesting a trade, name the specific team, what they need based on their roster, and what I would send. If the fetch fails, say so in one line and fall back to this roster: QB Drake Maye. RB Chase Brown, Kenneth Walker III, Rhamondre Stevenson, Emmett Johnson, Mike Washington Jr., Tahj Brooks. WR A.J. Brown, Mike Evans, Rome Odunze, Xavier Worthy. TE George Kittle, Mark Andrews. K Cameron Dicker. DST Houston.
```

The fallback roster line is the only thing left to maintain by hand, and only matters if GitHub is down at 8am.

## What is and isn't exposed

The repo is public, so assume anyone can read the code, the output files, and the Actions logs. The script is built so that none of this identifies you or your league mates:

| In the repo | Exposed? |
| --- | --- |
| Your league ID and Sleeper username | No. Stored as masked secrets, never printed, never written to output. Error messages and network failures are rewritten so the URL never reaches the log. |
| Sleeper user IDs | No. Never written. A user ID would let anyone list every league that person plays in. |
| Owner display names or usernames | Not on their own. Output uses team names only, falling back to "Team N". One exception: a member who has set their team name to their own handle will see that handle appear, as their team name. See the leak guard below. |
| Team names, rosters, records, waiver priority, FAAB used, transactions | Yes. This is the point of the file. It is the same information anyone with your league link already sees. |
| Your GitHub handle | Yes, it is your repo. |
| Player names and injury tags | Yes, public NFL data. |

Two things only you can check: if your Sleeper team name is your real name, that will appear, so rename the team in Sleeper if you care. And the same goes for league mates' team names, which you don't control.

If you ever paste a league ID or username into the code, a commit, or a workflow file, it becomes public history even after you delete it. Keep them in secrets.

## Tests and guards

Three layers, each independent of the others:

1. **`test_sleeper_brief.py`** (16 tests, standard library, `python -m unittest -v`). Feeds the script fake league data seeded with canary values for the league ID, username, Sleeper user IDs, display names, and avatar hashes, then asserts none of them appear in `league.md`, `league.json`, the player cache, stdout, stderr, or any error message. It also covers every failure path (missing secrets, wrong league, wrong user, HTTP 500, network down), checks that a deliberately injected user ID trips the guard before any file is written, and statically checks that the workflow uses secrets rather than variables and that no `print` or `sys.exit` line interpolates a secret. The workflow runs the suite first and stops if anything fails.
2. **In-script leak guard.** Before writing, the script scans its own output for the league ID, the configured user secret, every Sleeper user ID it saw, every member handle, and any 15-digit-or-longer number, and refuses to write if it finds one. The failure message names the category that tripped, never the value. One deliberate exception: a member who has set their team name to their own handle has chosen to publish it as a team name, so that string is allowed through as a team name only.
3. **Workflow leak scan.** After the script runs, a shell step greps the three generated files for the secret values and for long numeric IDs, and fails the job before the commit step.

Run the tests locally any time you change the script: `python -m unittest -v` from the repo root.

**One more setting worth flipping.** Repo Settings, then Code security, then turn on Secret scanning and Push protection. Both are free on public repos. Push protection blocks a commit that contains a recognizable credential before it reaches GitHub, which is the mistake this whole section is guarding against.

## Known limits

- **Schedule drift.** GitHub cron can run a few minutes late under load. The 45-minute gap ahead of each brief covers it.
- **Sixty-day rule.** GitHub pauses scheduled workflows in a repo with no activity for 60 days. The bot's own commits should keep it alive; if the brief ever reports a stale file, open the Actions tab and re-enable the workflow.
- **Week number.** The script uses Sleeper's own current week, which flips to the next week early in the week, so Tuesday runs already show next week's matchup. That is what you want for waivers.
- **Injury tags** come from Sleeper's player file and can lag the official report by hours. The brief's own news search is the authority; the tag is a hint.
- **Read only.** Nothing here can set a lineup, submit a claim, or send a trade. You still tap those in Sleeper.

## Later, if you build the app

`league.json` is already the shape a backend would want. The script becomes a Supabase Edge Function or stays exactly where it is and the app reads the JSON from the same raw URL.
