from __future__ import annotations

import base64
import json
import unittest

from foundry_databricks_agent.lakebase import (
    LakebaseQueryError,
    _guard_read_only,
    _identity_from_token,
    _qualified,
    _render,
)


def _token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class GuardReadOnlyTests(unittest.TestCase):
    def test_allows_select_and_cte(self) -> None:
        self.assertEqual("select 1", _guard_read_only("select 1"))
        self.assertEqual("SELECT 1", _guard_read_only("  SELECT 1 ;  "))
        self.assertTrue(_guard_read_only("with x as (select 1) select * from x"))

    def test_rejects_writes_and_ddl(self) -> None:
        for sql in (
            "insert into t values (1)",
            "update t set a = 1",
            "delete from t",
            "drop table t",
            "create table t (a int)",
            "grant select on t to public",
        ):
            with self.assertRaises(LakebaseQueryError, msg=sql):
                _guard_read_only(sql)

    def test_rejects_multiple_statements(self) -> None:
        with self.assertRaises(LakebaseQueryError):
            _guard_read_only("select 1; drop table t")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(LakebaseQueryError):
            _guard_read_only("   ;  ")


class IdentityTests(unittest.TestCase):
    def test_prefers_user_principal_name(self) -> None:
        token = _token({"upn": "user@contoso.com", "appid": "some-app-id"})
        self.assertEqual("user@contoso.com", _identity_from_token(token))

    def test_falls_back_to_service_principal_client_id(self) -> None:
        token = _token({"appid": "1111-2222"})
        self.assertEqual("1111-2222", _identity_from_token(token))

    def test_rejects_token_without_identity(self) -> None:
        with self.assertRaises(LakebaseQueryError):
            _identity_from_token(_token({"roles": ["x"]}))

    def test_rejects_opaque_token(self) -> None:
        with self.assertRaises(LakebaseQueryError):
            _identity_from_token("not-a-jwt")


class RenderTests(unittest.TestCase):
    def test_renders_rows_with_header(self) -> None:
        out = _render(["a", "b"], [(1, None), (2, "x")], truncated=False)
        self.assertEqual("a | b\n1 | \n2 | x", out)

    def test_flags_truncation(self) -> None:
        self.assertIn("rows shown", _render(["a"], [(1,)], truncated=True))

    def test_handles_empty_result(self) -> None:
        self.assertEqual("The query returned no rows.", _render(["a"], [], truncated=False))


class QualifiedNameTests(unittest.TestCase):
    def test_keeps_separate_schema_and_table(self) -> None:
        self.assertEqual(("public", "tickets"), _qualified("public", "tickets"))

    def test_splits_dotted_table_name(self) -> None:
        self.assertEqual(("public", "tickets"), _qualified("", "public.tickets"))
        self.assertEqual(("sales", "tickets"), _qualified("public", "sales.tickets"))

    def test_strips_quotes_and_whitespace(self) -> None:
        self.assertEqual(("public", "tickets"), _qualified(' "public" ', ' "tickets" '))

    def test_rejects_missing_table(self) -> None:
        with self.assertRaises(LakebaseQueryError):
            _qualified("public", "   ")


if __name__ == "__main__":
    unittest.main()
