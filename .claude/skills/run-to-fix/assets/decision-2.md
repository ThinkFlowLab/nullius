# Stop 2 of {{ROUND}}: what to run next

Build: {{BUILD}}. Trial runs on it: {{TRIAL_JOB}}, {{TRIALS}} trials. Spent in this round: {{SPENT}}. Budget left:
{{LEFT}} of {{BUDGET}}.

| tasks | trials passed before | trials passed in the trial run | test |
|---|---|---|---|
{{RESULTS}}

| option | what runs | price | command |
|---|---|---|---|
{{OPTIONS}}

Prices use the trial run's cost per trial, {{PER_TRIAL}}.

## Answer

```
python3 loop.py answer --loop {{LOOP}} --stop 2 --choice stop|rerun|full --text "the person's words"
```
