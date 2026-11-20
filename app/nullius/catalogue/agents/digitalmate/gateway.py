"""Submit a task through DigitalMate's native Gateway and return its final answer."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect


class Gateway:
    def __init__(self, socket, deadline: float):
        self.socket = socket
        self.deadline = deadline
        self.sequence = 0

    def request(self, **params) -> dict:
        self.sequence += 1
        request_id = f"nullius-{self.sequence}"
        end = min(self.deadline, time.monotonic() + 120)
        if time.monotonic() >= end:
            raise TimeoutError("DigitalMate task deadline exceeded")
        self.socket.send(json.dumps({
            "id": request_id, "method": "digitalmate.request", "params": params,
        }, ensure_ascii=False))
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("DigitalMate request timed out: " + params["action"])
            reply = json.loads(self.socket.recv(timeout=remaining))
            # Ignore streamed events; only task snapshots contain the final result.
            if not isinstance(reply, dict) or reply.get("id") != request_id:
                continue
            if reply.get("ok") is not True:
                error = reply.get("error") or {}
                raise RuntimeError("DigitalMate request rejected: " + str(error.get("code", "unknown")))
            result = reply.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("DigitalMate Gateway returned an invalid response")
            return result


def find_task(snapshot: dict, room_id: str, submission_id: str) -> dict | None:
    root = snapshot.get("snapshot", snapshot)
    if not isinstance(root, dict) or not isinstance(root.get("tasks", []), list):
        raise RuntimeError("DigitalMate Gateway returned an invalid snapshot")
    for task in root.get("tasks", []):
        if isinstance(task, dict) and task.get("room_id") == room_id and task.get("submission_id") == submission_id:
            return task
    return None


def final_answer(task: dict) -> str:
    if task.get("status") != "completed":
        raise RuntimeError("DigitalMate task did not complete: " + str(task.get("status")))
    answer = task.get("result")
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("DigitalMate completed without a final answer")
    return answer


def submit_task(prompt: str, project_path: Path, token: str, *, timeout: float = 600,
                url: str = "ws://127.0.0.1:18092/digitalmate/ws") -> str:
    """Use the isolated local service; forward the prompt and workspace unchanged."""
    deadline = time.monotonic() + timeout
    try:
        with connect(url,
                     additional_headers={"Authorization": "Bearer " + token},
                     open_timeout=min(30, timeout), close_timeout=5,
                     max_size=32 * 1024 * 1024, proxy=None) as socket:
            gateway = Gateway(socket, deadline)
            created = gateway.request(action="room.create", title="Nullius task", agent_id="digitalmate")
            room_id = created.get("room_id")
            if not isinstance(room_id, str) or not room_id:
                raise RuntimeError("DigitalMate room.create returned no room_id")
            submission_id = str(uuid.uuid4())
            snapshot = gateway.request(
                action="room.message", room_id=room_id, submission_id=submission_id,
                agent_id="digitalmate", text=prompt, project_path=str(project_path),
                client={"language": "en", "device": "mobile"},
            )
            task = find_task(snapshot, room_id, submission_id)
            if task is None and not snapshot.get("task_id"):
                raise RuntimeError("DigitalMate room.message returned no task receipt")
            while True:
                if task is not None:
                    status = task.get("status")
                    if status not in {"pending", "queued", "running"}:
                        return final_answer(task)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("DigitalMate task deadline exceeded")
                time.sleep(min(1, remaining))
                task = find_task(gateway.request(action="snapshot"), room_id, submission_id)
    except WebSocketException as error:
        # Keep authentication headers and connection internals out of stderr.
        raise RuntimeError("DigitalMate Gateway connection failed") from error
