"""Submit a task to DigitalMate's Hive Home through its Gateway and return the final answer."""
from __future__ import annotations

import json
import time
import uuid

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect


class Gateway:
    def __init__(self, socket, deadline: float):
        self.socket, self.deadline = socket, deadline
        self.sequence = 0
        self.events: list[dict] = []

    def receive(self, end: float) -> dict:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("DigitalMate task deadline exceeded")
        frame = json.loads(self.socket.recv(timeout=remaining))
        if not isinstance(frame, dict):
            raise RuntimeError("DigitalMate Gateway returned an invalid frame")
        if frame.get("final") is True and frame.get("event") in {
            "digitalmate.task.text", "hive.beebou.reply",
        }:
            self.events.append(frame)
        return frame

    def rpc(self, method: str, **params) -> dict:
        self.sequence += 1
        request_id = f"nullius-{self.sequence}"
        if time.monotonic() >= self.deadline:
            raise TimeoutError("DigitalMate task deadline exceeded")
        self.socket.send(json.dumps({"id": request_id, "method": method, "params": params}, ensure_ascii=False))
        while True:
            reply = self.receive(self.deadline)
            if reply.get("id") != request_id:
                continue
            if reply.get("ok") is not True:
                error = reply.get("error") or {}
                raise RuntimeError("DigitalMate request rejected: " + str(error.get("code", "unknown")))
            result = reply.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("DigitalMate Gateway returned an invalid response")
            return result


def text_answer(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("DigitalMate completed without a final answer")
    return value


def task_refs(value) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(ref, str) or not ref for ref in value):
        raise RuntimeError("DigitalMate returned an invalid Hive task receipt")
    return set(value)


class Completion:
    """A user turn and the complete dispatch lineage of its committed Bee reports."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self.initial = False
        self.tasks: set[str] = set()
        self.terminals: set[str] = set()
        self.reports: set[str] = set()
        self.answer = None
        self.answer_seq = -1
        self.seen: set[str] = set()
        self.answered: set[str] = set()

    def response(self, reply: dict) -> None:
        if reply.get("message_id") != self.message_id:
            raise RuntimeError("DigitalMate returned a different submission receipt")
        status = reply.get("status")
        if status in {"committed", "COMPLETED"}:
            self.tasks.update(task_refs(reply.get("task_refs")))
            self.answer = text_answer(reply.get("answer"))
            self.initial = True
        elif status not in {"accepted", "pending"}:
            raise RuntimeError("DigitalMate Hive turn did not complete: " + str(status))

    def collect(self, events: list[dict]) -> None:
        # A child may finish before its parent's report arrives. Retain unmatched
        # events and revisit after the report adds that child's task_ref.
        changed = True
        while changed:
            changed = False
            for event in events:
                identity = event.get("event_id")
                if not isinstance(identity, str) or not identity or identity in self.seen:
                    continue
                if event["event"] == "digitalmate.task.text":
                    ref = event.get("task_id")
                    if ref not in self.tasks:
                        continue
                    if event.get("state") not in (None, "completed"):
                        raise RuntimeError("DigitalMate Bee task ended: " + str(event["state"]))
                    text_answer(event.get("text"))
                    self.terminals.add(ref)
                else:
                    trigger, ref = event.get("trigger"), event.get("task_ref")
                    if trigger == "user":
                        if event.get("source_message_id") != self.message_id:
                            continue
                    elif trigger != "bee_done" or ref not in self.tasks:
                        continue
                    if event.get("outcome") != "committed":
                        raise RuntimeError("DigitalMate Beebou reply did not commit")
                    self.tasks.update(task_refs(event.get("task_refs")))
                    if trigger == "user":
                        self.initial = True
                    else:
                        self.reports.add(ref)
                    sequence = event.get("source_seq")
                    if not isinstance(sequence, int):
                        raise RuntimeError("DigitalMate returned an invalid Hive reply sequence")
                    answer = text_answer(event.get("text"))
                    if sequence > self.answer_seq:
                        self.answer, self.answer_seq = answer, sequence
                self.seen.add(identity)
                changed = True

    @property
    def done(self) -> bool:
        return self.initial and self.tasks <= self.terminals and self.tasks <= self.reports


class Question:
    """A Bee's open question card: the turn's reply, answered by the next user turn."""

    def __init__(self, task_ref: str, card) -> None:
        if not isinstance(card, dict) or not isinstance(card.get("interaction_id"), str) \
                or not card["interaction_id"]:
            raise RuntimeError("DigitalMate returned an invalid Bee question")
        questions = card.get("questions")
        if not isinstance(questions, list) or len(questions) != 1 or not isinstance(questions[0], dict):
            # The product's answer edge takes one answer to one question.
            raise RuntimeError("DigitalMate Bee asked several questions at once")
        self.task_ref, self.interaction_id = task_ref, card["interaction_id"]
        self.question = text_answer(questions[0].get("question"))
        options = [option.get("label") or option.get("value") if isinstance(option, dict) else option
                   for option in questions[0].get("options") or []]
        self.options = [option for option in options if isinstance(option, str) and option.strip()]

    @property
    def text(self) -> str:
        if not self.options:
            return self.question
        return self.question + "\n\nOptions: " + ", ".join(self.options)


def await_answer(gateway: Gateway, completion: Completion, *, questions: bool = False):
    """The turn's final answer, or with ``questions`` the first open Bee question."""
    while True:
        completion.collect(gateway.events)
        if completion.done:
            return text_answer(completion.answer)
        for ref in sorted(completion.tasks - completion.terminals):
            result = gateway.rpc("hive.result.get", task_ref=ref)
            if result.get("status") == "waiting_user":
                if not questions:
                    raise RuntimeError("DigitalMate Bee task requires user input")
                question = Question(ref, result.get("interaction"))
                # The card just answered can still read as open until its Run resumes.
                if question.interaction_id not in completion.answered:
                    return question
            elif result.get("status") not in {"pending", "available"}:
                raise RuntimeError("DigitalMate returned an invalid Hive result status")
        completion.collect(gateway.events)
        if completion.done:
            return text_answer(completion.answer)
        pause(gateway)


def pause(gateway: Gateway) -> None:
    # Receive live replies between polls; an idle socket is not a failed task.
    end = min(gateway.deadline, time.monotonic() + 1)
    try:
        gateway.receive(end)
    except TimeoutError:
        if time.monotonic() >= gateway.deadline:
            raise TimeoutError("DigitalMate task deadline exceeded") from None


# Committed but not yet proven delivered, or not reconciled: the edge asks for the same ID again.
RETRY_ANSWER = {"delivery_pending", "unknown"}


def answer_question(gateway: Gateway, completion: Completion, question: Question, text: str) -> None:
    """Answer the Bee's card at its original wait point, as the product's card does."""
    operation = str(uuid.uuid4())
    while True:
        reply = gateway.rpc("digitalmate.request", action="task.answer", task_id=question.task_ref,
                            interaction_id=question.interaction_id, message_id=operation,
                            answers={question.question: text})
        status = (reply.get("hive_answer") or {}).get("status")
        if status == "consumed":
            completion.answered.add(question.interaction_id)
            return
        if status not in RETRY_ANSWER:
            raise RuntimeError("DigitalMate did not accept the answer: " + str(status))
        pause(gateway)


def stop_tasks(gateway: Gateway, completion: Completion) -> None:
    # Best effort through the public owner. run.py also stops the entire isolated
    # process group, including a turn that never returned a dispatch receipt.
    gateway.deadline = time.monotonic() + 5
    for ref in sorted(completion.tasks - completion.terminals):
        try:
            gateway.rpc("hive.bee.stop", operation_id=str(uuid.uuid4()), task_ref=ref)
        except (OSError, RuntimeError, ValueError, WebSocketException):
            break


class Conversation:
    """One ACP session's open Bee question; Hive owns everything else."""

    def __init__(self) -> None:
        self.waiting: tuple[Completion, Question] | None = None


def submit_task(prompt: str, token: str, *, timeout: float = 600,
                url: str = "ws://127.0.0.1:18092/digitalmate/ws", conversation: Conversation | None = None) -> str:
    """Send the prompt unchanged as one Hive user turn; the answer is Beebou's last committed reply.

    Without a conversation a Bee question fails the task: nothing could answer it.
    With one (a session), the question is the reply and the next prompt answers it.
    """
    deadline = time.monotonic() + timeout
    waiting = conversation.waiting if conversation is not None else None
    completion = waiting[0] if waiting else Completion(str(uuid.uuid4()))
    if conversation is not None:
        conversation.waiting = None
    try:
        with connect(url, additional_headers={"Authorization": "Bearer " + token},
                     open_timeout=min(30, timeout), close_timeout=5,
                     max_size=32 * 1024 * 1024, proxy=None) as socket:
            gateway = Gateway(socket, deadline)
            try:
                if waiting:
                    answer_question(gateway, completion, waiting[1], prompt)
                else:
                    receipt = gateway.rpc("hive.message.send", message_id=completion.message_id, text=prompt)
                    completion.response(receipt)
                answer = await_answer(gateway, completion, questions=conversation is not None)
                if isinstance(answer, Question):
                    conversation.waiting = (completion, answer)
                    return answer.text
                return answer
            except BaseException:
                stop_tasks(gateway, completion)
                raise
    except WebSocketException as error:
        # Keep authentication headers and connection internals out of stderr.
        close = getattr(error, "rcvd", None) or getattr(error, "sent", None)
        code = f" {close.code}" if close is not None else ""
        raise RuntimeError(f"DigitalMate Gateway connection failed: {type(error).__name__}{code}") from error
