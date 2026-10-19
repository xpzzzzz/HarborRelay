
from __future__ import annotations

import json
import logging
from datetime import datetime
from importlib import import_module
from time import monotonic
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import ChatMessage
from app.schemas.dtos import AiMessage
from app.services.privacy import PrivacySanitizer


logger = logging.getLogger(__name__)


class MemoryCompactionSettings(Protocol):
    memory_compaction_enabled: bool
    memory_compaction_recent_messages: int
    memory_summary_max_chars: int


class RedisShortTermMemoryStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self._retry_after = 0.0
        self.client = self._connect()

    def load_recent(self, session_public_id: str, *, expected_message_ids: list[int] | None = None) -> list[AiMessage]:
        if not self._available():
            return []
        try:
            return self._read(session_public_id, self.settings.redis_memory_max_messages, expected_message_ids)
        except Exception as exc:
            self._defer_retry()
            logger.warning("Redis memory read unavailable: %s", exc)
            return []

    def messages_from_rows(self, rows: list[ChatMessage]) -> list[AiMessage]:
        return [self._message_from_row(row) for row in rows]

    def append(self, session_public_id: str, role: str, content: str, *, message_id: int | None = None) -> None:
        if not self._available():
            return
        key = self._key(session_public_id)
        payload = self._serialize(role, content, message_id)
        try:
            with self.client.pipeline(transaction=True) as pipe:
                pipe.rpush(key, payload)
                pipe.ltrim(key, -self.settings.redis_memory_max_messages, -1)
                pipe.expire(key, self.settings.redis_memory_ttl_seconds)
                pipe.execute()
        except Exception as exc:
            self._defer_retry()
            logger.warning("Redis memory append unavailable: %s", exc)

    def replace(self, session_public_id: str, messages: list[AiMessage], *, message_ids: list[int] | None = None) -> None:
        if message_ids is not None and len(message_ids) != len(messages):
            raise ValueError("Cache message IDs must match the message window")
        if not self._available():
            return
        key = self._key(session_public_id)
        try:
            with self.client.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                if messages:
                    ids = message_ids if message_ids is not None else [None] * len(messages)
                    pipe.rpush(key, *[self._serialize(message.role, message.content, identifier) for message, identifier in zip(messages, ids)])
                    pipe.ltrim(key, -self.settings.redis_memory_max_messages, -1)
                    pipe.expire(key, self.settings.redis_memory_ttl_seconds)
                pipe.execute()
        except Exception as exc:
            self._defer_retry()
            logger.warning("Redis memory replace unavailable: %s", exc)

    def _read(self, session_public_id: str, limit: int, expected_message_ids: list[int] | None = None) -> list[AiMessage]:
        raw_items = self.client.lrange(self._key(session_public_id), -limit, -1)
        messages = []
        identifiers = []
        for raw in raw_items:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            role = data.get("role")
            content = data.get("content")
            if not isinstance(role, str) or not isinstance(content, str):
                continue
            role = role.lower()
            # Redis holds conversation data, never privileged prompt instructions.
            if role in {"user", "assistant"} and content:
                messages.append(AiMessage(role=role, content=self.privacy.sanitize(content)))
                identifier = data.get("messageId")
                identifiers.append(identifier if type(identifier) is int and identifier > 0 else None)
        # Compare the entire DB-visible window, not just the last ID: a missing
        # middle append, duplicate EXEC or out-of-order writer must be a miss.
        if expected_message_ids is not None and identifiers != expected_message_ids:
            return []
        return messages

    def _connect(self):
        try:
            redis_module = import_module("redis")
        except ModuleNotFoundError as exc:
            raise RuntimeError("请先安装 requirements.txt 中的 redis 依赖") from exc
        client = redis_module.Redis.from_url(
            self.settings.redis_url,
            decode_responses=True,
            socket_timeout=self.settings.redis_socket_timeout_seconds,
            socket_connect_timeout=self.settings.redis_socket_timeout_seconds,
        )
        try:
            client.ping()
        except Exception as exc:
            self._defer_retry()
            logger.warning("Redis memory initially unavailable; later calls may reconnect: %s", exc)
        return client

    def _available(self) -> bool:
        return self.client is not None and monotonic() >= getattr(self, "_retry_after", 0)

    def _defer_retry(self) -> None:
        # Keep a failed cache from imposing repeated socket waits in the same turn.
        self._retry_after = monotonic() + self.settings.redis_socket_timeout_seconds

    def _message_from_row(self, row: ChatMessage) -> AiMessage:
        return AiMessage(role=row.role.lower(), content=self.privacy.sanitize(row.content))

    def _serialize(self, role: str, content: str, message_id: int | None = None) -> str:
        return json.dumps(
            {
                "role": role.lower(),
                "content": self.privacy.sanitize(content),
                "createdAt": datetime.utcnow().isoformat(),
                "messageId": message_id,
            },
            ensure_ascii=False,
        )

    def _key(self, session_public_id: str) -> str:
        return f"mindbridge:short-term-memory:{session_public_id}"


def load_committed_history(db: Session, memory, session_id: int, public_id: str, limit: int) -> tuple[list[AiMessage], str]:
    """Use Redis only when it matches this transaction's committed message window.

    Messages are append-only through the public API. A bounded ID query avoids
    fetching Text columns on cache hits. No cache/database distributed transaction
    is required: races can cause another miss, never authorize stale cache data.
    """
    identifiers = [row[0] for row in (
        db.query(ChatMessage.id)
        .filter(ChatMessage.session_id == session_id,
                ChatMessage.role.in_(["USER", "ASSISTANT", "user", "assistant"]),
                ChatMessage.content != "")
        .order_by(ChatMessage.id.desc()).limit(max(1, limit)).all()
    )][::-1]
    if not identifiers:
        return [], "database_empty"
    try:
        history = memory.load_recent(public_id, expected_message_ids=identifiers)
    except Exception:
        history = []
    if history:
        return history, "redis_verified"
    rows = (db.query(ChatMessage).filter(ChatMessage.session_id == session_id, ChatMessage.id.in_(identifiers))
            .order_by(ChatMessage.id.asc()).all())
    privacy = PrivacySanitizer()
    history = [AiMessage(role=row.role.lower(), content=privacy.sanitize(row.content)) for row in rows]
    try:
        memory.replace(public_id, history, message_ids=[row.id for row in rows])
    except Exception:
        pass  # DB history is already available; Redis remains optional.
    return history, "mysql_seeded"  # Preserve the historical trace label (also used with SQLite).


def compact_history_for_prompt(
    history: list[AiMessage],
    settings: MemoryCompactionSettings,
    current_input: str = "",
) -> tuple[list[AiMessage], str]:
    """Return bounded prompt history plus a student-safe memory brief.

    The summary is deterministic and avoids diagnostic labels. It is intended
    for prompt context and auditability, not for student-facing display.
    """

    sanitized = [AiMessage(role=item.role, content=PrivacySanitizer().sanitize(item.content)) for item in history]
    if not sanitized:
        return [], "无相关历史记忆。"

    recent_count = max(2, int(getattr(settings, "memory_compaction_recent_messages", 8)))
    max_chars = max(120, int(getattr(settings, "memory_summary_max_chars", 500)))
    brief = summarize_history_for_memory(sanitized, current_input, max_chars)

    if not getattr(settings, "memory_compaction_enabled", True) or len(sanitized) <= recent_count:
        return sanitized, brief

    recent = sanitized[-recent_count:]
    summary_message = AiMessage(
        role="system",
        content=(
            "历史摘要（仅供 HarborRelay 内部上下文使用；不要向学生展示；"
            "不要据此输出诊断、风险等级或后台标签）：\n" + brief
        ),
    )
    return [summary_message, *recent], brief


def summarize_history_for_memory(history: list[AiMessage], current_input: str = "", max_chars: int = 500) -> str:
    privacy = PrivacySanitizer()
    user_points = []
    assistant_points = []
    for message in history:
        content = " ".join(privacy.sanitize(message.content).split())
        if not content:
            continue
        if message.role == "user":
            user_points.append(content)
        elif message.role == "assistant":
            assistant_points.append(content)

    parts = []
    if user_points:
        parts.append("学生近期关注：" + "；".join(_clip(item, 80) for item in user_points[-4:]))
    if assistant_points:
        parts.append("已给过的支持：" + "；".join(_clip(item, 70) for item in assistant_points[-3:]))
    if current_input:
        parts.append("本轮输入关注：" + _clip(privacy.sanitize(current_input), 80))
    if not parts:
        return "无相关历史记忆。"
    return _clip("\n".join(parts), max_chars)


def _clip(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."
