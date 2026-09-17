from __future__ import annotations

import unittest

from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceNotFoundError,
)

from foundry_databricks_agent.cosmos_store import (
    ConversationConflictError,
    CosmosConversationSessionStore,
)


class _FakeContainer:
    def __init__(self, item: dict | None = None) -> None:
        self.item = item
        self.upserts: list[tuple[dict, dict]] = []
        self.deleted: list[tuple[str, str]] = []
        self.conflict = False

    async def read_item(self, item: str, partition_key: str) -> dict:
        stored = self.item
        if stored is None or stored["id"] != item or stored["ownerId"] != partition_key:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return dict(stored)

    async def upsert_item(self, body: dict, **kwargs: object) -> dict:
        if self.conflict:
            raise CosmosAccessConditionFailedError(status_code=412, message="conflict")
        self.upserts.append((body, dict(kwargs)))
        return body

    async def delete_item(self, item: str, partition_key: str) -> None:
        self.deleted.append((item, partition_key))


def _store(container: _FakeContainer) -> CosmosConversationSessionStore:
    store = CosmosConversationSessionStore(
        endpoint="https://example.documents.azure.com:443/",
        database="agent",
        container="conversations",
        credential=object(),
    )
    store._container = container
    return store


class CosmosConversationSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_a_new_conversation_under_its_owner(self) -> None:
        container = _FakeContainer()
        store = _store(container)

        async with store.session("user-a", "conv-1") as session:
            session.state["marker"] = "kept"

        (document, conditions), = container.upserts
        self.assertEqual("conv-1", document["id"])
        self.assertEqual("user-a", document["ownerId"])
        self.assertEqual("kept", document["session"]["state"]["marker"])
        self.assertIn("ttl", document)
        self.assertEqual({}, conditions)

    async def test_restores_stored_history_and_sends_etag(self) -> None:
        container = _FakeContainer(
            {
                "id": "conv-1",
                "ownerId": "user-a",
                "_etag": "etag-1",
                "session": {
                    "type": "session",
                    "session_id": "conv-1",
                    "service_session_id": None,
                    "state": {"marker": "from-cosmos"},
                },
            }
        )
        store = _store(container)

        async with store.session("user-a", "conv-1") as session:
            self.assertEqual("from-cosmos", session.state["marker"])

        _, conditions = container.upserts[0]
        self.assertEqual("etag-1", conditions.get("etag"))

    async def test_a_failed_turn_does_not_overwrite_history(self) -> None:
        container = _FakeContainer()
        store = _store(container)

        with self.assertRaises(RuntimeError):
            async with store.session("user-a", "conv-1"):
                raise RuntimeError("model call failed")

        self.assertEqual([], container.upserts)

    async def test_concurrent_write_surfaces_as_conflict(self) -> None:
        container = _FakeContainer()
        container.conflict = True
        store = _store(container)

        with self.assertRaises(ConversationConflictError):
            async with store.session("user-a", "conv-1"):
                pass

    async def test_unreadable_document_starts_a_fresh_conversation(self) -> None:
        container = _FakeContainer(
            {"id": "conv-1", "ownerId": "user-a", "session": {"bogus": True}}
        )
        store = _store(container)

        async with store.session("user-a", "conv-1") as session:
            self.assertEqual("conv-1", session.session_id)
            self.assertEqual({}, session.state)

    async def test_reset_deletes_the_conversation(self) -> None:
        container = _FakeContainer()
        store = _store(container)

        await store.reset("user-a", "conv-1")

        self.assertEqual([("conv-1", "user-a")], container.deleted)


if __name__ == "__main__":
    unittest.main()
