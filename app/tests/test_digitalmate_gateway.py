"""Exercise the adapter against Hive Home's Gateway frames, without a model or service."""
from collections import deque
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nullius/catalogue/agents/digitalmate"))


def stub_websockets():
    """The backend suite has no websockets; every test here patches the socket."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        import types
        exceptions = types.ModuleType("websockets.exceptions")
        exceptions.WebSocketException = type("WebSocketException", (Exception,), {})
        client = types.ModuleType("websockets.sync.client")
        client.connect = None
        sys.modules.update({"websockets": types.ModuleType("websockets"), "websockets.exceptions": exceptions,
                            "websockets.sync": types.ModuleType("websockets.sync"), "websockets.sync.client": client})


stub_websockets()
import gateway  # noqa: E402


def terminal(ref, *, state=None):
    result = {"event": "digitalmate.task.text", "task_id": ref, "execution_id": "run-" + ref,
              "event_id": "result-" + ref, "committed_message_id": "result-" + ref,
              "version": 1, "final": True, "text": "Worker text, not the product's final answer"}
    if state:
        result.update(state=state, text="")
    return result


def report(ref, *, seq=2, children=(), text="  FINAL ANSWER: São Paulo\n"):
    return {"event": "hive.beebou.reply", "event_id": "report-" + ref, "final": True,
            "trigger": "bee_done", "task_ref": ref, "source_seq": seq, "outcome": "committed",
            "task_refs": list(children), "text": text}


class Socket:
    """A scripted product protocol peer, including pushes interleaved with RPCs."""
    def __init__(self, *, tasks=(), status="committed", answer="FINAL ANSWER: 42", before=(), polls=None,
                 initial_error=None, waiting=(), receipt=True):
        self.tasks, self.status, self.answer = list(tasks), status, answer
        self.before, self.polls = list(before), polls or {}
        self.initial_error, self.waiting = initial_error, set(waiting)
        self.receipt = receipt
        self.frames, self.inbox = [], deque()
        self.now = 0.0

    def send(self, text):
        frame = json.loads(text)
        self.frames.append(frame)
        method, params = frame["method"], frame["params"]
        response = {"id": frame["id"], "ok": True}
        events = []
        if method == "hive.message.send":
            response["result"] = {"status": self.status, "message_id": params["message_id"],
                                  "answer": self.answer, "task_refs": self.tasks}
            if not self.receipt:
                response["result"] = {}
            if self.initial_error:
                response.update(ok=False, error=self.initial_error)
            events = self.before
            for event in events:
                if event.get("trigger") == "user":
                    event["source_message_id"] = params["message_id"]
        elif method == "hive.result.get":
            ref = params["task_ref"]
            if ref in self.waiting:
                response["result"] = {"status": "waiting_user", "task_ref": ref,
                                      "interaction": {"interaction_id": "q", "questions": [{"question": "Sure?"}]}}
            else:
                pending = self.polls.get(ref, deque())
                events = pending.popleft() if pending else []
                response["result"] = {"status": "available" if any(
                    e.get("event") == "digitalmate.task.text" for e in events) else "pending", "task_ref": ref}
        elif method == "hive.bee.stop":
            response["result"] = {"status": "stopped"}
        else:
            raise AssertionError("Unexpected method: " + method)
        self.inbox.extend(json.dumps(e) for e in events)
        self.inbox.append(json.dumps({"event": "digitalmate.stream", "final": True, "text": "Ignore stream"}))
        self.inbox.append(json.dumps(response))

    def recv(self, *, timeout):
        assert timeout > 0
        if self.inbox:
            return self.inbox.popleft()
        self.now += timeout
        raise TimeoutError()


class GatewayTests(unittest.TestCase):
    prompt = '  Read the file.\nReturn {"answer": "its content"}.\n'

    def submit(self, socket, timeout=3):
        connection = MagicMock()
        connection.__enter__.return_value = socket
        with patch.object(gateway, "connect", return_value=connection) as connect, \
                patch.object(gateway.time, "monotonic", side_effect=lambda: socket.now):
            answer = gateway.submit_task(self.prompt, "local-test-token", timeout=timeout)
        self.assertEqual(connect.call_args.args, ("ws://127.0.0.1:18092/digitalmate/ws",))
        self.assertEqual(connect.call_args.kwargs["additional_headers"],
                         {"Authorization": "Bearer local-test-token"})
        connection.__exit__.assert_called_once()
        return answer

    def test_nondelegated_turn_preserves_prompt_and_product_answer(self):
        socket = Socket()
        self.assertEqual(self.submit(socket), socket.answer)
        self.assertEqual(len(socket.frames), 1)
        frame = socket.frames[0]
        self.assertEqual(frame["method"], "hive.message.send")
        self.assertEqual(frame["params"]["text"], self.prompt)
        self.assertEqual(set(frame["params"]), {"message_id", "text"})

    def test_all_delegated_tasks_and_beebou_reports_are_required(self):
        socket = Socket(tasks=("a", "b"), answer="I dispatched your task.", polls={
            "a": deque([[terminal("a"), report("a", text="First part")]]),
            "b": deque([[], [terminal("b"), report("b", seq=3)]]),
        })
        self.assertEqual(self.submit(socket), "  FINAL ANSWER: São Paulo\n")
        polls = [f["params"]["task_ref"] for f in socket.frames if f["method"] == "hive.result.get"]
        self.assertEqual(polls, ["a", "b", "b"])

    def test_pushes_before_ack_and_out_of_order_child_completion_are_retained(self):
        socket = Socket(tasks=("parent",), answer="Acknowledgement", before=[
            terminal("unrelated"), report("unrelated", seq=100, text="Wrong task"),
            terminal("child"), report("child", seq=3),
            terminal("parent"), report("parent", children=("child",), text="Follow-up dispatched"),
        ])
        self.assertEqual(self.submit(socket), "  FINAL ANSWER: São Paulo\n")
        self.assertEqual(len(socket.frames), 1)

    def test_pending_initial_turn_waits_for_its_committed_reply(self):
        initial = report("unused", seq=1, text="FINAL ANSWER: delayed")
        initial.update(trigger="user", task_ref=None)
        socket = Socket(status="pending", answer=None, before=[initial])
        self.assertEqual(self.submit(socket), "FINAL ANSWER: delayed")

    def test_raw_worker_result_without_beebou_report_is_not_final(self):
        socket = Socket(tasks=("a",), polls={"a": deque([[terminal("a")]])})
        with self.assertRaisesRegex(TimeoutError, "deadline exceeded"):
            self.submit(socket)
        self.assertEqual(sum(f["method"] == "hive.message.send" for f in socket.frames), 1)

    def test_failure_stopped_and_unavailable_do_not_return_partial_text(self):
        for state in ("failed", "stopped", "unavailable"):
            with self.subTest(state=state):
                socket = Socket(tasks=("a",), polls={"a": deque([[terminal("a", state=state)]])})
                with self.assertRaisesRegex(RuntimeError, "Bee task ended: " + state):
                    self.submit(socket)

    def test_a_bee_question_fails_the_task_and_requests_stop_once(self):
        socket = Socket(tasks=("a",), waiting=("a",))
        with self.assertRaisesRegex(RuntimeError, "requires user input"):
            self.submit(socket)
        stops = [f for f in socket.frames if f["method"] == "hive.bee.stop"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["params"]["task_ref"], "a")
        self.assertTrue(stops[0]["params"]["operation_id"])

    def test_failed_beebou_report_is_not_a_final_answer(self):
        failed = report("a")
        failed.update(outcome="failed", text="Failure notice")
        with self.assertRaisesRegex(RuntimeError, "reply did not commit"):
            self.submit(Socket(tasks=("a",), before=[terminal("a"), failed]))

    def test_empty_or_malformed_answer_is_rejected(self):
        for answer in (None, "", " \n", 42, ["42"]):
            with self.subTest(answer=answer), self.assertRaisesRegex(RuntimeError, "without a final answer"):
                self.submit(Socket(answer=answer))

    def test_a_reply_without_its_submission_receipt_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "different submission receipt"):
            self.submit(Socket(receipt=False))

    def test_rejected_submission_is_not_retried_and_does_not_expose_details(self):
        socket = Socket(initial_error={"code": "BAD_PARAMS", "message": "private details"})
        with self.assertRaisesRegex(RuntimeError, "request rejected: BAD_PARAMS") as error:
            self.submit(socket)
        self.assertNotIn("private details", str(error.exception))
        self.assertEqual(len(socket.frames), 1)

    def test_deadline_does_not_resubmit_and_cleans_up_known_tasks(self):
        socket = Socket(tasks=("a",))
        with self.assertRaisesRegex(TimeoutError, "deadline exceeded"):
            self.submit(socket)
        self.assertEqual(socket.now, 3)
        self.assertEqual(socket.frames[-1]["method"], "hive.bee.stop")
        self.assertEqual(sum(f["method"] == "hive.message.send" for f in socket.frames), 1)

    def test_expired_deadline_prevents_a_request(self):
        socket = MagicMock()
        with self.assertRaises(TimeoutError):
            gateway.Gateway(socket, gateway.time.monotonic() - 1).rpc("hive.result.get", task_ref="a")
        socket.send.assert_not_called()

    def test_connection_failure_is_reported_without_credentials(self):
        with patch.object(gateway, "connect", side_effect=gateway.WebSocketException("private header")):
            with self.assertRaisesRegex(RuntimeError, "Gateway connection failed") as error:
                gateway.submit_task(self.prompt, "local-test-token")
        self.assertNotIn("private header", str(error.exception))

    def test_a_dropped_connection_names_its_close_code_only(self):
        dropped = gateway.WebSocketException("private reason")
        dropped.rcvd = MagicMock(code=1011, reason="private reason")
        with patch.object(gateway, "connect", side_effect=dropped):
            with self.assertRaisesRegex(RuntimeError, "connection failed: WebSocketException 1011$") as error:
                gateway.submit_task(self.prompt, "local-test-token")
        self.assertNotIn("private", str(error.exception))

    def test_malformed_matching_response_is_rejected(self):
        socket = MagicMock()
        socket.recv.return_value = json.dumps({"id": "nullius-1", "ok": True, "result": "not a result"})
        with self.assertRaisesRegex(RuntimeError, "invalid response"):
            gateway.Gateway(socket, gateway.time.monotonic() + 10).rpc("hive.result.get", task_ref="a")


if __name__ == "__main__":
    unittest.main()
