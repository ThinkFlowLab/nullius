# Stop 1 of {{ROUND}}: the candidates to keep

{{SUMMARY}}

| candidate | cause | effect | trials of the cause | test before → after the fix | suite failures added | replay before → after (refs) | trials of the cause whose label changes | false successes added | diff |
|---|---|---|---|---|---|---|---|---|---|
{{ROWS}}

Replay counts are of the {{TRIALS}} recorded trials: done and passed / done and failed / not done and passed / not
done and failed. A false success is a failed trial that the agent reports as a success. A trial that keeps its
recorded label is one the replay could not reproduce; its label comes from the recorded run.

## Candidates whose fix changes no label of their cause

{{UNCHANGED}}

## Checks that did not run

{{NOT_RUN}}

## Answer

```
python3 loop.py answer --loop {{LOOP}} --stop 1 --keep IDS --text "the person's words"
```
