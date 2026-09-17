from __future__ import annotations

import asyncio
import unittest

from foundry_databricks_agent.conversation_store import ConversationSessionStore


class ConversationSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_session_only_for_same_owner_and_conversation(self) -> None:
        store = ConversationSessionStore()

        async with store.session("user-a", "conversation") as first:
            first.state["marker"] = "kept"
        async with store.session("user-a", "conversation") as resumed:
            self.assertIs(first, resumed)
            self.assertEqual("kept", resumed.state["marker"])
        async with store.session("user-b", "conversation") as isolated:
            self.assertIsNot(first, isolated)
            self.assertNotIn("marker", isolated.state)

    async def test_reset_replaces_existing_session(self) -> None:
        store = ConversationSessionStore()

        async with store.session("user", "conversation") as original:
            original.state["marker"] = "discard"
        await store.reset("user", "conversation")
        async with store.session("user", "conversation") as reset:
            self.assertIsNot(original, reset)
            self.assertEqual({}, reset.state)

    async def test_serializes_overlapping_turns_for_one_conversation(self) -> None:
        store = ConversationSessionStore()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def first_turn() -> None:
            async with store.session("user", "conversation"):
                order.append("first-enter")
                first_entered.set()
                await release_first.wait()
                order.append("first-exit")

        async def second_turn() -> None:
            await first_entered.wait()
            async with store.session("user", "conversation"):
                order.append("second-enter")

        first_task = asyncio.create_task(first_turn())
        second_task = asyncio.create_task(second_turn())
        await first_entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(["first-enter"], order)
        release_first.set()
        await asyncio.gather(first_task, second_task)

        self.assertEqual(["first-enter", "first-exit", "second-enter"], order)


if __name__ == "__main__":
    unittest.main()