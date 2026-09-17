"""Application-owned storage for Microsoft Agent Framework conversation sessions.

Sessions are kept per authenticated owner so a client-supplied conversation id can never
reach another user's conversation, and turns for one conversation are serialized so two
overlapping requests cannot interleave into the same history.

This store is process-local and bounded. It is sufficient for a single replica; running
multiple replicas requires either session affinity or a shared store.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from agent_framework import AgentSession


class ConversationCapacityError(RuntimeError):
    """Raised when every stored conversation is active and capacity is exhausted."""


@dataclass
class _ConversationEntry:
    session: AgentSession
    last_access: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConversationSessionStore:
    """Keep bounded, user-isolated Agent Framework sessions in process memory."""

    def __init__(
        self,
        *,
        max_sessions: int = 1000,
        ttl_seconds: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")
        self._max_sessions = max_sessions
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[tuple[str, str], _ConversationEntry] = {}
        self._entries_lock = asyncio.Lock()

    @asynccontextmanager
    async def session(
        self,
        owner_id: str,
        conversation_id: str,
    ) -> AsyncIterator[AgentSession]:
        """Lease one session and serialize turns that target the same conversation."""
        entry = await self._get_or_create(owner_id, conversation_id)
        async with entry.lock:
            entry.last_access = self._clock()
            try:
                yield entry.session
            finally:
                entry.last_access = self._clock()

    async def reset(self, owner_id: str, conversation_id: str) -> None:
        """Replace a conversation's session after any in-flight turn completes."""
        key = (owner_id, conversation_id)
        async with self._entries_lock:
            entry = self._entries.get(key)
        if entry is None:
            return
        async with entry.lock:
            entry.session = AgentSession(session_id=conversation_id)
            entry.last_access = self._clock()

    async def _get_or_create(
        self,
        owner_id: str,
        conversation_id: str,
    ) -> _ConversationEntry:
        key = (owner_id, conversation_id)
        async with self._entries_lock:
            now = self._clock()
            self._evict_expired(now)
            entry = self._entries.get(key)
            if entry is not None:
                return entry

            if len(self._entries) >= self._max_sessions:
                self._evict_oldest_idle()
            if len(self._entries) >= self._max_sessions:
                raise ConversationCapacityError(
                    "Conversation capacity is temporarily exhausted."
                )

            entry = _ConversationEntry(
                session=AgentSession(session_id=conversation_id),
                last_access=now,
            )
            self._entries[key] = entry
            return entry

    def _evict_expired(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if not entry.lock.locked() and now - entry.last_access >= self._ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)

    def _evict_oldest_idle(self) -> None:
        idle = (
            (key, entry) for key, entry in self._entries.items() if not entry.lock.locked()
        )
        oldest = min(idle, key=lambda item: item[1].last_access, default=None)
        if oldest is not None:
            self._entries.pop(oldest[0], None)
