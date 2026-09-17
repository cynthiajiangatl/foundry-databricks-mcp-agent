"""Durable conversation history backed by Azure Cosmos DB.

One document per conversation, partitioned by the authenticated owner, so a conversation
can only be read back under the identity that created it. The stored value is the whole
serialized :class:`~agent_framework.AgentSession` -- with ``store=False`` the chat history
lives in the session's own state, so persisting the session persists the history (plus the
Genie conversation id used to continue an existing Genie conversation).

Authentication is Microsoft Entra ID only (Cosmos data-plane RBAC); no account keys are
read or stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from agent_framework import AgentSession

logger = logging.getLogger(__name__)

# Cosmos rejects documents larger than 2 MB; warn well before a conversation reaches it.
_WARN_DOCUMENT_BYTES = 1_000_000
_DEFAULT_TTL_SECONDS = 30 * 24 * 3600
_MAX_TRACKED_LOCKS = 2048


class ConversationConflictError(RuntimeError):
    """Raised when the same conversation was written concurrently somewhere else."""


class CosmosConversationSessionStore:
    """Load and save agent sessions in Cosmos DB, one document per conversation."""

    def __init__(
        self,
        *,
        endpoint: str,
        database: str,
        container: str,
        credential: Any | None = None,
        ttl_seconds: int | None = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._endpoint = endpoint
        self._database = database
        self._container_name = container
        self._credential = credential
        self._owns_credential = credential is None
        self._ttl_seconds = ttl_seconds
        self._client: Any | None = None
        self._container: Any | None = None
        self._init_lock = asyncio.Lock()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def _ensure_container(self) -> Any:
        if self._container is not None:
            return self._container
        async with self._init_lock:
            if self._container is None:
                from azure.cosmos.aio import CosmosClient

                if self._credential is None:
                    from azure.identity.aio import DefaultAzureCredential

                    self._credential = DefaultAzureCredential()
                self._client = CosmosClient(self._endpoint, credential=self._credential)
                database = self._client.get_database_client(self._database)
                self._container = database.get_container_client(self._container_name)
        return self._container

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        """Serialize turns for one conversation inside this process."""
        lock = self._locks.get(key)
        if lock is None:
            if len(self._locks) >= _MAX_TRACKED_LOCKS:
                for tracked_key, tracked in list(self._locks.items()):
                    if not tracked.locked():
                        del self._locks[tracked_key]
            lock = self._locks.setdefault(key, asyncio.Lock())
        return lock

    @asynccontextmanager
    async def session(
        self, owner_id: str, conversation_id: str
    ) -> AsyncIterator[AgentSession]:
        """Load the conversation, hand it to the caller, then save it back if the turn succeeds."""
        container = await self._ensure_container()
        async with self._lock_for((owner_id, conversation_id)):
            session, etag = await self._load(container, owner_id, conversation_id)
            yield session
            # Only reached when the turn completed: a failed run must not overwrite history.
            await self._save(container, owner_id, conversation_id, session, etag)

    async def reset(self, owner_id: str, conversation_id: str) -> None:
        """Delete a conversation so the next question starts from an empty context."""
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = await self._ensure_container()
        async with self._lock_for((owner_id, conversation_id)):
            try:
                await container.delete_item(item=conversation_id, partition_key=owner_id)
            except CosmosResourceNotFoundError:
                return

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._container = None
        if self._owns_credential and self._credential is not None:
            await self._credential.close()
            self._credential = None

    async def _load(
        self, container: Any, owner_id: str, conversation_id: str
    ) -> tuple[AgentSession, str | None]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            item = await container.read_item(item=conversation_id, partition_key=owner_id)
        except CosmosResourceNotFoundError:
            return AgentSession(session_id=conversation_id), None

        etag = item.get("_etag")
        payload = item.get("session")
        if isinstance(payload, dict):
            try:
                return AgentSession.from_dict(payload), etag
            except Exception:  # noqa: BLE001 - a corrupt document must not wedge the chat
                logger.warning(
                    "Stored conversation %s could not be restored; starting a new one.",
                    conversation_id,
                )
        return AgentSession(session_id=conversation_id), etag

    async def _save(
        self,
        container: Any,
        owner_id: str,
        conversation_id: str,
        session: AgentSession,
        etag: str | None,
    ) -> None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        document: dict[str, Any] = {
            "id": conversation_id,
            "ownerId": owner_id,
            "session": session.to_dict(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        if self._ttl_seconds:
            document["ttl"] = self._ttl_seconds

        size = len(json.dumps(document))
        if size > _WARN_DOCUMENT_BYTES:
            logger.warning(
                "Conversation %s is %d bytes and approaching the 2 MB document limit.",
                conversation_id,
                size,
            )

        conditions: dict[str, Any] = {}
        if etag:
            conditions = {"etag": etag, "match_condition": MatchConditions.IfNotModified}
        try:
            await container.upsert_item(document, **conditions)
        except CosmosAccessConditionFailedError as exc:
            raise ConversationConflictError(
                "This conversation was updated somewhere else. Send the message again."
            ) from exc
