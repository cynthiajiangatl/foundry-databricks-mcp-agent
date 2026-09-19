from __future__ import annotations

import unittest

from foundry_databricks_agent.webapp import _parse_starters


class ParseStartersTests(unittest.TestCase):
    def test_extracts_array_surrounded_by_prose(self) -> None:
        reply = 'Sure! ["How many tickets are open?", "Status of ticket 12?"] Hope that helps.'
        self.assertEqual(
            ["How many tickets are open?", "Status of ticket 12?"], _parse_starters(reply)
        )

    def test_drops_non_strings_and_blanks_and_caps_at_four(self) -> None:
        reply = '["a", "", "  ", 7, null, "b", "c", "d", "e", "f"]'
        self.assertEqual(["a", "b", "c", "d"], _parse_starters(reply))

    def test_strips_whitespace(self) -> None:
        self.assertEqual(["a"], _parse_starters('["  a  "]'))

    def test_salvages_an_array_wrapped_in_an_object(self) -> None:
        self.assertEqual(["a", "b"], _parse_starters('{"questions": ["a", "b"]}'))

    def test_returns_empty_for_unusable_replies(self) -> None:
        for reply in ("no json here", "", "[not valid json", "[]", '{"a": 1}'):
            self.assertEqual([], _parse_starters(reply), reply)


if __name__ == "__main__":
    unittest.main()
