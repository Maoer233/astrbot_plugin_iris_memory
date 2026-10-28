"""主动回复发送者忽略门控测试

覆盖点：
- 混合写法的 ID 清单解析（纯 ID / 平台前缀 / 带昵称）
- 与 astrbot_plugin_completely_disable 黑名单的同步（含白名单模式拒绝同步）
- 自身 ID 永不被忽略
- 手写名单与外部名单叠加
- 滑窗清扫与 watch 过滤
"""

import json
import os

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.sender_gate import (
    SenderIgnoreList,
    extract_ids,
)


class _Cfg:
    """ConfigManager 替身：只暴露 SenderIgnoreList 用到的属性。"""

    def __init__(self, ids: str = "", sync: bool = True, user_only: bool = False) -> None:
        self.ignore_sender_ids = ids
        self.ignore_sync_external = sync
        self.ignore_sync_user_only = user_only


def _next_refresh_reset(gate) -> None:
    """跳过 10 秒复用窗口，模拟「下一次刷新时机已到」。

    直接改私有字段是有意的：测试要验证的是「刷新后是否重新解析」，
    而不是等待真实时间。
    """
    gate._next_refresh = 0.0


def _write_external(config_dir, payload: dict) -> None:
    (config_dir / "astrbot_plugin_completely_disable_config.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


class TestExtractIds:
    def test_mixed_forms(self):
        raw = [
            "100000005",
            "qq:FriendMessage:100000000",
            "@茧墨（肥鱼版）(100000005)",
        ]
        assert extract_ids(raw) == {"100000005", "100000000"}

    def test_nickname_with_extra_digits(self):
        # 昵称里带年份时，必须取最后一段数字（括号内真实 ID）
        assert extract_ids(["@玩家2024(1234567)"]) == {"1234567"}

    def test_separators(self):
        assert extract_ids("100000005,100000000") == {"100000005", "100000000"}
        assert extract_ids("100000005\n100000000") == {"100000005", "100000000"}
        assert extract_ids("100000005 100000000") == {"100000005", "100000000"}
        # 斜杠/加号/与号也要能拆开，否则前一个 ID 会被吞掉
        assert extract_ids("100000005/100000000") == {"100000005", "100000000"}
        assert extract_ids("100000005+100000000") == {"100000005", "100000000"}
        assert extract_ids("100000005&100000000") == {"100000005", "100000000"}

    @pytest.mark.parametrize("raw", [None, "", "  ", "None", "abc"])
    def test_no_id_returns_empty(self, raw):
        assert extract_ids(raw) == set()

    @pytest.mark.parametrize(
        "raw",
        [
            "@玩家20245",  # 昵称自带 5 位数字，无括号 ID
            "@玩家2024",  # 昵称自带年份
            "@玩家 20245",  # 昵称与数字间有空格：分隔符会把它拆成两段
            "@John 20245",
            "@群友 88888888",
            "1234",  # 不足 5 位
            "２８３５６７５８５５",  # 全角数字：平台 sender_id 恒为半角
        ],
    )
    def test_ambiguous_input_is_rejected(self, raw):
        """格式不明确时宁可不解析：误伤无辜用户比漏配严重。"""
        assert extract_ids([raw]) == set()

    def test_paren_id_wins_over_trailing_digits(self):
        """括号后的额外数字不能覆盖括号里的真 ID。"""
        assert extract_ids(["@茧墨(100000005)123456"]) == {"100000005"}

    def test_at_prefixed_bare_id(self):
        assert extract_ids(["@100000005"]) == {"100000005"}

    def test_fullwidth_colon(self):
        assert extract_ids(["qq：100000000"]) == {"100000000"}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # 顺序必须对称：@昵称(ID) 在前时，后面的裸数字也是独立条目
            ("888888, @某人(999999)", {"888888", "999999"}),
            ("@某人(999999),888888", {"888888", "999999"}),
            ("@茧墨（肥鱼版）(100000005),100000000", {"100000005", "100000000"}),
            (
                "@茧墨（肥鱼版）(100000005)\n100000000\n100000000",
                {"100000005", "100000000", "100000000"},
            ),
        ],
    )
    def test_order_symmetry(self, raw, expected):
        """``@昵称(ID)`` 在前时不能吞掉其后的裸数字 ID。

        曾因只判「上一 token 含 @」而不判「上一 token 是否已产出 ID」，
        导致 ``@某人(999999),888888`` 只解析出 999999。
        """
        assert extract_ids([raw]) == expected


class TestExternalSync:
    def test_blacklist_mode_syncs(self, tmp_path):
        _write_external(
            tmp_path,
            {"mode": "blacklist", "disable_list": ["100000005", "@茧墨(1234567)"]},
        )
        gate = SenderIgnoreList(_Cfg(), config_dir=tmp_path)
        assert gate.snapshot() == {"100000005", "1234567"}
        assert gate.is_ignored("100000005")
        assert not gate.is_ignored("999")

    def test_whitelist_mode_does_not_sync(self, tmp_path):
        # 白名单模式下列表语义相反，同步会把「除了名单内的人」全忽略
        _write_external(tmp_path, {"mode": "whitelist", "disable_list": ["100000005"]})
        assert SenderIgnoreList(_Cfg(), config_dir=tmp_path).snapshot() == set()

    def test_empty_mode_falls_back_to_blacklist(self, tmp_path):
        # data.get("mode", default) 只在键缺失时兜底；空串会让同步静默失效
        _write_external(tmp_path, {"mode": "", "disable_list": ["100000005"]})
        assert SenderIgnoreList(_Cfg(), config_dir=tmp_path).snapshot() == {
            "100000005"
        }

    def test_missing_mode_key_defaults_to_blacklist(self, tmp_path):
        _write_external(tmp_path, {"disable_list": ["100000005"]})
        assert SenderIgnoreList(_Cfg(), config_dir=tmp_path).snapshot() == {
            "100000005"
        }

    def test_uppercase_mode_is_normalized(self, tmp_path):
        _write_external(tmp_path, {"mode": "BLACKLIST", "disable_list": ["100000005"]})
        assert SenderIgnoreList(_Cfg(), config_dir=tmp_path).snapshot() == {
            "100000005"
        }

    def test_user_only_skips_bare_numbers(self, tmp_path):
        """源插件的表是「群号+好友号」混合体，严格模式只收明确是用户的条目。"""
        _write_external(
            tmp_path,
            {
                "mode": "blacklist",
                "disable_list": [
                    "100000004",
                    "qq:FriendMessage:100000000",
                    "@茧墨（肥鱼版）(100000005)",
                ],
            },
        )
        assert SenderIgnoreList(_Cfg(user_only=True), config_dir=tmp_path).snapshot() == {
            "100000000",
            "100000005",
        }

    def test_compat_mode_keeps_bare_numbers(self, tmp_path):
        """默认兼容模式行为不变：纯数字照收。"""
        _write_external(
            tmp_path,
            {"mode": "blacklist", "disable_list": ["100000004", "100000005"]},
        )
        assert SenderIgnoreList(_Cfg(), config_dir=tmp_path).snapshot() == {
            "100000004",
            "100000005",
        }

    def test_missing_file(self, tmp_path):
        assert SenderIgnoreList(_Cfg(), config_dir=tmp_path).snapshot() == set()

    def test_utf8_bom_config_is_readable(self, tmp_path):
        # Windows 记事本保存的 JSON 带 BOM；用 utf-8 严格解码会整张名单失效
        (tmp_path / "astrbot_plugin_completely_disable_config.json").write_text(
            json.dumps({"mode": "blacklist", "disable_list": ["100000005"]}),
            encoding="utf-8-sig",
        )
        gate = SenderIgnoreList(_Cfg(), config_dir=tmp_path)
        assert gate.snapshot() == {"100000005"}

    def test_broken_json(self, tmp_path):
        (tmp_path / "astrbot_plugin_completely_disable_config.json").write_text(
            "{not json", encoding="utf-8"
        )
        assert SenderIgnoreList(_Cfg(), config_dir=tmp_path).snapshot() == set()

    def test_sync_disabled(self, tmp_path):
        _write_external(tmp_path, {"mode": "blacklist", "disable_list": ["100000005"]})
        gate = SenderIgnoreList(_Cfg(sync=False), config_dir=tmp_path)
        assert gate.snapshot() == set()

    def test_external_file_change_is_picked_up(self, tmp_path):
        path = tmp_path / "astrbot_plugin_completely_disable_config.json"
        _write_external(tmp_path, {"mode": "blacklist", "disable_list": ["111111"]})
        os.utime(path, (1_000_000, 1_000_000))
        gate = SenderIgnoreList(_Cfg(), config_dir=tmp_path)
        assert gate.snapshot() == {"111111"}

        _write_external(tmp_path, {"mode": "blacklist", "disable_list": ["222222"]})
        # 显式设置不同 mtime：快速连写可能落在同一时间片，缓存会误判未变
        os.utime(path, (2_000_000, 2_000_000))
        gate._next_refresh = 0.0  # 跳过 10 秒复用窗口，模拟下一轮刷新
        assert gate.snapshot() == {"222222"}


class TestMergedList:
    def test_own_and_external_merge(self, tmp_path):
        _write_external(tmp_path, {"mode": "blacklist", "disable_list": ["100000005"]})
        gate = SenderIgnoreList(_Cfg(ids="888888, @某人(999999)"), config_dir=tmp_path)
        assert gate.snapshot() == {"888888", "999999", "100000005"}

    def test_self_never_ignored(self, tmp_path):
        _write_external(tmp_path, {"mode": "blacklist", "disable_list": ["100000005"]})
        gate = SenderIgnoreList(_Cfg(), config_dir=tmp_path)
        assert gate.is_ignored("100000005", self_id="100000005") is False

    @pytest.mark.parametrize("sender", ["", None])
    def test_empty_sender(self, sender):
        gate = SenderIgnoreList(_Cfg(ids="123456"), config_dir=None)
        assert gate.is_ignored(sender) is False

    def test_own_list_change_is_immediate(self):
        """WebUI 改手写名单必须立即生效，不能等 10 秒复用窗口。"""
        gate = SenderIgnoreList(_Cfg(ids=""), config_dir=None)
        assert gate.is_ignored("100000005") is False

        gate._config.ignore_sender_ids = "100000005"
        assert gate.is_ignored("100000005") is True

        gate._config.ignore_sender_ids = ""
        assert gate.is_ignored("100000005") is False


class TestChangeCallback:
    def test_callback_fires_once_on_change(self, tmp_path):
        seen: list[set[str]] = []
        gate = SenderIgnoreList(_Cfg(ids="111111"), config_dir=tmp_path)
        gate.on_change = lambda ids: seen.append(set(ids))

        gate.snapshot()
        assert seen == [{"111111"}]

        _next_refresh_reset(gate)
        gate.snapshot()  # 名单未变，不重复回调
        assert seen == [{"111111"}]

        gate._config = _Cfg(ids="222222")
        _next_refresh_reset(gate)
        gate.snapshot()
        assert seen == [{"111111"}, {"222222"}]


class TestPoolPurge:
    def test_purge_senders_removes_ignored_only(self, config, make_msg):
        from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
            SlidingWindow,
        )

        window = SlidingWindow(config)
        window.append("g1", make_msg(sender_id="100000005"))
        window.append("g1", make_msg(sender_id="u1"))
        window.append("g2", make_msg(sender_id="100000005"))

        removed = window.purge_senders(set(), {"100000005"})
        assert removed == 2
        assert [m.sender_id for m in window.get_messages("g1")] == ["u1"]
        assert window.get_messages("g2") == []

    def test_purge_scoped_to_groups(self, config, make_msg):
        from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
            SlidingWindow,
        )

        window = SlidingWindow(config)
        window.append("g1", make_msg(sender_id="100000005"))
        window.append("g2", make_msg(sender_id="100000005"))

        assert window.purge_senders({"g1"}, {"100000005"}) == 1
        assert window.get_messages("g1") == []
        assert len(window.get_messages("g2")) == 1

    def test_purge_empty_set_is_noop(self, config, make_msg):
        from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
            SlidingWindow,
        )

        window = SlidingWindow(config)
        window.append("g1", make_msg(sender_id="u1"))
        assert window.purge_senders(set(), set()) == 0
        assert len(window.get_messages("g1")) == 1

    def test_purge_protects_self_id(self, config, make_msg):
        """自身 ID 即使出现在名单里也不删：与 is_ignored 的自我豁免保持一致。"""
        from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
            SlidingWindow,
        )

        window = SlidingWindow(config)
        window.append("g1", make_msg(sender_id="bot-1"))
        window.append("g1", make_msg(sender_id="100000005"))

        removed = window.purge_senders(
            set(), {"bot-1", "100000005"}, protected_ids={"bot-1"}
        )
        assert removed == 1
        assert [m.sender_id for m in window.get_messages("g1")] == ["bot-1"]

    def test_purge_with_only_protected_ids_is_noop(self, config, make_msg):
        from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
            SlidingWindow,
        )

        window = SlidingWindow(config)
        window.append("g1", make_msg(sender_id="bot-1"))
        assert window.purge_senders(set(), {"bot-1"}, protected_ids={"bot-1"}) == 0
        assert len(window.get_messages("g1")) == 1
