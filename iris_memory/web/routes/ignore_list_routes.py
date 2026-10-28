"""
主动回复忽略名单 API 路由

为 WebUI「忽略名单」子页面提供批量管理能力，一人一行。

设计要点：
- 手写名单存于隐藏配置 ``reply_ignore_sender_ids``（逗号分隔的原文），
  本路由只负责解析/回写，实际存储仍由 ``HiddenConfigManager`` 负责，
  保证与命令 ``/iris_reply ignore``、隐藏参数页共用同一份数据。
- 读取时额外返回「生效状态」：某些条目可能来自外部黑名单同步、
  也可能因格式无法解析而不生效，界面上必须能看出来，否则用户会
  以为配上了却毫无效果。
"""

from __future__ import annotations

import re
from typing import Any

from quart import jsonify, request

from ...config import get_config
from ...core import get_logger
from ...proactive.sender_gate import extract_ids

logger = get_logger("web.ignore_list")

PLUGIN_NAME = "astrbot_plugin_iris_memory"
_MODULE_KEY = "reply_ignore_sender_ids"

# 硬分隔符：逗号类 + 管道 + 斜杠 + 加号 + 与号。这些永远视为条目边界。
_HARD_SEP = re.compile(r"[,，、;；|/+&]+")
# 空白：仅在「两侧都不是同一条写法的一部分」时才作分隔，见 _split_entries。
_BLANK_SEP = re.compile(r"\s+")
# 纯 ID（5 位以上半角数字）
_BARE_ID = re.compile(r"^\d{5,}$", re.ASCII)
# 已自包含 ID 的写法：括号或冒号里已有 ID，无需向后拼接
_HAS_ID = re.compile(r"[（(]\s*\d{5,}\s*[)）]|[:：]\s*\d{5,}\s*$", re.ASCII)


def _split_entries(raw: str) -> list[str]:
    """把手写名单原文拆成条目（保留原始写法，便于回显）。

    空白是否作为分隔符，取决于上下文：

    - ``100000005 100000000``：两侧都是裸 ID → 切成两条
    - ``@某人 (111111)``：前段是带 ``@`` 的昵称，空格属于写法的一部分
      → 粘成一条，否则会多出一行「格式无效」的 ``@某人``
    - ``@a(111111) @b(222222)``：两段各自已含 ID → 切成两条

    判定依据是「当前段是否已经产出 ID」：未产出且以 ``@`` 开头时，
    把后续段并入，直到拼出 ID 为止。
    """
    if not raw:
        return []

    entries: list[str] = []
    for chunk in _HARD_SEP.split(str(raw)):
        chunk = chunk.strip()
        if not chunk:
            continue

        parts = [p for p in _BLANK_SEP.split(chunk) if p]
        buf = ""
        for part in parts:
            if not buf:
                buf = part
                continue

            # 已攒出一段但还没有 ID，且它是 @ 昵称：继续并入下一段
            needs_id = (
                "@" in buf and not _HAS_ID.search(buf) and not _BARE_ID.match(buf)
            )
            if needs_id:
                buf = f"{buf} {part}"
                continue

            entries.append(buf)
            buf = part

        if buf:
            entries.append(buf)
    return entries


def _build_rows(
    own_raw: str,
    effective: set[str],
    external: set[str] | None = None,
) -> list[dict[str, Any]]:
    """把名单展开成「一人一行」。

    两类条目都会出现在列表里：

    - ``source="own"``：手写名单（可编辑、可删除）
    - ``source="external"``：从外部黑名单同步而来（只读，需去源插件修改）

    不渲染同步条目会造成「显示 0 条、生效 2 个」的矛盾，用户无从判断
    到底拦了谁，因此必须并列展示。

    每条包含：
    - ``raw``      原始写法（回显用）
    - ``ids``      该条目解析出的 ID（可能为空 = 格式无效）
    - ``active``   是否真的在生效集合里
    - ``status``   ok / invalid / not_active
    - ``source``   own / external
    """
    rows: list[dict[str, Any]] = []
    covered: set[str] = set()

    for index, entry in enumerate(_split_entries(own_raw)):
        ids = sorted(extract_ids([entry]))
        if not ids:
            status = "invalid"
        elif all(i in effective for i in ids):
            status = "ok"
        else:
            status = "not_active"
        covered.update(ids)
        rows.append(
            {
                "index": index,
                "raw": entry,
                "ids": ids,
                "active": status == "ok",
                "status": status,
                "source": "own",
            }
        )

    # 外部同步来的条目：手写名单没覆盖到的才单列，避免同一人出现两行
    for ext_id in sorted((external or set()) - covered):
        rows.append(
            {
                "index": -1,
                "raw": ext_id,
                "ids": [ext_id],
                "active": ext_id in effective,
                "status": "ok" if ext_id in effective else "not_active",
                "source": "external",
            }
        )

    return rows


def _serialize(entries: list[str]) -> str:
    """把行列表写回隐藏配置。空条目一律丢弃。"""
    clean = [str(e).strip() for e in entries if str(e).strip()]
    return ",".join(clean)


async def get_ignore_list():
    try:
        cfg = get_config()
        own_raw = str(cfg.get(_MODULE_KEY) or "")
        effective = cfg.get_ignore_senders_snapshot()
        external = cfg.get_ignore_external_ids()
        return jsonify(
            {
                "success": True,
                "rows": _build_rows(own_raw, effective, external),
                "raw": own_raw,
                "effective": sorted(effective),
                "external": sorted(external),
                "sync_external": bool(cfg.get("reply_ignore_sync_external")),
                "sync_user_only": bool(cfg.get("reply_ignore_sync_user_only")),
            }
        )
    except Exception as e:
        logger.error(f"读取忽略名单失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def save_ignore_list():
    """整表保存（批量管理的主入口）。

    请求体：``{"entries": ["100000005", "@某人(111111)", ...]}``
    或       ``{"raw": "100000005,111111"}``（纯文本贴入）
    """
    try:
        data = await request.get_json(silent=True) or {}
        if "entries" in data and isinstance(data["entries"], list):
            entries = [str(x) for x in data["entries"]]
        elif "raw" in data:
            entries = _split_entries(str(data["raw"]))
        else:
            return jsonify({"success": False, "error": "缺少 entries 或 raw"}), 400

        # 逐条校验：无法解析的条目保留下来但在响应里标出，让用户自己决定
        invalid = [e for e in entries if e.strip() and not extract_ids([e])]
        payload = _serialize(entries)

        cfg = get_config()
        cfg.set_hidden(_MODULE_KEY, payload)

        effective = cfg.get_ignore_senders_snapshot()
        external = cfg.get_ignore_external_ids()
        logger.info(
            f"忽略名单已保存：{len(entries)} 条手写，"
            f"{len(effective)} 个生效（无效条目 {len(invalid)}）"
        )
        return jsonify(
            {
                "success": True,
                "raw": payload,
                "rows": _build_rows(payload, effective, external),
                "effective": sorted(effective),
                "external": sorted(external),
                "invalid": invalid,
            }
        )
    except Exception as e:
        logger.error(f"保存忽略名单失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def check_ids():
    """批量校验用户粘贴的一串 ID，返回每行解析结果（导入前预检）。"""
    try:
        data = await request.get_json(silent=True) or {}
        text = str(data.get("text") or "")
        entries = _split_entries(text)
        rows = []
        for entry in entries:
            ids = sorted(extract_ids([entry]))
            rows.append(
                {
                    "raw": entry,
                    "ids": ids,
                    "valid": bool(ids),
                }
            )
        return jsonify({"success": True, "rows": rows})
    except Exception as e:
        logger.error(f"校验 ID 失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


def register_ignore_list_routes(context) -> None:
    prefix = f"/{PLUGIN_NAME}/ignore-list"

    routes = [
        (f"{prefix}", get_ignore_list, ["GET"], "读取主动回复忽略名单"),
        (f"{prefix}/save", save_ignore_list, ["POST"], "保存主动回复忽略名单"),
        (f"{prefix}/check", check_ids, ["POST"], "批量校验用户 ID"),
    ]

    for route, handler, methods, desc in routes:
        context.register_web_api(route, handler, methods, desc)
