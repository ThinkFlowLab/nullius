"""The tools kit, version 2: its MCP server speaks JSON-RPC and answers each
tool from the unit's seed, and each check is scored from the server's own call
log and the final message - passed only with both. Kit versions are recorded
with a proof, and a proof on an older kit is said to be one."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from nullius.standard import definitions, jsonl

KIT = os.path.join(definitions.KITS, "agent-tools-v1")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _module(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(KIT, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mcp = _module("kit_mcp_under_test", "mcp_kit.py")
driver = _module("kit_driver_under_test", "driver.py")
SEED = "0123456789abcdef"


def rpc(method, params=None, rid=1, seed=SEED, only=None, log=None):
    return mcp.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, seed, only, log)


def tool(name, args, **kw):
    return rpc("tools/call", {"name": name, "arguments": args}, **kw)["result"]


def text(r):
    return r["content"][0]["text"]


class Server(unittest.TestCase):
    def test_handshake_lists_twenty_tools_with_schemas(self):
        init = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")      # the client's own, echoed
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, SEED))
        tools = rpc("tools/list")["result"]["tools"]
        self.assertEqual(len(tools), 20)
        self.assertEqual(len({t["name"] for t in tools}), 20)
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertEqual(sorted(t["inputSchema"].get("required", [])), sorted(t["inputSchema"]["properties"]))
        self.assertEqual(rpc("ping")["result"], {})
        self.assertEqual(rpc("nope")["error"]["code"], -32601)

    def test_one_tool_mode_lists_and_answers_only_that_tool(self):
        self.assertEqual([t["name"] for t in rpc("tools/list", only=["lookup_code"])["result"]["tools"]], ["lookup_code"])
        self.assertIn("error", rpc("tools/call", {"name": "get_stock_code", "arguments": {}}, only=["lookup_code"]))
        self.assertTrue(text(tool("lookup_code", {}, only=["lookup_code"])).startswith("kit-"))

    def test_every_tool_answers_from_the_seed(self):
        args = {"warehouse": "Riverside", "item": 7, "parcel": "P-1000", "order": 1234,
                "customer": "CUS-AAAA-BBBB", "account": "ACC-AAAA-BBBB", "site": mcp.site_id(SEED, "Mill Lane"), "year": 2024}
        for t in rpc("tools/list")["result"]["tools"]:
            mine = {k: args[k] for k in t["inputSchema"]["properties"]}
            r = tool(t["name"], mine)
            self.assertFalse(r["isError"], (t["name"], text(r)))
            self.assertEqual(text(r), text(tool(t["name"], mine)))                     # deterministic
            self.assertNotEqual(text(r), text(tool(t["name"], mine, seed="another")), t["name"])   # and the seed's

    def test_arguments_change_the_answer_and_wrong_types_are_tool_errors(self):
        a = text(tool("get_stock_code", {"warehouse": "Riverside", "item": 7}))
        self.assertIn(mcp.code(SEED, "stock", "RIVERSIDE", 7), a)
        self.assertEqual(a, text(tool("get_stock_code", {"warehouse": " riverside ", "item": 7.0})))
        self.assertNotEqual(a, text(tool("get_stock_code", {"warehouse": "Riverside", "item": 8})))
        self.assertNotEqual(a, text(tool("get_stock_code", {"warehouse": "Northgate", "item": 7})))
        bad = tool("get_stock_code", {"warehouse": "Riverside", "item": "7"})
        self.assertTrue(bad["isError"])
        self.assertIn("item must be an integer", text(bad))
        self.assertTrue(tool("get_stock_code", {"item": 7})["isError"])

    def test_decoys_answer_in_the_same_shape_with_other_codes(self):
        codes = {text(tool(n, {"parcel": "P-1000"})).rsplit(" ", 1)[1] for n in mcp.NUMBERS}
        self.assertEqual(len(codes), len(mcp.NUMBERS))
        self.assertTrue(all(len(c) == 9 and c[4] == "-" for c in codes))

    def test_the_chain_is_three_hops(self):
        customer, account, settlement = mcp.chain(SEED, 1234)
        self.assertIn(customer, text(tool("get_order_customer", {"order": 1234})))
        self.assertIn(account, text(tool("get_customer_account", {"customer": customer})))
        self.assertIn(settlement, text(tool("get_account_settlement", {"account": account})))

    def test_a_site_name_is_refused_with_its_id_then_the_id_answers(self):
        r = tool("get_permit_code", {"site": "Mill Lane", "year": 2024})
        self.assertTrue(r["isError"])
        sid = mcp.site_id(SEED, "Mill Lane")
        self.assertIn(sid, text(r))
        ok = tool("get_permit_code", {"site": sid, "year": 2024})
        self.assertFalse(ok["isError"])
        self.assertIn(mcp.code(SEED, "permit", sid, 2024), text(ok))

    def test_every_call_is_logged(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "calls.jsonl")
        tool("get_permit_code", {"site": "Mill Lane", "year": 2024}, log=log)
        tool("get_order_status", {"order": 5}, log=log)
        calls = driver.read_log(log)
        self.assertEqual([(c["tool"], c["isError"]) for c in calls], [("get_permit_code", True), ("get_order_status", False)])
        self.assertEqual(calls[0]["arguments"], {"site": "Mill Lane", "year": 2024})

    def test_the_server_over_stdio(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "lookup_code", "arguments": {}}}]
        out = subprocess.run([sys.executable, os.path.join(KIT, "mcp_kit.py")], input="".join(json.dumps(m) + "\n" for m in msgs),
                             capture_output=True, text=True, timeout=30, env={"KIT_SEED": SEED, "PATH": os.environ.get("PATH", "")})
        replies = [json.loads(l) for l in out.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[2]["result"]["content"][0]["text"], mcp.call(SEED, "lookup_code", {}))


def good_calls(unit, seed):
    """What an agent that does each check right leaves in the log."""
    a = driver.asked(seed)

    def c(name, args):
        try:
            return {"tool": name, "arguments": args, "isError": False, "text": mcp.call(seed, name, args)}
        except mcp.ToolError as e:
            return {"tool": name, "arguments": args, "isError": True, "text": str(e)}
    if unit == "one-tool":
        return [c("lookup_code", {})]
    if unit == "arguments":
        return [c("get_stock_code", {"warehouse": a["warehouse"], "item": a["item"]})]
    if unit == "decoys":
        return [c("get_tracing_number", {"parcel": a["parcel"]}), c("get_tracking_number", {"parcel": a["parcel"]})]
    if unit == "chain":
        customer, account, _ = mcp.chain(seed, a["order"])
        return [c("get_order_customer", {"order": a["order"]}), c("get_customer_account", {"customer": customer}),
                c("get_account_settlement", {"account": account})]
    return [c("get_permit_code", {"site": a["site"], "year": a["year"]}),
            c("get_permit_code", {"site": mcp.site_id(seed, a["site"]), "year": a["year"]})]


class Scoring(unittest.TestCase):
    def test_each_unit_passes_with_its_calls_and_its_answer(self):
        for unit in driver.UNITS:
            want = driver.expected(unit, SEED)
            ok, detail = driver.verdict(unit, SEED, good_calls(unit, SEED), f"It is {want.lower()}.")
            self.assertTrue(ok, (unit, detail))

    def test_the_answer_alone_is_not_enough(self):
        for unit in driver.UNITS:
            ok, detail = driver.verdict(unit, SEED, [], driver.expected(unit, SEED))
            self.assertFalse(ok, unit)
            self.assertIn("why", detail)

    def test_the_calls_alone_are_not_enough(self):
        for unit in driver.UNITS:
            ok, detail = driver.verdict(unit, SEED, good_calls(unit, SEED), "I could not find it.")
            self.assertFalse(ok, unit)
            self.assertEqual(detail["why"], "the final message does not carry the answer")

    def test_an_answer_from_a_decoy_fails_and_says_where_it_came_from(self):
        calls = good_calls("decoys", SEED)
        wrong = calls[0]["text"].rsplit(" ", 1)[1]
        ok, detail = driver.verdict("decoys", SEED, calls, wrong)
        self.assertFalse(ok)
        self.assertEqual(detail["answer_from"], ["get_tracing_number"])

    def test_wrong_arguments_fail(self):
        a = driver.asked(SEED)
        call = {"tool": "get_stock_code", "arguments": {"warehouse": a["warehouse"], "item": a["item"] + 1}, "isError": False}
        ok, detail = driver.verdict("arguments", SEED, [call], driver.expected("arguments", SEED))
        self.assertFalse(ok)
        self.assertIn("no successful get_stock_code", detail["why"])

    def test_a_broken_chain_fails(self):
        calls = good_calls("chain", SEED)
        ok, detail = driver.verdict("chain", SEED, calls[:1] + calls[2:], driver.expected("chain", SEED))
        self.assertFalse(ok)
        self.assertIn("get_customer_account", detail["why"])
        ok, _ = driver.verdict("chain", SEED, calls[1:] + calls[:1], driver.expected("chain", SEED))
        self.assertFalse(ok)                                                         # out of order

    def test_retry_needs_the_error_then_the_corrected_call(self):
        calls = good_calls("retry", SEED)
        self.assertTrue(calls[0]["isError"])
        want = driver.expected("retry", SEED)
        self.assertFalse(driver.verdict("retry", SEED, calls[1:], want)[0])          # no error seen
        self.assertFalse(driver.verdict("retry", SEED, calls[::-1], want)[0])        # corrected before the error

    def test_prompts_never_carry_the_answer_or_a_tool_name(self):
        names = [t[0] for t in mcp.TOOLS]
        for unit in driver.UNITS:
            p = driver.prompt(unit, SEED)
            self.assertNotIn(driver._flat(driver.expected(unit, SEED)), driver._flat(p))
            self.assertFalse([n for n in names if n in p], unit)
        self.assertNotEqual(driver.expected("chain", SEED), driver.expected("chain", "another seed"))


class Driver(unittest.TestCase):
    """The driver as a run speaks to it: set up, the server it names called as an agent would, scored."""

    def test_setup_then_the_named_server_then_score(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": APP, "PYTHONDONTWRITEBYTECODE": "1"}
        d = jsonl.Proc([sys.executable, os.path.join(KIT, "driver.py")], KIT, env, os.path.join(tmp, "driver.log")).start()
        self.addCleanup(d.close)
        self.assertEqual(d.request("units", dataset="kit")["units"], list(driver.UNITS))
        for unit in ("one-tool", "retry"):
            ws = os.path.join(tmp, unit, "workspace")
            os.makedirs(ws)
            s = d.request("setup", unit=unit, dir=ws)
            srv = s["mcp"][0]
            self.assertEqual(srv["name"], "kit")
            self.assertNotIn("KIT_SEED", s["prompt"])
            seed = srv["env"]["KIT_SEED"]
            calls = [{"tool": c["tool"], "arguments": c["arguments"]} for c in good_calls(unit, seed)]
            msgs = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": c["tool"], "arguments": c["arguments"]}}
                    for i, c in enumerate(calls)]
            subprocess.run([srv["command"], *srv["args"]], input="".join(json.dumps(m) + "\n" for m in msgs), text=True,
                           capture_output=True, timeout=30, env=dict(srv["env"], PATH=env["PATH"]))
            self.assertTrue(os.path.isfile(os.path.join(tmp, unit, "verifier", "mcp-calls.jsonl")))
            self.assertFalse(os.listdir(ws))                                            # the log is not in the workspace
            got = d.request("score", unit=unit, dir=ws, transcript=[], answer=driver.expected(unit, seed))
            self.assertEqual(got["reward"], 1.0, got)
            bad = d.request("score", unit=unit, dir=ws, transcript=[], answer="no idea")
            self.assertEqual(bad["reward"], 0.0)

    def test_on_docker_the_server_is_started_by_the_images_python(self):
        """The agent starts it in its container: this machine's interpreter, mounted over the image's, would not run."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        from nullius.standard import container
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": APP, "PYTHONDONTWRITEBYTECODE": "1",
               container.CHECK_IMAGE: "nullius/base:test"}
        d = jsonl.Proc([sys.executable, os.path.join(KIT, "driver.py")], KIT, env, os.path.join(tmp, "driver.log")).start()
        self.addCleanup(d.close)
        ws = os.path.join(tmp, "one-tool", "workspace")
        os.makedirs(ws)
        srv = d.request("setup", unit="one-tool", dir=ws)["mcp"][0]
        self.assertEqual(srv["command"], "python3")
        self.assertEqual(container.mcp_mounts([srv], os.path.join(tmp, "one-tool"))[1], [os.path.join(KIT, "mcp_kit.py")])


class KitVersions(unittest.TestCase):
    def test_every_kit_has_a_version_and_the_tools_kit_is_two(self):
        for k in definitions.kits():
            self.assertIsInstance(k.get("kit_version"), int, k["key"])
        self.assertEqual(definitions.kit_version(definitions.kit("agent.tools/v1")), 2)
        self.assertEqual(definitions.kit_version(definitions.kit("agent.task/v1")), 1)
        self.assertEqual(definitions.kit_version({}), 1)

    def test_a_bad_kit_version_is_refused(self):
        k = definitions.kit("agent.task/v1")
        base = {x: v for x, v in k.items() if not x.startswith("_")}
        for bad in (0, "2", True, 1.5):
            self.assertIn("kit_version: a kit's version, a whole number from 1",
                          definitions.problems(dict(base, kit_version=bad), "benchmark"), bad)
        self.assertEqual(definitions.problems(dict(base, kit_version=3), "benchmark"), [])

    def test_a_proof_on_an_older_kit_is_still_a_proof_and_says_so(self):
        old = {"passed": "2/2", "on": "2026-10-10"}                                  # from before kits had versions
        self.assertEqual(definitions.proof_kit(old), 1)
        self.assertFalse(definitions.on_current_kit("agent.tools/v1", old))
        self.assertEqual(definitions.older_kit("agent.tools/v1", old), "proven on kit v1; kit v2 is current")
        self.assertTrue(definitions.on_current_kit("agent.task/v1", old))
        self.assertIsNone(definitions.older_kit("agent.task/v1", old))
        new = dict(old, kit_version=2)
        self.assertTrue(definitions.on_current_kit("agent.tools/v1", new))
        self.assertIsNone(definitions.older_kit("agent.tools/v1", new))
        self.assertIsNone(definitions.older_kit("agent.tools/v1", None))

    def test_cli_names_an_older_proof_and_still_counts_it(self):
        from nullius.standard import cli, match
        d = {"_kind": "agent", "label": "Bot", "key": "bot", "offers": ["agent.task/v1", "agent.tools/v1"],
             "proven": {"agent.task/v1": {"passed": "3/3"}, "agent.tools/v1": {"passed": "2/2"}}}
        self.assertEqual(cli.older(d), ["agent.tools/v1 proven on kit v1; kit v2 is current"])
        self.assertEqual(cli.older(d, ["agent.task/v1"]), [])
        self.assertEqual(match.fit(definitions.kit("agent.tools/v1"), d), [])        # still a fit


if __name__ == "__main__":
    unittest.main()
