"""Message bus: the ONLY way agents talk to each other.

Every message is a validated JSON document (sender, receiver, performative,
content, conversation_id) and is written to message_log for traceability.
"""
import uuid
from typing import Callable, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel

from .config import now
from .models import MessageLog

Performative = Literal["REQUEST", "AGREE", "REFUSE", "PROPOSE", "INFORM", "FAILURE"]


class Message(BaseModel):
    sender: str
    receiver: str
    performative: Performative
    content: dict
    conversation_id: str


def new_conversation(prefix: str = "conv") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


Handler = Callable[..., Optional[Tuple[str, dict]]]


class MessageBus:
    def __init__(self) -> None:
        self.handlers: Dict[str, Handler] = {}
        self.listeners: List[str] = []

    def register(self, name: str, handler: Handler, listener: bool = False) -> None:
        self.handlers[name] = handler
        if listener and name not in self.listeners:
            self.listeners.append(name)

    @staticmethod
    def _log(db, m: Message) -> None:
        db.add(MessageLog(conversation_id=m.conversation_id, sender=m.sender, receiver=m.receiver,
                          performative=m.performative, content=m.content, ts=now()))
        db.flush()

    def send(self, db, sender: str, receiver: str, performative: str, content: dict,
             conversation_id: Optional[str] = None) -> Message:
        """Point-to-point message. Returns the receiver's reply (AGREE/REFUSE/PROPOSE/INFORM/FAILURE)."""
        conv = conversation_id or new_conversation()
        msg = Message(sender=sender, receiver=receiver, performative=performative,
                      content=content, conversation_id=conv)
        self._log(db, msg)
        handler = self.handlers.get(receiver)
        if handler is None:
            reply = Message(sender=receiver, receiver=sender, performative="FAILURE",
                            content={"reason": f"unknown agent '{receiver}'"}, conversation_id=conv)
            self._log(db, reply)
            return reply
        out = handler(db, msg)
        if out is None:
            return Message(sender=receiver, receiver=sender, performative="INFORM",
                           content={}, conversation_id=conv)
        perf, body = out
        reply = Message(sender=receiver, receiver=sender, performative=perf,
                        content=body, conversation_id=conv)
        self._log(db, reply)
        return reply

    def broadcast(self, db, sender: str, content: dict, conversation_id: Optional[str] = None) -> str:
        """INFORM sent to every listener agent (reactive behaviour)."""
        conv = conversation_id or new_conversation()
        msg = Message(sender=sender, receiver="ALL", performative="INFORM",
                      content=content, conversation_id=conv)
        self._log(db, msg)
        for name in list(self.listeners):
            if name != sender:
                self.handlers[name](db, msg)
        return conv


bus = MessageBus()


def refuse(reason: str, **extra) -> Tuple[str, dict]:
    return "REFUSE", {"reason": reason, **extra}
