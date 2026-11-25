"""A Nullius server from another machine: its HTTP API (``/api/v1``) as Python, and as ``nullius remote``.

    from nullius.client import Client

    nx = Client("http://bench.internal:5057", token)     # or NULLIUS_SERVER and NULLIUS_TOKEN
    check = nx.check("nightly")                          # a saved recipe by name, or a recipe as a dict
    run = nx.start("nightly", confirm=True)["runs"][0]
    for line in nx.follow(run["id"]):
        print(line)
    job = nx.job(run["job_ids"][0])
    job["adjusted_solve_rate"], job["counts"]

Every method returns the server's JSON document as it is: a job is what
``nullius runs show --json`` prints, a comparison what ``nullius compare
--json`` prints. A refusal raises :class:`ApiError` with the server's sentence,
its status and its document. The token is the server's (``nullius access
create``); no model key is sent or read.

Standard library only, and nothing else of Nullius: this file works copied alone.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator, Optional, Union

ENDED = ("done", "failed", "stopped")       # a run's states that no longer change


class ApiError(Exception):
    """The server refused, or could not be reached: ``status`` (0 when unreachable) and its ``document``."""

    def __init__(self, status: int, message: str, document: Optional[dict] = None):
        super().__init__(message)
        self.status, self.document = status, document or {}


class _Stay(urllib.request.HTTPRedirectHandler):
    """No redirect is followed: it would hand the token to another address, and make a GET of a POST."""

    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, timeout: float = 120.0):
        self.url = (url or os.environ.get("NULLIUS_SERVER") or "").rstrip("/")
        self.token = (token or os.environ.get("NULLIUS_TOKEN") or "").strip()
        self.timeout = timeout
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("the server's address is an http(s) URL: Client(url, token), or NULLIUS_SERVER")
        if not self.token:
            raise ValueError("the server's token is missing: Client(url, token), or NULLIUS_TOKEN")
        if not (self.token.isascii() and self.token.isprintable()):
            raise ValueError("the server's token holds a character no token has")
        self._open = urllib.request.build_opener(_Stay).open

    def request(self, method: str, path: str, body=None, wait: Optional[float] = None, **query):
        """One call: the JSON document the server answers, or :class:`ApiError`. ``wait``: this call's own timeout."""
        pairs = [(k, v) for k, value in query.items() if value is not None and value is not False and value != ""
                 for v in (value if isinstance(value, (list, tuple)) else [1 if value is True else value])]
        url = f"{self.url}/api/v1{path}" + ("?" + urllib.parse.urlencode(pairs) if pairs else "")
        try:
            return self._sent(method, url, body, wait)
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            raise ApiError(0, f"{self.url} did not answer: {getattr(e, 'reason', None) or e or type(e).__name__}") from None

    def _sent(self, method: str, url: str, body, wait):
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                                              **({"Content-Type": "application/json"} if body is not None else {})})
        try:
            with self._open(req, timeout=wait or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            with e:
                try:
                    raw = e.read().decode("utf-8", "replace")
                except (OSError, http.client.HTTPException):
                    raw = ""
            if 300 <= e.code < 400:
                raise ApiError(e.code, f"{self.url} sends this request to {e.headers.get('Location')}: use that server's address") from None
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = {}
            doc = doc if isinstance(doc, dict) else {}
            raise ApiError(e.code, str(doc.get("error") or f"HTTP {e.code} from {self.url}"), doc) from None

    @staticmethod
    def _q(*parts: str) -> str:
        return "".join("/" + urllib.parse.quote(str(p), safe="") for p in parts)

    # what the pages read

    def info(self) -> dict:
        """The server's version, this token's scope and where a run can go."""
        return self.request("GET", "")

    def catalogue(self) -> dict:
        return self.request("GET", "/catalogue")

    def workspaces(self) -> list:
        return self.request("GET", "/workspaces")

    def jobs(self, workspace: Optional[str] = None) -> list:
        return self.request("GET", "/jobs", workspace=workspace)

    def job(self, job_id: str, workspace: Optional[str] = None) -> dict:
        return self.request("GET", self._q("jobs", job_id), workspace=workspace)

    def live(self, job_id: str) -> dict:
        return self.request("GET", self._q("jobs", job_id, "live"))

    def trial(self, job_id: str, trial: str, workspace: Optional[str] = None) -> dict:
        return self.request("GET", self._q("jobs", job_id, "trials", trial), workspace=workspace)

    def conversation(self, job_id: str, trial: str, **query) -> dict:
        """The trial's conversation, paged: ``q``, ``kind``, ``page``, ``step``."""
        return self.request("GET", self._q("jobs", job_id, "trials", trial, "conversation"), **query)

    def trace(self, job_id: str, trial: str) -> dict:
        return self.request("GET", self._q("jobs", job_id, "trials", trial, "trace"))

    def artifacts(self, job_id: str, trial: str) -> list:
        return self.request("GET", self._q("jobs", job_id, "trials", trial, "artifacts"))

    def artifact(self, job_id: str, trial: str, key: str) -> dict:
        return self.request("GET", self._q("jobs", job_id, "trials", trial, "artifacts", key))

    def compare(self, *job_ids: str, matched: bool = False, workspace: Optional[str] = None) -> dict:
        return self.request("GET", "/compare", job=list(job_ids), matched=matched, workspace=workspace)

    # judgements: reading one is free, asking is a paid model call for each trial

    def judgements(self, job_id: str, lane: str = "decision") -> dict:
        """One judge's records for a job: ``judged``, ``trials``, ``records`` by trial."""
        return self.request("GET", self._q("jobs", job_id, "judgements"), lane=lane)

    def judgement(self, job_id: str, trial: str, lane: str = "decision") -> dict:
        return self.request("GET", self._q("jobs", job_id, "trials", trial, "judgement"), lane=lane)

    def judge(self, job_id: str, trial: str, lane: str = "decision") -> dict:
        """Ask a judge (``decision`` or ``llm``) about one trial: the record as stored."""
        return self.request("POST", self._q("jobs", job_id, "trials", trial, "judge"), {"lane": lane}, wait=600)

    def judge_job(self, job_id: str, lane: str = "decision", confirm: bool = False) -> dict:
        """Ask about every trial of the job that judge has not judged. Without ``confirm`` it is refused with
        ``todo``, how many it would ask; the call returns when all are asked."""
        return self.request("POST", self._q("jobs", job_id, "judge"), {"lane": lane, **({"confirm": True} if confirm else {})}, wait=6 * 3600)

    # recipes and runs

    def recipes(self) -> list:
        return self.request("GET", "/recipes")

    def recipe(self, id_or_name: str) -> dict:
        return self.request("GET", self._q("recipes", id_or_name))

    def save_recipe(self, recipe: dict, recipe_id: Optional[str] = None) -> dict:
        return self.request("POST", "/recipes", {"recipe": recipe, "id": recipe_id})

    @staticmethod
    def _run_body(recipe: Union[dict, str], where=None, **more) -> dict:
        return {"recipe": recipe, **({"where": where} if where else {}), **{k: v for k, v in more.items() if v}}

    def check(self, recipe: Union[dict, str], where=None) -> dict:
        """What Review shows for a recipe (a dict, or a saved one's id or name): ``ready``, ``errors``, ``confirm``, ``machines``."""
        return self.request("POST", "/runs/check", self._run_body(recipe, where))

    def start(self, recipe: Union[dict, str], where=None, name: Optional[str] = None, confirm: bool = False) -> dict:
        """Start a recipe; ``runs`` holds one run per machine. ``confirm``: the yes to the question ``check`` returns."""
        return self.request("POST", "/runs", self._run_body(recipe, where, name=name, confirm=confirm))

    def runs(self) -> list:
        return self.request("GET", "/runs")

    def run(self, run_id: str, since: int = 0) -> dict:
        """A run's state and stages, with its log lines from ``since`` on."""
        return self.request("GET", self._q("runs", run_id), since=since or None)

    def stop(self, run_id: str) -> dict:
        return self.request("POST", self._q("runs", run_id, "stop"))

    def continue_(self, run_id: str) -> dict:
        """The answer to a run that paused with a ``question``: go on."""
        return self.request("POST", self._q("runs", run_id, "continue"))

    def follow(self, run_id: str, every: float = 2.0) -> Iterator[str]:
        """The run's log lines as they come, until it ends or pauses with a question."""
        seen = 0
        while True:
            got = self.run(run_id, since=seen)
            yield from got["log"]
            seen += len(got["log"])
            if got["state"] in ENDED or got.get("question"):
                return
            time.sleep(every)

    def wait(self, run_id: str, every: float = 2.0, timeout: Optional[float] = None) -> dict:
        """The run once it has ended (or paused with a question): its last summary."""
        until = None if timeout is None else time.monotonic() + timeout
        while True:
            got = self.run(run_id, since=1 << 30)
            if got["state"] in ENDED or got.get("question"):
                return got
            if until is not None and time.monotonic() > until:
                raise TimeoutError(f"run {run_id} is still {got['state']} after {timeout} s")
            time.sleep(every)


# nullius remote: the same calls from a shell, one JSON document on stdout.

EXIT_OK, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_FAILED = 0, 2, 3, 4

EPILOG = """\
The server and its token: --server and --token, or NULLIUS_SERVER and NULLIUS_TOKEN.
RECIPE is a recipe file (JSON, as the builder saves one), or a saved recipe's id or name on the server.

exit codes:
  0  done
  2  usage
  3  not found on the server
  4  refused, unreachable, or the run did not end done
"""


def _recipe_arg(value: str):
    if os.path.isfile(value):
        with open(value, encoding="utf-8") as fh:
            got = json.load(fh)
        return got["recipe"] if isinstance(got, dict) and isinstance(got.get("recipe"), dict) and "version" not in got else got
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nullius remote", description="Ask a Nullius server on another machine.",
                                 epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="the server's address (NULLIUS_SERVER)")
    ap.add_argument("--token", help="its token (NULLIUS_TOKEN)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="the server's version, the token's scope, where a run can go")
    sub.add_parser("catalogue", help="benchmarks, agents and capabilities")
    sub.add_parser("workspaces")
    p = sub.add_parser("jobs", help="the jobs of the open workspace")
    p.add_argument("--workspace")
    p = sub.add_parser("job", help="one job, or one of its trials")
    p.add_argument("job_id")
    p.add_argument("trial", nargs="?")
    p.add_argument("--workspace")
    p = sub.add_parser("compare", help="two or more jobs side by side")
    p.add_argument("job_ids", nargs="+")
    p.add_argument("--matched", action="store_true")
    p.add_argument("--workspace")
    p = sub.add_parser("judgements", help="a judge's records for a job")
    p.add_argument("job_id")
    p.add_argument("--lane", choices=("decision", "llm"), default="decision")
    p = sub.add_parser("judge", help="ask a judge about one trial, or a job's unjudged trials: a paid call each")
    p.add_argument("job_id")
    p.add_argument("trial", nargs="?")
    p.add_argument("--lane", choices=("decision", "llm"), default="decision")
    p.add_argument("--yes", action="store_true", help="ask about every unjudged trial of the job")
    sub.add_parser("recipes", help="the saved recipes")
    p = sub.add_parser("check", help="what Review shows for a recipe; starts nothing")
    p.add_argument("recipe", metavar="RECIPE")
    p.add_argument("--where")
    p = sub.add_parser("run", help="start a recipe")
    p.add_argument("recipe", metavar="RECIPE")
    p.add_argument("--where")
    p.add_argument("--name", help="the run's name (default: the recipe's name and the second)")
    p.add_argument("--yes", action="store_true", help="answer yes to the question the check returns")
    p.add_argument("--follow", action="store_true", help="print the log until the run ends; exit 4 unless it ends done")
    sub.add_parser("runs", help="the runs the server follows")
    p = sub.add_parser("status", help="one run: its state, stages and log")
    p.add_argument("run_id")
    p.add_argument("--since", type=int, default=0)
    for name, text in (("stop", "stop a run"), ("continue", "answer a paused run: go on")):
        p = sub.add_parser(name, help=text)
        p.add_argument("run_id")
    a = ap.parse_args(argv)
    try:
        nx = Client(a.server, a.token)
    except ValueError as e:
        print(e, file=sys.stderr)
        return EXIT_USAGE
    try:
        if a.cmd == "run":
            out = nx.start(_recipe_arg(a.recipe), where=a.where, name=a.name, confirm=a.yes)
            if a.follow:
                ok = True
                for r in out["runs"]:
                    for line in nx.follow(r["id"]):
                        print(line, file=sys.stderr)
                    r.update(nx.run(r["id"], since=1 << 30))
                    ok = ok and r["state"] == "done"
                print(json.dumps(out, indent=1))
                return EXIT_OK if ok else EXIT_FAILED
        else:
            out = {"info": nx.info, "catalogue": nx.catalogue, "workspaces": nx.workspaces, "recipes": nx.recipes, "runs": nx.runs,
                   "jobs": lambda: nx.jobs(a.workspace),
                   "job": lambda: nx.trial(a.job_id, a.trial, a.workspace) if a.trial else nx.job(a.job_id, a.workspace),
                   "compare": lambda: nx.compare(*a.job_ids, matched=a.matched, workspace=a.workspace),
                   "judgements": lambda: nx.judgements(a.job_id, a.lane),
                   "judge": lambda: nx.judge(a.job_id, a.trial, a.lane) if a.trial else nx.judge_job(a.job_id, a.lane, confirm=a.yes),
                   "check": lambda: nx.check(_recipe_arg(a.recipe), a.where),
                   "status": lambda: nx.run(a.run_id, a.since), "stop": lambda: nx.stop(a.run_id),
                   "continue": lambda: nx.continue_(a.run_id)}[a.cmd]()
    except ApiError as e:
        print(e, file=sys.stderr)
        if e.document and set(e.document) - {"error"}:
            print(json.dumps(e.document, indent=1))
        return EXIT_NOT_FOUND if e.status == 404 else EXIT_FAILED
    except (OSError, ValueError) as e:      # a recipe file that cannot be read
        print(e, file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(out, indent=1))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
