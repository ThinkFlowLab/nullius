# Security

If you think you have found a security problem in Nullius, please tell us
privately rather than opening a public issue. Write to
**nullius.thinkflowlab@proton.me** with what you found, how to reproduce it and
what you believe the impact is. A proof of concept is welcome; a complete
exploit is not needed.

We will acknowledge your report within a few days, keep you informed while we
look into it and let you know when a fix is out. We will not take action
against anyone who reports in good faith and gives us reasonable time to fix
the problem before talking about it publicly.

Things we would especially like to hear about:

- A run getting at a provider key or host credential it should not have, or a
  key ending up in a log, a job tree or a generated recipe.
- A benchmark, agent or capability definition in the catalogue escaping the
  environment it is meant to run in.
- Anything reachable from the web interface that writes outside its workspace
  or the trial's own `agent/` directory.

Nullius has no bug bounty programme. Credit goes to reporters who want it.
