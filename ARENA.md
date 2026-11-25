# Arena: compete on branches, merge the winners

```
main ─────────●────────────●──────────   Joe's live version (only Joe merges)
               \            ↑ merge PR that wins
agent/claude    ●──●──●─────┘
agent/codex     ●──●──●──●            each agent: own branch, own clone, own port, own data
```

## Setting up an agent
1. `git clone <repo> C:\Users\<user>\arena\<name>` and `git switch -c agent/<name>`.
2. Make a venv in `gexwalls\` (see README) and copy `alerts_config.example.json` → `alerts_config.json`.
3. Run it on its own port: `.venv\Scripts\python.exe walls_app.py --no-browser --port <port>`
   (claude 8051, codex 8052, gemini 8053, copilot 8054, grok 8055, opencode 8056, hermes 8057).
   It writes its caches and picks next to its own `walls_app.py`, so versions never share data.
4. Daily, staggered by 20 min per agent after 16:30 ET (CBOE limits):
   `set GEXWALLS_URL=http://127.0.0.1:<port>` then `.venv\Scripts\python.exe audit\audit.py`.
   Each clone's `audit\picks_log.jsonl` becomes that version's own forward-tested track record.

## Scoring (same yardstick for everyone)
`python arena\score.py C:\Users\<user>\arena\* C:\Users\<user>\Documents\GEXWalls` prints a table:
- **Audit score** (0-100) from the latest `audit\history.jsonl` line, and failed checks.
- **Graded picks** (counted = pays >= 10%): number closed, independent ticker/expiry clusters, win rate vs predicted,
  **Brier score** (lower = better calibrated), average P/L % of capital.
Trust a pick comparison only after >= 20 independent clusters per version, ideally over the same weeks.
Scores are recorded in `arena/RESULTS.md` (append; never edit old rows).

## Merging
An agent opens a PR from `agent/<name>` into `main` when its version beats `main` on the scorecard without failing
a check `main` passes. Joe (or an agent Joe asks) reviews, merges, and deploys to the live folder with the backup →
test → SHA-256 → restart → re-audit → rollback procedure in `gexwalls/audit/audit_prompt.md` step 5. Other agents
then `git merge main` into their branch to stay comparable.
