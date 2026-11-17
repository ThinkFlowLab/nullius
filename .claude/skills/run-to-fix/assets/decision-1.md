# Stop 1 of {{ROUND}}: the candidates to keep

{{SUMMARY}}

| candidate | cause | trials the cause explains | test before → after the fix | suite failures added | replay before → after | false successes added | diff |
|---|---|---|---|---|---|---|---|
{{ROWS}}

Replay counts are of the {{TRIALS}} recorded trials: done and passed / done and failed / not done and passed / not
done and failed. A false success is a failed trial that the agent reports as a success.

## Checks that did not run

{{NOT_RUN}}

## Answer

```
python3 loop.py answer --loop {{LOOP}} --stop 1 --keep IDS --text "the person's words"
```
