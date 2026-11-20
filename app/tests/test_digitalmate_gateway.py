"""Exercise the adapter against native Gateway frames, without a model or service."""
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


class Socket:
    """A service that advances a task across snapshots and interleaves events."""
    def __init__(self, statuses=("pending", "running", "completed"), *, answer="  FINAL ANSWER: São Paulo\n",
                 nested=True, receipt_only=False, error=None):
        self.statuses = iter(statuses)
        self.answer = answer
        self.nested = nested
        self.receipt_only = receipt_only
        self.error = error
        self.frames = []
        self.inbox = deque()
        self.submission = ""

    def snapshot(self):
        task = {"id": "task-1", "room_id": "room-1", "submission_id": self.submission,
                "status": next(self.statuses), "result": self.answer}
        root = {"tasks": [
            dict(task, room_id="other-room", result="Wrong room"),
            dict(task, submission_id="other-submission", result="Wrong submission"),
            task,
        ]}
        return {"snapshot": root} if self.nested else root

    def send(self, message):
        frame = json.loads(message)
        self.frames.append(frame)
        params = frame["params"]
        action = params["action"]
        reply = {"id": frame["id"], "ok": True}
        if action == "room.create":
            reply["result"] = {"room_id": "room-1"}
        elif action == "room.message":
            self.submission = params["submission_id"]
            if self.error:
                reply.update(ok=False, error=self.error)
            else:
                reply["result"] = {"task_id": "task-1"} if self.receipt_only else self.snapshot()
        elif action == "snapshot":
            reply["result"] = self.snapshot()
        else:
            raise AssertionError("Unexpected action: " + action)
        self.inbox.extend([
            json.dumps({"event": "digitalmate.stream", "final": True, "text": "Not the task result"}),
            json.dumps({"id": "unrelated-request", "ok": True, "result": {"text": "Wrong reply"}}),
            json.dumps(reply),
        ])

    def recv(self, *, timeout):
        assert timeout > 0
        return self.inbox.popleft()


class GatewayTests(unittest.TestCase):
    prompt = '  Read the file.\nReturn {"answer": "its content"}.\n'
    workspace = Path("/trial/workspace with spaces")

    def submit(self, socket):
        connection = MagicMock()
        connection.__enter__.return_value = socket
        with patch.object(gateway, "connect", return_value=connection) as connect, \
                patch.object(gateway.time, "sleep"):
            answer = gateway.submit_task(self.prompt, self.workspace, "local-test-token")
        self.assertEqual(connect.call_args.args, ("ws://127.0.0.1:18092/digitalmate/ws",))
        self.assertEqual(connect.call_args.kwargs["additional_headers"],
                         {"Authorization": "Bearer local-test-token"})
        connection.__exit__.assert_called_once()
        return answer

    def test_native_protocol_preserves_prompt_workspace_and_final_result(self):
        for nested in (True, False):
            with self.subTest(nested=nested):
                socket = Socket(nested=nested)
                self.assertEqual(self.submit(socket), socket.answer)
                params = [f["params"] for f in socket.frames]
                self.assertEqual([p["action"] for p in params],
                                 ["room.create", "room.message", "snapshot", "snapshot"])
                self.assertEqual(params[1]["text"], self.prompt)
                self.assertEqual(params[1]["project_path"], str(self.workspace))
                self.assertEqual(params[1]["client"], {"language": "en", "device": "mobile"})
                self.assertTrue(all(f["method"] == "digitalmate.request" for f in socket.frames))
                self.assertEqual(len({f["id"] for f in socket.frames}), len(socket.frames))

    def test_receipt_only_submission_is_resolved_from_snapshot(self):
        socket = Socket(statuses=("completed",), receipt_only=True)
        self.assertEqual(self.submit(socket), socket.answer)
        self.assertEqual(len(socket.frames), 3)

    def test_immediate_completion_needs_no_polling(self):
        socket = Socket(statuses=("completed",))
        self.assertEqual(self.submit(socket), socket.answer)
        self.assertEqual(len(socket.frames), 2)

    def test_noncompleted_task_never_returns_plausible_partial_text(self):
        for status in ("failed", "waiting", "cancelled", "stale", "blocked", None):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, "did not complete"):
                self.submit(Socket(statuses=(status,)))

    def test_completed_task_requires_a_nonempty_text_result(self):
        for answer in (None, "", " \n", 42, ["42"]):
            with self.subTest(answer=answer), self.assertRaisesRegex(RuntimeError, "without a final answer"):
                self.submit(Socket(statuses=("completed",), answer=answer))

    def test_rejected_submission_is_not_retried(self):
        socket = Socket(error={"code": "BAD_PARAMS", "message": "private details"})
        with self.assertRaisesRegex(RuntimeError, "request rejected: BAD_PARAMS") as raised:
            self.submit(socket)
        self.assertNotIn("private details", str(raised.exception))
        self.assertEqual(len(socket.frames), 2)

    def test_expired_deadline_prevents_further_requests(self):
        socket = MagicMock()
        with self.assertRaises(TimeoutError):
            gateway.Gateway(socket, gateway.time.monotonic() - 1).request(action="snapshot")
        socket.send.assert_not_called()

    def test_receive_timeout_fails_without_resubmitting(self):
        socket = Socket()
        with patch.object(socket, "recv", side_effect=TimeoutError), self.assertRaises(TimeoutError):
            self.submit(socket)
        self.assertEqual(len(socket.frames), 1)

    def test_running_snapshots_cannot_extend_the_overall_deadline(self):
        socket = Socket(statuses=("running", "running", "running"))
        connection = MagicMock()
        connection.__enter__.return_value = socket
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(gateway, "connect", return_value=connection), \
                patch.object(gateway.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(gateway.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(TimeoutError, "deadline exceeded"):
                gateway.submit_task(self.prompt, self.workspace, "local-test-token", timeout=2)
        self.assertEqual(clock[0], 2)
        self.assertEqual([f["params"]["action"] for f in socket.frames],
                         ["room.create", "room.message", "snapshot"])
        connection.__exit__.assert_called_once()

    def test_connection_failure_is_reported_without_credentials(self):
        with patch.object(gateway, "connect", side_effect=gateway.WebSocketException("private header")):
            with self.assertRaisesRegex(RuntimeError, "Gateway connection failed") as raised:
                gateway.submit_task(self.prompt, self.workspace, "local-test-token")
        self.assertNotIn("private header", str(raised.exception))

    def test_malformed_matching_response_is_rejected(self):
        socket = MagicMock()
        socket.recv.return_value = json.dumps({"id": "nullius-1", "ok": True, "result": "not a snapshot"})
        with self.assertRaisesRegex(RuntimeError, "invalid response"):
            gateway.Gateway(socket, gateway.time.monotonic() + 10).request(action="snapshot")


if __name__ == "__main__":
    unittest.main()
