# Staging A/B runs for launcher changes

`scripts/stage_ab.py` replays finished read and review jobs twice, once with the control
launcher and once with the treatment launcher, and compares tokens, requests and verdicts.
Runs go through the live proxy (so Morph's shared limits stay governed) but never through the
live broker: each run gets its own `AGENTQ_HOME` under `~/.cache/agentq-staging/<run>/home`,
a throwaway worktree at `origin/main`, and the job's tier model.

Jobs whose prompt mentions production access, pushing or merging are skipped. Pick jobs with
`--job-ids` or let the script choose recent ones with `--jobs N`.

```bash
python3 scripts/stage_ab.py \
  --control-code ~/path/to/main/tools/agent-dash \
  --treatment-code ~/path/to/branch/tools/agent-dash \
  --treatment-env AGENTQ_TOOL_OUTPUT_CAP=8000 \
  --job-ids <id>,<id> --concurrency 2 --timeout-min 40 --out /tmp/stage-ab
```

`--dry-run` prints the chosen jobs and commands. Results land in `<out>/report.md` and
`<out>/results.tsv`.

## Results so far

Bash-output trim (`agentq/pi-ext/trim-tool-output.ts`, cap 8,000 characters), 2026-11-14,
8 replayed jobs that had produced long bash output: the trim fired in every treatment run and
cut context per request in most of them, but treatment runs took more turns (409 vs 284
requests) and used 27% more tokens in total; verdicts agreed in 7 of 8 pairs. Run-to-run
variance is large at this sample size. The trim stays opt-in (`AGENTQ_TOOL_OUTPUT_CAP=<n>`).
