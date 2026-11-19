# Stop 2 of {{ROUND}}: what to run next

Build: {{BUILD}}. Code of the trial runs: {{CODE}}. Trial runs on it: {{TRIAL_JOB}}, {{TRIALS}} trials. Spent in this round: {{SPENT}}. Budget left:
{{LEFT}} of {{BUDGET}}.

| tasks | trials passed before | trials passed in the trial run | test |
|---|---|---|---|
{{RESULTS}}

| option | what runs | price | command |
|---|---|---|---|
{{OPTIONS}}
{{REPORT_NOTE}}

Each task is priced at the mean cost of its trials in this round's trial runs on this build, else in the run that
was read, times the trials it gets. A task with no recorded cost takes the mean of the others.

## Answer

```
python3 loop.py answer --loop {{LOOP}} --stop 2 --choice stop|rerun|full --text "the person's words"
```
