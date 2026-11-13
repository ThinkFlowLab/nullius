# Sandbox

Where agents run apart from this machine: Docker or Podman here, and Boat VMs. Under **Execution**.

| Card | Does |
| --- | --- |
| Engine | Docker or Podman: whether it answers, its name and version; why not when it does not |
| Boat | The API key of the [Boat backend](../standard.md#the-boat-backend): Edit saves it to the owner-only store, Remove deletes it; it is never shown again. `BOAT_API_KEY` in the environment wins |

| Rule | |
| --- | --- |
| Hosts | This machine only; a host keeps its own login and images |
| Code | `app/nullius/sandbox.py`, `app/nullius/standard/boat.py` |
