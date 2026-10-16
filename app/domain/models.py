"""Stable contracts between transport, AI routing and application code.

These types deliberately use only the standard library.  The database and the
existing service still use dictionaries/rows; conversion at the boundary lets
the refactor proceed without changing the financial schema or ledger rules.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class UserRole(str, Enum):
    PLAYER = 'player'
    ADMIN = 'admin'


class ResponseLevel(str, Enum):
    SUMMARY = 'summary'
    DETAILS = 'details'
    FULL = 'full'


class EntityType(str, Enum):
    PLAYER = 'player'
    SESSION = 'session'


class Action(str, Enum):
    """Actions understood by the current application layer.

    Keeping this allow-list here makes an AI response a routing contract,
    rather than an arbitrary command that can reach a repository.
    """

    HELP = 'help'
    CARD = 'card'
    DEBT = 'debt'
    SESSION_INFO = 'session_info'
    IDENTIFY = 'identify'
    PAYMENT_AMOUNT = 'payment_amount'
    DECLARE_DEBT = 'declare_debt'
    ADMIN_RECEIPT = 'admin_receipt'
    CLARIFY = 'clarify'
    CANCEL = 'cancel'
    CONFIG = 'config'
    LIST_PLAYERS = 'list_players'
    LIST_SESSIONS = 'list_sessions'
    REPORT = 'report'
    PENDING = 'pending'
    CONFIRM = 'confirm'
    DENY = 'deny'
    APPROVE = 'approve'
    REJECT = 'reject'
    LINK = 'link'
    BATCH = 'batch'
    NEW_PLAYER = 'new_player'
    NEW_SESSION = 'new_session'
    UPDATE_SESSION = 'update_session'
    ATTENDANCE = 'attendance'
    FINALIZE = 'finalize'
    ADJUSTMENT = 'adjustment'
    SETTINGS = 'settings'
    PLAYER_NOTE = 'player_note'
    SETTLE = 'settle'


@dataclass(frozen=True)
class EntityRef:
    """The small piece of conversation memory needed for a follow-up."""

    type: str
    id: int | str
    title: str | None = None

    def __post_init__(self):
        if isinstance(self.type, EntityType):
            object.__setattr__(self, 'type', self.type.value)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {'type': self.type, 'id': self.id}
        if self.title:
            value['title'] = self.title
        return value

    @classmethod
    def from_dict(cls, value: Any) -> 'EntityRef | None':
        if value is None:
            return None
        if not isinstance(value, dict) or not isinstance(value.get('type'), str):
            raise ValueError('last_entity must be an object with a type and id')
        ident = value.get('id')
        if isinstance(ident, bool) or not isinstance(ident, (int, str)):
            raise ValueError('last_entity.id must be a string or integer')
        title = value.get('title')
        if title is not None and not isinstance(title, str):
            raise ValueError('last_entity.title must be a string')
        return cls(value['type'], ident, title)


@dataclass
class ConversationState:
    """Serializable state, intentionally much smaller than message history."""

    last_entity: EntityRef | None = None
    response_level: ResponseLevel = ResponseLevel.SUMMARY
    receipt: dict[str, Any] | None = None
    identity_phone: str | None = None

    def __post_init__(self):
        if isinstance(self.last_entity, dict):
            self.last_entity = EntityRef.from_dict(self.last_entity)
        if not isinstance(self.response_level, ResponseLevel):
            try:
                self.response_level = ResponseLevel(self.response_level)
            except (TypeError, ValueError):
                self.response_level = ResponseLevel.SUMMARY

    @classmethod
    def from_dict(cls, value: Any) -> 'ConversationState':
        if not isinstance(value, dict):
            return cls()
        raw_level = value.get('response_level', ResponseLevel.SUMMARY.value)
        try:
            level = ResponseLevel(raw_level)
        except (TypeError, ValueError):
            level = ResponseLevel.SUMMARY
        receipt = value.get('receipt')
        if receipt is not None and not isinstance(receipt, dict):
            receipt = None
        phone = value.get('identity_phone')
        return cls(EntityRef.from_dict(value.get('last_entity')), level, receipt,
                   phone if isinstance(phone, str) else None)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {'response_level': self.response_level.value}
        if self.last_entity:
            value['last_entity'] = self.last_entity.to_dict()
        if self.receipt is not None:
            value['receipt'] = self.receipt
        if self.identity_phone is not None:
            value['identity_phone'] = self.identity_phone
        return value


@dataclass(frozen=True)
class Intent:
    """Validated, side-effect-free result of AI/deterministic routing."""

    action: Action
    args: dict[str, Any] = field(default_factory=dict)
    question: str | None = None

    def __post_init__(self):
        if not isinstance(self.action, Action):
            try:
                object.__setattr__(self, 'action', Action(self.action))
            except (TypeError, ValueError):
                raise ValueError('intent action is not supported') from None
        if not isinstance(self.args, dict):
            raise ValueError('intent args must be an object')

    @classmethod
    def from_dict(cls, value: Any) -> 'Intent':
        if not isinstance(value, dict):
            raise ValueError('intent must be an object')
        raw_action = value.get('action')
        args = value.get('args', {})
        if not isinstance(args, dict):
            raise ValueError('intent args must be an object')
        question = value.get('question')
        if question is not None and not isinstance(question, str):
            raise ValueError('intent question must be a string')
        return cls(raw_action, dict(args), question)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {'action': self.action.value, 'args': dict(self.args)}
        if self.question is not None:
            value['question'] = self.question
        return value