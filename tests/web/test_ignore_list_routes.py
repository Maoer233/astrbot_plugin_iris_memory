"""忽略名单 Web API 的纯函数测试（不启 HTTP 服务）。

只测可脱离 quart 请求上下文的部分：原文拆分、行状态判定、写回序列化。
HTTP handler 依赖 request/jsonify 上下文，留给集成验证。
"""

import pytest

from astrbot_plugin_iris_memory.iris_memory.web.routes.ignore_list_routes import (
    _build_rows,
    _serialize,
    _split_entries,
)


class TestSplitEntries:
    def test_empty(self):
        assert _split_entries("") == []
        assert _split_entries("   ") == []

    def test_single(self):
        assert _split_entries("100000005") == ["100000005"]

    @pytest.mark.parametrize(
        "raw",
        [
            "100000005,100000000",
            "100000005，100000000",
            "100000005\n100000000",
            "100000005 100000000",
            "100000005、100000000",
            "100000005;100000000",
            "100000005/100000000",
            "100000005|100000000",
        ],
    )
    def test_separators(self, raw):
        assert _split_entries(raw) == ["100000005", "100000000"]

    def test_keeps_nickname_form(self):
        """原始写法要保留，界面上回显用户自己写的样子。"""
        assert _split_entries("@茧墨（肥鱼版）(100000005)") == [
            "@茧墨（肥鱼版）(100000005)"
        ]

    def test_drops_blank_between(self):
        assert _split_entries("a,,b,  ,c") == ["a", "b", "c"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # 带空格的昵称整体是一条，不能被空白切开
            ("@某人 (111111)", ["@某人 (111111)"]),
            ("@John Smith(111111)", ["@John Smith(111111)"]),
            ("@某 人(111111)", ["@某 人(111111)"]),
            # 两段各自已含 ID：必须切成两条
            ("@a(111111) @b(222222)", ["@a(111111)", "@b(222222)"]),
            # 含 ID 的昵称 + 后面的裸 ID：切成两条
            ("@某人(111111) 100000005", ["@某人(111111)", "100000005"]),
            # 无 ID 的「昵称 + 裸数字」写法保持一条（后续判定为格式无效）
            ("@玩家 20245", ["@玩家 20245"]),
        ],
    )
    def test_blank_inside_nickname_is_not_a_separator(self, raw, expected):
        """空白是否分隔取决于上下文。

        早期实现用 ``\\s`` 无条件切分，``@某人 (111111)`` 会变成
        ``@某人`` + ``(111111)`` 两条，前者解析不出 ID，
        界面就多出一行莫名的「格式无效」。
        """
        assert _split_entries(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "100000006",
            "100000005,100000000",
            "@某人 (111111)",
            "@John Smith(111111)",
            "@a(111111) @b(222222)",
            "qq:FriendMessage:100000000",
            "@茧墨（肥鱼版）(100000005)",
        ],
    )
    def test_roundtrip_is_stable(self, raw):
        """保存后读回不能变形，否则每次保存都会累积垃圾。"""
        entries = _split_entries(raw)
        assert _split_entries(_serialize(entries)) == entries


class TestBuildRows:
    def test_active_rows(self):
        rows = _build_rows("100000005,100000000", {"100000005", "100000000"})
        assert [r["status"] for r in rows] == ["ok", "ok"]
        assert all(r["active"] for r in rows)

    def test_not_active_when_missing_from_effective(self):
        rows = _build_rows("100000005", set())
        assert rows[0]["status"] == "not_active"
        assert rows[0]["active"] is False

    @pytest.mark.parametrize("raw", ["@玩家20245", "abc", "1234", "None"])
    def test_invalid_rows(self, raw):
        rows = _build_rows(raw, set())
        assert rows[0]["status"] == "invalid"
        assert rows[0]["ids"] == []

    def test_nickname_form_resolves(self):
        rows = _build_rows("@茧墨（肥鱼版）(100000005)", {"100000005"})
        assert rows[0]["ids"] == ["100000005"]
        assert rows[0]["status"] == "ok"

    def test_empty_list(self):
        assert _build_rows("", set()) == []

    def test_index_is_sequential(self):
        rows = _build_rows("1,2,3".replace("1", "111111").replace("2", "222222").replace("3", "333333"), set())
        assert [r["index"] for r in rows] == [0, 1, 2]

    def test_mixed_statuses(self):
        raw = "@茧墨（肥鱼版）(100000005),999999888,@玩家20245"
        rows = _build_rows(raw, {"100000005"})
        assert rows[0]["status"] == "ok"
        assert rows[1]["status"] == "not_active"
        assert rows[2]["status"] == "invalid"


class TestExternalRows:
    """外部黑名单同步来的条目必须列出。

    否则页面会出现「手写 0 条、却显示生效 2 个」的矛盾，
    用户完全无法判断到底拦了谁。
    """

    def test_external_only_list(self):
        effective = {"100000000", "100000005"}
        rows = _build_rows("", effective, effective)
        assert len(rows) == 2
        assert all(r["source"] == "external" for r in rows)
        assert all(r["status"] == "ok" for r in rows)
        assert {r["raw"] for r in rows} == effective

    def test_own_rows_marked_as_own(self):
        rows = _build_rows("100000005", {"100000005"}, set())
        assert rows[0]["source"] == "own"

    def test_no_duplicate_between_own_and_external(self):
        rows = _build_rows(
            "100000005", {"100000005", "100000000"}, {"100000005", "100000000"}
        )
        own = [r for r in rows if r["source"] == "own"]
        ext = [r for r in rows if r["source"] == "external"]
        assert len(own) == 1
        assert len(ext) == 1, "手写已覆盖的 ID 不该再从外部出现一次"
        assert ext[0]["ids"] == ["100000000"]

    def test_external_empty_means_no_rows(self):
        assert _build_rows("", {"100000005"}, set()) == []

    def test_external_not_active(self):
        rows = _build_rows("", {"100000005"}, {"100000005", "100000000"})
        by_id = {r["ids"][0]: r["status"] for r in rows}
        assert by_id["100000005"] == "ok"
        assert by_id["100000000"] == "not_active"

    def test_external_ids_are_sorted(self):
        rows = _build_rows("", {"333333", "111111"}, {"333333", "111111"})
        assert [r["raw"] for r in rows] == ["111111", "333333"]


class TestSerialize:
    def test_empty(self):
        assert _serialize([]) == ""

    def test_strips_and_drops_blank(self):
        assert _serialize([" 100000005 ", "", "   "]) == "100000005"

    def test_joins_with_comma(self):
        assert _serialize(["111111", "222222"]) == "111111,222222"

    def test_preserves_nickname_form(self):
        assert _serialize(["@茧墨（肥鱼版）(100000005)"]) == "@茧墨（肥鱼版）(100000005)"

    def test_roundtrip(self):
        """序列化后再拆分应还原同样条目。"""
        entries = ["100000005", "@某人(111111)"]
        assert _split_entries(_serialize(entries)) == entries
