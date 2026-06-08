import asyncio
import json
import math
import random
import shutil
import shlex
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class Main(Star):
    """
    炉石传说国服排行榜查询插件。

    v1.7.3：
    - 战棋榜 最强/最菜 改为涨跌榜，不再是当前榜单前后名。
    - 战棋榜 双打排行 / 战棋榜 单打排行：当前排行榜，默认 20 行，可加数字。
    - 查人精确命中时显示排名上下文；例如第 500 名显示 450-500 区间。
    - 仍支持无斜杠中文入口：战棋榜 XX。
    - 新增后台自动记录：启动后每天固定时间自动刷新并记录排行榜。
    - 修复 /hsrank 等命令同时被 command 和 regex 命中导致重复回复的问题。
    - 默认查人时只显示命中的模式：只有双打显示双打，只有单打显示单打，双打单打都有才显示两个。
    - 数据文件迁移到 AstrBot/data/plugin_data/astrbot_plugin_hs_rank/，并自动兼容旧路径。
    """

    CN_API = "https://webapi.blizzard.cn/hs-rank-api-server/api/game/ranks"
    INTL_API = "https://hearthstone.blizzard.com/en-us/api/community/leaderboardsData"

    CACHE_TTL_SECONDS = 300
    FALLBACK_SEASON_ID = 17

    MODE_ALIASES = {
        "duo": "battlegroundsduo",
        "双打": "battlegroundsduo",
        "战棋双打": "battlegroundsduo",
        "battlegroundsduo": "battlegroundsduo",
        "bg": "battlegrounds",
        "单打": "battlegrounds",
        "战棋": "battlegrounds",
        "酒馆战棋": "battlegrounds",
        "battlegrounds": "battlegrounds",
    }

    MODES = ["battlegroundsduo", "battlegrounds"]

    MODE_NAMES = {
        "battlegroundsduo": "战棋双打",
        "battlegrounds": "酒馆战棋单打",
    }

    DEFAULT_FAKE_NOTE = "手动添加，群友自己装逼写上去的"

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.plugin_config = config or {}
        self.CACHE_TTL_SECONDS = self._config_int("cache_ttl_seconds", self.CACHE_TTL_SECONDS, 30, 3600)
        self.DEFAULT_FAKE_NOTE = self._config_str("fake_note", self.DEFAULT_FAKE_NOTE)
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[str, dict] = {}
        self.locks = {
            "battlegroundsduo": asyncio.Lock(),
            "battlegrounds": asyncio.Lock(),
        }
        self.state_lock = asyncio.Lock()
        self.state_file = self._find_state_file_path()
        self.state = self._load_state()
        self.auto_snapshot_task: asyncio.Task | None = None
        self._start_auto_snapshot_task()

    @filter.command("hsrank")
    async def hsrank(self, event: AstrMessageEvent):
        async for result in self._handle_rank_command(event):
            yield result

    @filter.regex(r"^\s*战棋榜(?:\s+.*)?\s*$")
    async def battle_rank_text_command(self, event: AstrMessageEvent):
        # 支持群里直接发送：战棋榜 某人、战棋榜 涨跌 某人。不要在这里匹配 hsrank，避免和 @filter.command 重复触发。
        async for result in self._handle_rank_command(event):
            yield result

    @filter.command("炉石榜")
    async def hsrank_cn_1(self, event: AstrMessageEvent):
        async for result in self._handle_rank_command(event):
            yield result

    @filter.command("炉石排行")
    async def hsrank_cn_2(self, event: AstrMessageEvent):
        async for result in self._handle_rank_command(event):
            yield result

    @filter.command("今日最强")
    async def hsrank_best_alias(self, event: AstrMessageEvent):
        tokens = self._parse_alias_tokens(event.message_str, "今日最强")
        async for result in self._handle_daily_official(event, want_best=True, args=tokens):
            yield result

    @filter.command("今日最菜")
    async def hsrank_worst_alias(self, event: AstrMessageEvent):
        tokens = self._parse_alias_tokens(event.message_str, "今日最菜")
        async for result in self._handle_daily_official(event, want_best=False, args=tokens):
            yield result

    def _start_auto_snapshot_task(self):
        if not self._config_bool("enable_auto_snapshot", False):
            return

        try:
            self.auto_snapshot_task = asyncio.create_task(self._auto_snapshot_loop())
            logger.info("炉石战棋榜自动记录任务已启动。")
        except RuntimeError as e:
            logger.warning(f"自动记录任务启动失败，可能事件循环尚未就绪：{e}")

    async def _auto_snapshot_loop(self):
        # 启动后先稍等，避免 AstrBot 刚启动时网络/插件还没完全就绪。
        await asyncio.sleep(10)

        while self._config_bool("enable_auto_snapshot", False):
            try:
                delay = self._seconds_until_next_snapshot()
                logger.info(f"炉石战棋榜下次自动记录将在 {int(delay)} 秒后执行。")
                await asyncio.sleep(delay)

                jitter_minutes = self._config_int("auto_snapshot_jitter_minutes", 0, 0, 60)
                if jitter_minutes > 0:
                    await asyncio.sleep(random.randint(0, jitter_minutes * 60))

                await self._auto_snapshot_once()

                # 避免时间计算边界导致同一分钟重复触发。
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"炉石战棋榜自动记录失败：{e}")
                # 失败后不要高频重试，等 10 分钟后重新进入下一轮计算。
                await asyncio.sleep(600)

    def _seconds_until_next_snapshot(self) -> float:
        time_text = self._config_str("auto_snapshot_time", "09:10")
        hour, minute = 9, 10

        try:
            parts = time_text.strip().split(":")
            hour = max(0, min(23, int(parts[0])))
            minute = max(0, min(59, int(parts[1])))
        except Exception:
            logger.warning(f"auto_snapshot_time 配置无效：{time_text}，使用默认 09:10。")

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        return max(1.0, (target - now).total_seconds())

    def _auto_snapshot_modes(self) -> list[str]:
        raw = self._config_get("auto_snapshot_modes", ["duo", "bg"])
        if isinstance(raw, str):
            parts = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        elif isinstance(raw, list):
            parts = [str(x).strip() for x in raw if str(x).strip()]
        else:
            parts = ["duo", "bg"]

        modes = []
        for part in parts:
            mode = self._mode_from_token(part)
            if mode and mode not in modes:
                modes.append(mode)

        return modes or ["battlegroundsduo", "battlegrounds"]

    async def _auto_snapshot_once(self):
        modes = self._auto_snapshot_modes()
        logger.info(f"炉石战棋榜开始自动记录：{','.join(self._mode_short(m) for m in modes)}")

        for mode in modes:
            try:
                self.cache.pop(mode, None)
                data = await self._get_leaderboard(mode, force=True)
                logger.info(
                    f"炉石战棋榜自动记录完成：{self.MODE_NAMES[mode]}，官方 {len(data.get('official_rows', []))} 条。"
                )
            except Exception as e:
                logger.exception(f"炉石战棋榜自动记录 {self.MODE_NAMES.get(mode, mode)} 失败：{e}")

    async def _handle_rank_command(self, event: AstrMessageEvent):
        try:
            tokens = self._parse_tokens(event.message_str)

            if not tokens or tokens[0].lower() in {"help", "帮助", "?"}:
                yield event.plain_result(self._help_text())
                return

            cmd = tokens[0].lower()

            if cmd in {"settings", "setting", "配置", "设置", "后台"}:
                yield event.plain_result(self._config_help_text())
                return

            if cmd in {"admin", "管理员", "我是管理员", "initadmin", "admininit"}:
                yield event.plain_result(
                    "管理员现在在 AstrBot 后台插件配置里设置，不再用群聊命令设置。\n"
                    "普通查询不需要管理员：战棋榜 涨跌 昵称"
                )
                return

            if cmd in {"bind", "绑定", "绑定昵称", "unbind", "解绑", "my", "我的", "bindlist", "绑定列表"}:
                yield event.plain_result(
                    "新版不需要绑定昵称。\n"
                    "直接查询：战棋榜 昵称 或 战棋榜 涨跌 昵称"
                )
                return

            if cmd in {"trend", "涨跌", "涨跌记录", "track"}:
                async for result in self._handle_trend(event, tokens[1:]):
                    yield result
                return

            if cmd in {"best", "strong", "最强", "今日最强"}:
                async for result in self._handle_trend_board(event, want_best=True, args=tokens[1:]):
                    yield result
                return

            if cmd in {"worst", "weak", "菜", "最菜", "今日最菜"}:
                async for result in self._handle_trend_board(event, want_best=False, args=tokens[1:]):
                    yield result
                return

            if cmd in {"add", "addfake", "添加", "手动添加", "装逼", "zb"}:
                async for result in self._handle_add_fake(event, tokens[1:]):
                    yield result
                return

            if cmd in {"fake", "fakes", "装逼榜", "手动榜", "listfake"}:
                if not self._config_bool("enable_fake_rank", True):
                    yield event.plain_result("后台已关闭手动装逼榜功能。")
                    return
                mode, rest, has_mode = self._parse_optional_mode(tokens[1:])
                limit = self._parse_int(rest[0], 50) if rest else 50
                if has_mode:
                    rows = self._get_fake_rows(mode)
                    data = self._fake_data(mode)
                    yield event.plain_result(
                        self._format_rows(
                            mode=mode,
                            rows=rows[:limit],
                            data=data,
                            title_suffix="手动添加名单",
                            total_count=len(rows),
                            limit=limit,
                        )
                    )
                else:
                    yield event.plain_result(self._format_fake_both(limit))
                return

            if cmd in {"delfake", "del", "deletefake", "删除装逼", "删装逼", "删除手动"}:
                if not self._config_bool("enable_fake_rank", True):
                    yield event.plain_result("后台已关闭手动装逼榜功能。")
                    return
                if self._config_bool("require_admin_for_fake_ops", False) and not self._is_config_admin(event):
                    yield event.plain_result("后台已设置：只有管理员可以删除手动装逼榜。")
                    return
                mode, rest, has_mode = self._parse_optional_mode(tokens[1:])
                if not has_mode:
                    yield event.plain_result("删除手动榜请指定模式：战棋榜 delfake duo 昵称 或 战棋榜 delfake bg 昵称")
                    return
                if not rest:
                    yield event.plain_result("格式：战棋榜 delfake [duo|bg] 昵称\n例：战棋榜 delfake duo 群友")
                    return

                nickname = " ".join(rest).strip()
                async with self.state_lock:
                    fake_entries = self.state.setdefault("fake_entries", self._default_fake_entries())
                    before = len(fake_entries.get(mode, []))
                    fake_entries[mode] = [
                        item for item in fake_entries.get(mode, [])
                        if str(item.get("name", "")).lower() != nickname.lower()
                    ]
                    after = len(fake_entries.get(mode, []))
                    self._save_state()

                if before == after:
                    yield event.plain_result(f"{self.MODE_NAMES[mode]}没有找到手动添加的昵称：{nickname}")
                else:
                    yield event.plain_result(f"已删除{self.MODE_NAMES[mode]}手动添加昵称：{nickname}")
                return

            if cmd in {"clearfake", "清空装逼", "清空手动"}:
                if not self._config_bool("enable_fake_rank", True):
                    yield event.plain_result("后台已关闭手动装逼榜功能。")
                    return
                if self._config_bool("require_admin_for_fake_ops", False) and not self._is_config_admin(event):
                    yield event.plain_result("后台已设置：只有管理员可以清空手动装逼榜。")
                    return
                mode, rest, has_mode = self._parse_optional_mode(tokens[1:])
                if not has_mode:
                    yield event.plain_result("清空手动榜请指定模式：战棋榜 clearfake duo 或 战棋榜 clearfake bg")
                    return
                async with self.state_lock:
                    fake_entries = self.state.setdefault("fake_entries", self._default_fake_entries())
                    count = len(fake_entries.get(mode, []))
                    fake_entries[mode] = []
                    self._save_state()
                yield event.plain_result(f"已清空{self.MODE_NAMES[mode]}手动添加名单，共删除 {count} 条。")
                return

            if cmd in {"snapshot", "记录", "记录榜单"}:
                mode, rest, has_mode = self._parse_optional_mode(tokens[1:])
                if has_mode:
                    self.cache.pop(mode, None)
                    data = await self._get_leaderboard(mode, force=True)
                    yield event.plain_result(
                        f"已记录今日{self.MODE_NAMES[mode]}榜单：官方 {len(data['official_rows'])} 条。"
                    )
                else:
                    msg = await self._refresh_modes(self.MODES)
                    yield event.plain_result(msg.replace("刷新", "记录"))
                return

            if cmd in {"refresh", "刷新", "更新", "update"}:
                mode, rest, has_mode = self._parse_optional_mode(tokens[1:])
                if has_mode:
                    self.cache.pop(mode, None)
                    data = await self._get_leaderboard(mode, force=True)
                    yield event.plain_result(
                        f"已刷新{self.MODE_NAMES[mode]}排行榜，官方 {len(data['official_rows'])} 条，"
                        f"手动添加 {len(self._get_fake_rows(mode))} 条。"
                    )
                else:
                    yield event.plain_result(await self._refresh_modes(self.MODES))
                return

            if cmd in {"双打排行", "双打排名", "duo排行", "duo排名"}:
                limit = self._parse_int(tokens[1], self._default_top_limit()) if len(tokens) >= 2 else self._default_top_limit()
                mode = "battlegroundsduo"
                data = await self._get_leaderboard(mode)
                rows = data["rows"][:limit]
                yield event.plain_result(
                    self._format_rows(
                        mode=mode,
                        rows=rows,
                        data=data,
                        title_suffix=f"前{limit}名",
                        total_count=len(rows),
                        limit=limit,
                    )
                )
                return

            if cmd in {"单打排行", "单打排名", "bg排行", "bg排名"}:
                limit = self._parse_int(tokens[1], self._default_top_limit()) if len(tokens) >= 2 else self._default_top_limit()
                mode = "battlegrounds"
                data = await self._get_leaderboard(mode)
                rows = data["rows"][:limit]
                yield event.plain_result(
                    self._format_rows(
                        mode=mode,
                        rows=rows,
                        data=data,
                        title_suffix=f"前{limit}名",
                        total_count=len(rows),
                        limit=limit,
                    )
                )
                return

            if cmd in {"top", "前", "排行榜", "rank"}:
                mode, rest, has_mode = self._parse_optional_mode(tokens[1:])
                limit = self._parse_int(rest[0], self._default_top_limit()) if rest else self._default_top_limit()

                if has_mode:
                    data = await self._get_leaderboard(mode)
                    rows = data["rows"][:limit]
                    yield event.plain_result(
                        self._format_rows(
                            mode=mode,
                            rows=rows,
                            data=data,
                            title_suffix=f"前{limit}名",
                            total_count=len(rows),
                            limit=limit,
                        )
                    )
                else:
                    yield event.plain_result(await self._format_top_both(limit))
                return

            if cmd in {"id", "查id", "查询id", "昵称"}:
                mode, rest, has_mode = self._parse_optional_mode(tokens[1:])
                if not rest:
                    yield event.plain_result("请提供要查询的昵称，例如：战棋榜 id 某人 或 战棋榜 某人")
                    return

                nickname = " ".join(rest).strip()
                if has_mode:
                    data = await self._get_leaderboard(mode)
                    yield event.plain_result(
                        self._format_exact_context(
                            mode=mode,
                            nickname=nickname,
                            data=data,
                            context_before=self._context_before(),
                        )
                    )
                else:
                    yield event.plain_result(await self._format_search_both(nickname, exact=True, limit=self._context_before() + 1))
                return

            if cmd in {"find", "search", "查", "搜索"}:
                mode, rest, has_mode = self._parse_optional_mode(tokens[1:])
                if not rest:
                    yield event.plain_result("请提供关键词，例如：战棋榜 查 关键词 或 战棋榜 某人")
                    return

                keyword = rest[0]
                limit = self._parse_int(rest[1], self._default_query_limit()) if len(rest) >= 2 else self._default_query_limit()

                if has_mode:
                    data = await self._get_leaderboard(mode)
                    rows = [
                        row for row in data["rows"]
                        if keyword.lower() in row["name"].lower()
                    ]
                    yield event.plain_result(
                        self._format_rows(
                            mode=mode,
                            rows=rows[:limit],
                            data=data,
                            title_suffix=f"昵称含有'{keyword}'的名单",
                            total_count=len(rows),
                            limit=limit,
                        )
                    )
                else:
                    yield event.plain_result(await self._format_search_both(keyword, exact=False, limit=limit))
                return

            mode = self._mode_from_token(tokens[0])

            if mode:
                if len(tokens) == 1:
                    limit = self._default_top_limit()
                    data = await self._get_leaderboard(mode)
                    rows = data["rows"][:limit]
                    yield event.plain_result(
                        self._format_rows(
                            mode=mode,
                            rows=rows,
                            data=data,
                            title_suffix=f"前{limit}名",
                            total_count=len(rows),
                            limit=limit,
                        )
                    )
                    return

                keyword = tokens[1]
                limit = self._parse_int(tokens[2], self._default_query_limit()) if len(tokens) >= 3 else self._default_query_limit()

                data = await self._get_leaderboard(mode)
                rows = [
                    row for row in data["rows"]
                    if keyword.lower() in row["name"].lower()
                ]

                yield event.plain_result(
                    self._format_rows(
                        mode=mode,
                        rows=rows[:limit],
                        data=data,
                        title_suffix=f"昵称含有'{keyword}'的名单",
                        total_count=len(rows),
                        limit=limit,
                    )
                )
                return

            # 简化：/hsrank 某人 直接当作双打+单打模糊查人。
            keyword = " ".join(tokens).strip()
            if keyword:
                yield event.plain_result(await self._format_search_both(keyword, exact=False, limit=self._default_query_limit()))
                return

            yield event.plain_result(self._help_text())

        except Exception as e:
            logger.exception("获取炉石排行榜失败")
            yield event.plain_result(
                f"获取炉石排行榜失败：{e}\n"
                f"可能原因：接口暂时不可用、赛季 ID 变化、网络无法访问国服接口。"
            )

    def _config_get(self, key: str, default=None):
        try:
            if hasattr(self.plugin_config, "get"):
                return self.plugin_config.get(key, default)
        except Exception:
            pass
        return default

    def _config_str(self, key: str, default: str = "") -> str:
        value = self._config_get(key, default)
        if value is None:
            return default
        value = str(value).strip()
        return value if value else default

    def _config_bool(self, key: str, default: bool = False) -> bool:
        value = self._config_get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"1", "true", "yes", "y", "on", "是", "开", "开启"}
        return bool(value)

    def _config_int(self, key: str, default: int, min_val: int | None = None, max_val: int | None = None) -> int:
        try:
            value = int(self._config_get(key, default))
        except Exception:
            value = default
        if min_val is not None:
            value = max(min_val, value)
        if max_val is not None:
            value = min(max_val, value)
        return value

    def _default_query_limit(self) -> int:
        return self._config_int("default_query_limit", 30, 1, self._max_output_limit())

    def _default_top_limit(self) -> int:
        return self._config_int("default_top_limit", 20, 1, self._max_output_limit())

    def _default_daily_limit(self) -> int:
        return self._config_int("default_daily_limit", 10, 1, self._max_output_limit())

    def _max_output_limit(self) -> int:
        return self._config_int("max_output_limit", 100, 1, 300)

    def _default_daily_mode(self) -> str:
        mode = self._config_str("default_daily_mode", "duo")
        return self.MODE_ALIASES.get(mode.lower(), "battlegroundsduo")

    def _sender_id(self, event: AstrMessageEvent) -> str:
        try:
            sender = event.get_sender_id()
            if sender is not None:
                return str(sender)
        except Exception:
            pass
        return "unknown"

    def _is_config_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = self._config_get("admin_ids", [])
        if isinstance(admin_ids, str):
            admin_ids = [x.strip() for x in admin_ids.replace("，", ",").split(",") if x.strip()]
        if not isinstance(admin_ids, list):
            admin_ids = []
        sender_id = self._sender_id(event)
        return str(sender_id) in {str(x).strip() for x in admin_ids}

    def _config_help_text(self) -> str:
        return (
            "本插件支持 AstrBot 后台配置按钮。可在插件配置里修改：\n"
            "1. 默认查询数量 / 今日最强最菜数量 / 最大输出数量\n"
            "2. 默认今日榜模式：duo 或 bg\n"
            "3. 缓存时间 cache_ttl_seconds\n"
            "4. 是否启用手动装逼榜 enable_fake_rank\n"
            "5. 是否只有管理员能改手动榜 require_admin_for_fake_ops\n"
            "6. 管理员 QQ/用户ID 列表 admin_ids\n"
            "7. 历史记录文件路径 state_file_path\n"
            "普通查询不需要管理员：战棋榜 某人 / 战棋榜 涨跌 某人"
        )

    async def _refresh_modes(self, modes: list[str]) -> str:
        lines = []
        for mode in modes:
            self.cache.pop(mode, None)
            data = await self._get_leaderboard(mode, force=True)
            lines.append(
                f"{self.MODE_NAMES[mode]}：官方 {len(data['official_rows'])} 条，"
                f"手动添加 {len(self._get_fake_rows(mode))} 条"
            )
        return "已刷新并记录今日榜单：\n" + "\n".join(lines)

    async def _handle_trend(self, event: AstrMessageEvent, args: list[str]):
        mode, rest, has_mode = self._parse_optional_mode(args)

        if not rest:
            yield event.plain_result(
                "格式：战棋榜 涨跌 昵称\n"
                "例：战棋榜 涨跌 某人\n"
                "也可以指定单模式：战棋榜 涨跌 duo 某人 或 战棋榜 涨跌 bg 某人"
            )
            return

        nickname = " ".join(rest).strip()

        if has_mode:
            await self._get_leaderboard(mode)
            yield event.plain_result(self._format_trend(mode, nickname))
            return

        sections = []
        for one_mode in self.MODES:
            await self._get_leaderboard(one_mode)
            sections.append(self._format_trend(one_mode, nickname))
        yield event.plain_result("\n\n".join(sections))


    async def _handle_trend_board(self, event: AstrMessageEvent, want_best: bool, args: list[str] | None = None):
        """
        最强/最菜 = 涨跌榜：
        最强：相比上一条历史记录，排名上升最多。
        最菜：相比上一条历史记录，排名下降最多。
        不指定模式时同时显示双打+单打。
        """
        args = args or []
        mode, rest, has_mode = self._parse_optional_mode(args)
        limit = self._parse_int(rest[0], self._default_daily_limit()) if rest else self._default_daily_limit()

        if has_mode:
            await self._get_leaderboard(mode)
            yield event.plain_result(self._format_trend_board(mode, want_best, limit))
            return

        sections = []
        for one_mode in self.MODES:
            await self._get_leaderboard(one_mode)
            sections.append(self._format_trend_board(one_mode, want_best, limit))
        yield event.plain_result("\n\n".join(sections))

    async def _handle_daily_official(self, event: AstrMessageEvent, want_best: bool, args: list[str] | None = None):
        args = args or []
        mode, rest, has_mode = self._parse_optional_mode(args)
        limit = self._parse_int(rest[0], self._default_daily_limit()) if rest else self._default_daily_limit()

        if not has_mode:
            mode = self._default_daily_mode()

        data = await self._get_leaderboard(mode)
        official_rows = data.get("official_rows", [])

        yield event.plain_result(
            self._format_daily_official(
                mode=mode,
                rows=official_rows,
                data=data,
                want_best=want_best,
                limit=limit,
            )
        )

    async def _handle_add_fake(self, event: AstrMessageEvent, args: list[str]):
        if not self._config_bool("enable_fake_rank", True):
            yield event.plain_result("后台已关闭手动装逼榜功能。")
            return
        if self._config_bool("require_admin_for_fake_ops", False) and not self._is_config_admin(event):
            yield event.plain_result("后台已设置：只有管理员可以添加/修改手动装逼榜。")
            return
        mode, rest, has_mode = self._parse_optional_mode(args)
        if not has_mode:
            yield event.plain_result(
                "手动添加需要指定模式，避免不知道加到双打还是单打：\n"
                "战棋榜 add duo 昵称 排名 积分 [备注]\n"
                "战棋榜 add bg 昵称 排名 积分 [备注]"
            )
            return

        if len(rest) < 3:
            yield event.plain_result(
                "格式：/hsrank add [duo|bg] 昵称 排名 积分 [备注]\n"
                "例：战棋榜 add duo 群友 1 99999\n"
                "例：战棋榜 add duo 群友 1 99999 宇宙第一战棋王"
            )
            return

        nickname = rest[0]
        rank = self._parse_int_unlimited(rest[1])
        score = self._parse_int_unlimited(rest[2])
        note = " ".join(rest[3:]).strip() if len(rest) >= 4 else self.DEFAULT_FAKE_NOTE

        if rank is None or score is None:
            yield event.plain_result("排名和积分必须是数字，例如：战棋榜 add duo 群友 1 99999")
            return

        entry = {
            "rank": rank,
            "name": nickname,
            "score": score,
            "note": note,
            "fake": True,
            "created_at": int(time.time()),
        }

        async with self.state_lock:
            fake_entries = self.state.setdefault("fake_entries", self._default_fake_entries())
            entries = fake_entries.setdefault(mode, [])
            entries[:] = [
                item for item in entries
                if str(item.get("name", "")).lower() != nickname.lower()
            ]
            entries.append(entry)
            entries.sort(key=lambda row: int(row.get("rank", 999999)))
            self._save_state()

        yield event.plain_result(
            f"已手动添加到{self.MODE_NAMES[mode]}：\n"
            f"排名:{rank}（手动添加） 昵称:{nickname}（{note}）\n"
            f"积分:{score}（手动添加）"
        )

    async def _format_search_both(self, keyword: str, exact: bool, limit: int) -> str:
        """
        默认查人逻辑：
        - 后台同时查双打和单打。
        - 哪个模式有结果就显示哪个模式。
        - 只有双打命中：只显示双打。
        - 只有单打命中：只显示单打。
        - 双打和单打都命中：两个都显示。
        - 两边都没命中：合并提示未找到。
        """
        sections = []
        total = 0
        hit_modes = []

        for mode in self.MODES:
            data = await self._get_leaderboard(mode)

            if exact:
                rows = [
                    row for row in data.get("official_rows", [])
                    if row["name"].lower() == keyword.lower()
                ]

                fake = self._find_fake_by_name(mode, keyword)

                if rows or fake:
                    total += len(rows) + (1 if fake and not rows else 0)
                    hit_modes.append(self.MODE_NAMES[mode])
                    sections.append(
                        self._format_exact_context(
                            mode=mode,
                            nickname=keyword,
                            data=data,
                            context_before=self._context_before(),
                        )
                    )
            else:
                rows = [
                    row for row in data["rows"]
                    if keyword.lower() in row["name"].lower()
                ]

                if rows:
                    total += len(rows)
                    hit_modes.append(self.MODE_NAMES[mode])
                    suffix = f"昵称含有'{keyword}'的名单"
                    sections.append(
                        self._format_rows(
                            mode=mode,
                            rows=rows[:limit],
                            data=data,
                            title_suffix=suffix,
                            total_count=len(rows),
                            limit=limit,
                        )
                    )

        if not sections:
            mode_names = "、".join(self.MODE_NAMES[m] for m in self.MODES)
            search_type = "精确昵称" if exact else "昵称关键词"
            return (
                f"已同时查询【{mode_names}】，没有找到{search_type}“{keyword}”。\n"
                f"可以试试：战棋榜 查 {keyword} 或确认大小写/昵称是否一致。"
            )

        if len(hit_modes) == 1:
            header = f"已查询双打和单打，仅【{hit_modes[0]}】命中，共 {total} 条。"
        else:
            header = f"已查询双打和单打，【{'、'.join(hit_modes)}】均命中，共 {total} 条。"

        return header + "\n\n" + "\n\n".join(sections)

    async def _format_top_both(self, limit: int) -> str:
        sections = []
        for mode in self.MODES:
            data = await self._get_leaderboard(mode)
            rows = data["rows"][:limit]
            sections.append(
                self._format_rows(
                    mode=mode,
                    rows=rows,
                    data=data,
                    title_suffix=f"前{limit}名",
                    total_count=len(rows),
                    limit=limit,
                )
            )
        return "\n\n".join(sections)

    def _format_fake_both(self, limit: int) -> str:
        sections = []
        for mode in self.MODES:
            rows = self._get_fake_rows(mode)
            data = self._fake_data(mode)
            sections.append(
                self._format_rows(
                    mode=mode,
                    rows=rows[:limit],
                    data=data,
                    title_suffix="手动添加名单",
                    total_count=len(rows),
                    limit=limit,
                )
            )
        return "\n\n".join(sections)

    def _plugin_data_dir(self) -> Path:
        """
        插件数据目录。
        按 AstrBot 建议，状态文件、历史记录、后续卡牌缓存等都放在：
        /AstrBot/data/plugin_data/astrbot_plugin_hs_rank/
        避免污染 /AstrBot/data 根目录。
        """
        candidates = [
            Path("/AstrBot/data/plugin_data/astrbot_plugin_hs_rank"),
            Path.cwd() / "data" / "plugin_data" / "astrbot_plugin_hs_rank",
            Path("data") / "plugin_data" / "astrbot_plugin_hs_rank",
        ]

        for path in candidates:
            try:
                path.mkdir(parents=True, exist_ok=True)
                return path
            except Exception:
                continue

        fallback = Path("astrbot_plugin_hs_rank_data")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    def _find_state_file_path(self) -> Path:
        configured = self._config_str("state_file_path", "").strip()

        # 如果用户后台显式配置了路径，尊重用户配置。
        if configured:
            try:
                path = Path(configured)
                path.parent.mkdir(parents=True, exist_ok=True)
                return path
            except Exception as e:
                logger.warning(f"state_file_path 配置无效，将使用默认插件数据目录：{configured}，原因：{e}")

        new_path = self._plugin_data_dir() / "hsrank_state.json"

        # 兼容旧版本：自动迁移旧状态文件。
        old_paths = [
            Path("/AstrBot/data/plugin_data/astrbot_plugin_hs_rank/hsrank_state.json"),
            Path.cwd() / "data" / "hsrank_state.json",
            Path("hsrank_state.json"),
        ]

        if not new_path.exists():
            for old_path in old_paths:
                try:
                    if old_path.exists() and old_path.resolve() != new_path.resolve():
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(old_path, new_path)
                        logger.info(f"已迁移炉石战棋榜状态文件：{old_path} -> {new_path}")
                        break
                except Exception as e:
                    logger.warning(f"迁移旧状态文件失败：{old_path} -> {new_path}，原因：{e}")

        return new_path

    def _default_fake_entries(self) -> dict:
        return {
            "battlegroundsduo": [],
            "battlegrounds": [],
        }

    def _default_state(self) -> dict:
        return {
            "fake_entries": self._default_fake_entries(),
            "history": {
                "battlegroundsduo": {},
                "battlegrounds": {},
            },
        }

    def _load_state(self) -> dict:
        default = self._default_state()

        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return default

                data.setdefault("fake_entries", self._default_fake_entries())
                data.setdefault("history", {})
                for mode in self.MODES:
                    data["fake_entries"].setdefault(mode, [])
                    data["history"].setdefault(mode, {})

                # 兼容旧版：旧字段 admins / bindings 即使存在，新版也不再使用。
                return data
            except Exception as e:
                logger.warning(f"读取状态文件失败，将使用空状态：{e}")

        old_fake_file = Path("/AstrBot/data/hsrank_fake_entries.json")
        if old_fake_file.exists():
            try:
                fake = json.loads(old_fake_file.read_text(encoding="utf-8"))
                if isinstance(fake, dict):
                    default["fake_entries"].update(fake)
            except Exception:
                pass

        return default

    def _save_state(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存状态文件失败：{e}")

    def _today_str(self) -> str:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    def _record_history(self, mode: str, official_rows: list[dict]):
        today = self._today_str()
        history = self.state.setdefault("history", {}).setdefault(mode, {})

        snapshot: dict[str, dict] = {}
        for row in official_rows:
            name = str(row.get("name", "")).strip()
            if not name:
                continue

            key = name.lower()
            try:
                rank = int(row.get("rank"))
            except Exception:
                continue

            try:
                score = int(row.get("score"))
            except Exception:
                score = 0

            old = snapshot.get(key)
            if old is None or rank < int(old.get("rank", 999999)):
                snapshot[key] = {
                    "name": name,
                    "rank": rank,
                    "score": score,
                    "recorded_at": int(time.time()),
                }

        history[today] = snapshot

        dates = sorted(history.keys())
        for old_date in dates[:-60]:
            history.pop(old_date, None)

        self._save_state()

    def _find_history_records(self, mode: str, nickname: str) -> list[tuple[str, dict]]:
        key = nickname.lower()
        history = self.state.setdefault("history", {}).setdefault(mode, {})
        records = []
        for date in sorted(history.keys()):
            item = history.get(date, {}).get(key)
            if item:
                records.append((date, item))
        return records

    def _format_trend(self, mode: str, nickname: str) -> str:
        records = self._find_history_records(mode, nickname)
        fake = self._find_fake_by_name(mode, nickname)

        if not records:
            lines = [
                f"【{self.MODE_NAMES[mode]}】涨跌记录：{nickname}",
                "官方历史记录里还没有这个昵称。",
                f"可以先执行 战棋榜 更新 或 战棋榜 记录 记录今日双模式榜单。",
            ]
            if fake:
                lines.append(
                    f"但手动榜里有：排名 {fake['rank']}，积分 {fake['score']}（手动添加：{fake.get('note') or self.DEFAULT_FAKE_NOTE}）"
                )
            return "\n".join(lines)

        lines = [f"【{self.MODE_NAMES[mode]}】涨跌记录：{records[-1][1].get('name', nickname)}"]
        for date, item in records[-7:]:
            lines.append(f"{date}：排名 {item['rank']}，积分 {item['score']}")

        if len(records) >= 2:
            prev_date, prev = records[-2]
            latest_date, latest = records[-1]
            rank_delta = int(prev["rank"]) - int(latest["rank"])
            score_delta = int(latest["score"]) - int(prev["score"])

            if rank_delta > 0:
                rank_text = f"上升 {rank_delta} 名"
            elif rank_delta < 0:
                rank_text = f"下降 {abs(rank_delta)} 名"
            else:
                rank_text = "排名不变"

            score_text = f"+{score_delta}" if score_delta >= 0 else str(score_delta)
            lines.append(f"最近变化（{prev_date} -> {latest_date}）：{rank_text}，积分 {score_text}")
        else:
            lines.append("目前只记录到 1 次，从现在开始每天查询/刷新会继续累计。")

        if fake:
            lines.append("注：手动榜里也有同名记录，但涨跌只按官方榜单计算。")

        return "\n".join(lines)


    def _context_before(self) -> int:
        return self._config_int("context_before", 50, 0, self._max_output_limit())

    def _format_exact_context(self, mode: str, nickname: str, data: dict, context_before: int = 50) -> str:
        """
        精确查人时显示排名上下文。
        例如命中第 500 名，默认显示 450-500。
        只用官方榜单计算区间，手动榜同名会额外提示。
        """
        official_rows = data.get("official_rows", [])
        matches = [
            row for row in official_rows
            if str(row.get("name", "")).lower() == nickname.lower()
        ]

        if not matches:
            # 官方没命中时，再看手动榜。
            fake = self._find_fake_by_name(mode, nickname)
            if fake:
                fake_data = self._fake_data(mode)
                return self._format_rows(
                    mode=mode,
                    rows=[fake],
                    data=fake_data,
                    title_suffix=f"昵称等于'{nickname}'的手动名单",
                    total_count=1,
                    limit=1,
                )

            return self._format_rows(
                mode=mode,
                rows=[],
                data=data,
                title_suffix=f"昵称等于'{nickname}'的名单",
                total_count=0,
                limit=1,
            )

        sections = []
        sorted_rows = sorted(
            official_rows,
            key=lambda row: int(row["rank"]) if str(row["rank"]).isdigit() else 999999,
        )

        for match in matches:
            try:
                rank = int(match.get("rank"))
            except Exception:
                rank = 0

            start_rank = max(1, rank - context_before)
            end_rank = rank

            window_rows = []
            for row in sorted_rows:
                try:
                    row_rank = int(row.get("rank"))
                except Exception:
                    continue
                if start_rank <= row_rank <= end_rank:
                    window_rows.append(row)

            sections.append(
                self._format_rows(
                    mode=mode,
                    rows=window_rows,
                    data=data,
                    title_suffix=f"昵称等于'{nickname}'，排名区间{start_rank}-{end_rank}",
                    total_count=len(window_rows),
                    limit=len(window_rows),
                )
            )

        fake = self._find_fake_by_name(mode, nickname)
        if fake:
            sections.append(
                f"注：手动榜也有同名记录：排名 {fake['rank']}，积分 {fake['score']}（手动添加：{fake.get('note') or self.DEFAULT_FAKE_NOTE}）。"
            )

        return "\n\n".join(sections)

    def _format_trend_board(self, mode: str, want_best: bool, limit: int) -> str:
        """
        根据最近两次历史快照生成涨跌榜。
        """
        history = self.state.setdefault("history", {}).setdefault(mode, {})
        dates = sorted([d for d in history.keys() if isinstance(history.get(d), dict)])

        if len(dates) < 2:
            return (
                f"【{self.MODE_NAMES[mode]}】{'最强涨幅榜' if want_best else '最菜跌幅榜'}：\n"
                f"历史记录不足，至少需要两天/两次不同日期的榜单快照。\n"
                f"先执行“战棋榜 更新”，明天再执行一次后就能统计涨跌榜。"
            )

        prev_date, latest_date = dates[-2], dates[-1]
        prev_snapshot = history.get(prev_date, {})
        latest_snapshot = history.get(latest_date, {})

        changes = []
        for key, latest in latest_snapshot.items():
            prev = prev_snapshot.get(key)
            if not prev:
                continue
            try:
                prev_rank = int(prev.get("rank"))
                latest_rank = int(latest.get("rank"))
                prev_score = int(prev.get("score", 0))
                latest_score = int(latest.get("score", 0))
            except Exception:
                continue

            rank_delta = prev_rank - latest_rank  # 正数=上升，负数=下降
            score_delta = latest_score - prev_score

            changes.append({
                "name": latest.get("name") or prev.get("name") or key,
                "prev_rank": prev_rank,
                "latest_rank": latest_rank,
                "prev_score": prev_score,
                "latest_score": latest_score,
                "rank_delta": rank_delta,
                "score_delta": score_delta,
            })

        if not changes:
            return (
                f"【{self.MODE_NAMES[mode]}】{'最强涨幅榜' if want_best else '最菜跌幅榜'}：\n"
                f"{prev_date} -> {latest_date} 没有找到可对比的同名玩家。"
            )

        if want_best:
            # 上升最多优先，其次分数增加多。
            changes.sort(key=lambda x: (x["rank_delta"], x["score_delta"]), reverse=True)
            title = f"最强涨幅榜前{limit}名"
        else:
            # 下降最多优先：rank_delta 越小越菜。
            changes.sort(key=lambda x: (x["rank_delta"], x["score_delta"]))
            title = f"最菜跌幅榜前{limit}名"

        chosen = changes[:limit]
        lines = [f"【{self.MODE_NAMES[mode]}】{title}（{prev_date} -> {latest_date}）："]

        for item in chosen:
            if item["rank_delta"] > 0:
                rank_text = f"上升{item['rank_delta']}名"
            elif item["rank_delta"] < 0:
                rank_text = f"下降{abs(item['rank_delta'])}名"
            else:
                rank_text = "排名不变"

            score_text = f"+{item['score_delta']}" if item["score_delta"] >= 0 else str(item["score_delta"])
            lines.append(
                f"{item['name']}：{item['prev_rank']} -> {item['latest_rank']}，{rank_text}，积分{score_text}"
            )

        if want_best:
            lines.append("说明：最强=排名上升最多。")
        else:
            lines.append("说明：最菜=排名下降最多，仅群聊娱乐。")
        return "\n".join(lines)

    def _format_daily_official(
        self,
        mode: str,
        rows: list[dict],
        data: dict,
        want_best: bool,
        limit: int,
    ) -> str:
        if not rows:
            return f"{self.MODE_NAMES[mode]}官方榜单为空，暂时无法统计今日{'最强' if want_best else '最菜'}。"

        official_sorted = sorted(
            rows,
            key=lambda row: int(row["rank"]) if str(row["rank"]).isdigit() else 999999,
        )

        if want_best:
            chosen = official_sorted[:limit]
            title = f"今日最强前{limit}名"
            note = "按官方排行榜排名从高到低统计。"
        else:
            chosen = list(reversed(official_sorted[-limit:]))
            title = f"今日最菜后{limit}名"
            note = "按官方在榜玩家末尾统计；这里的“最菜”只是群聊娱乐。"

        update_time = data.get("official_update")
        if update_time:
            time_label = f"榜单更新时间{update_time}"
        else:
            time_label = f"数据获取时间{self._format_fetch_time(data['fetched_at'])}"

        lines = [
            f"{self.MODE_NAMES[mode]}{title}（{time_label}，赛季ID:{data['season_id']}）："
        ]

        for row in chosen:
            lines.append(f"排名:{str(row['rank']).ljust(8)} 昵称:{row['name']}")
            lines.append(f"积分:{row['score']}")

        lines.append(note)
        if not update_time:
            lines.append("注：接口未返回官方榜单更新时间，以上为本次数据获取时间。")
        return "\n".join(lines)

    def _get_fake_rows(self, mode: str) -> list[dict]:
        if not self._config_bool("enable_fake_rank", True):
            return []
        rows = []
        fake_entries = self.state.setdefault("fake_entries", self._default_fake_entries())

        for item in fake_entries.get(mode, []):
            try:
                rows.append(
                    {
                        "rank": int(item.get("rank", 999999)),
                        "name": str(item.get("name", "")),
                        "score": int(item.get("score", 0)),
                        "note": str(item.get("note", self.DEFAULT_FAKE_NOTE)) or self.DEFAULT_FAKE_NOTE,
                        "fake": True,
                    }
                )
            except Exception:
                continue

        rows = [row for row in rows if row["name"]]
        rows.sort(key=lambda row: int(row.get("rank", 999999)))
        return rows

    def _find_fake_by_name(self, mode: str, nickname: str) -> dict | None:
        for row in self._get_fake_rows(mode):
            if row["name"].lower() == nickname.lower():
                return row
        return None

    def _merge_fake_rows(self, mode: str, official_rows: list[dict]) -> list[dict]:
        merged = official_rows + self._get_fake_rows(mode)
        merged.sort(key=lambda row: int(row["rank"]) if str(row["rank"]).isdigit() else 999999)
        return merged

    def _fake_data(self, mode: str) -> dict:
        return {
            "mode": mode,
            "season_id": "手动",
            "rows": self._get_fake_rows(mode),
            "official_rows": [],
            "total": len(self._get_fake_rows(mode)),
            "official_update": "",
            "fetched_at": time.time(),
            "fake_only": True,
        }

    def _parse_tokens(self, text: str) -> list[str]:
        text = text.strip()
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()

        command_names = {
            "hsrank",
            "战棋榜",
            "炉石榜",
            "炉石排行",
            "炉石排行榜",
        }

        if tokens and tokens[0].lstrip("/／").lower() in command_names:
            tokens = tokens[1:]

        return tokens

    def _parse_alias_tokens(self, text: str, alias: str) -> list[str]:
        text = text.strip()
        if text.startswith("/"):
            text = text[1:]
        if text.startswith(alias):
            rest = text[len(alias):].strip()
            if not rest:
                return []
            try:
                return shlex.split(rest)
            except ValueError:
                return rest.split()
        return []

    def _mode_from_token(self, token: str | None) -> str | None:
        if not token:
            return None
        return self.MODE_ALIASES.get(token.lower()) or self.MODE_ALIASES.get(token)

    def _mode_short(self, mode: str) -> str:
        return "duo" if mode == "battlegroundsduo" else "bg"

    def _parse_optional_mode(self, tokens: list[str]) -> tuple[str, list[str], bool]:
        if tokens:
            mode = self._mode_from_token(tokens[0])
            if mode:
                return mode, tokens[1:], True
        return "battlegroundsduo", tokens, False

    def _parse_int(self, value: str, default: int) -> int:
        try:
            n = int(value)
            return max(1, min(n, self._max_output_limit()))
        except Exception:
            return default

    def _parse_int_unlimited(self, value: str) -> int | None:
        try:
            return int(value)
        except Exception:
            return None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=45, connect=10, sock_read=30)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 AstrBot-HSRank/1.5",
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://hs.blizzard.cn/community/leaderboards/",
                },
            )
        return self.session

    async def _json_get(self, url: str, params: dict) -> dict:
        session = await self._ensure_session()
        async with session.get(url, params=params) as resp:
            text = await resp.text()

            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}: {text[:120]}")

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"接口返回不是 JSON: {text[:120]}")

    async def _get_current_season_id(self) -> int:
        try:
            data = await self._json_get(
                self.INTL_API,
                {
                    "region": "AP",
                    "leaderboardId": "battlegrounds",
                    "page": 1,
                },
            )
            season_id = data.get("seasonId") or data.get("season_id")
            if season_id:
                return int(season_id)
        except Exception as e:
            logger.warning(f"读取当前赛季 ID 失败，使用默认赛季 ID：{e}")

        return self.FALLBACK_SEASON_ID

    async def _fetch_cn_page(self, mode: str, season_id: int, page: int) -> dict:
        data = await self._json_get(
            self.CN_API,
            {
                "page": page,
                "mode_name": mode,
                "season_id": season_id,
            },
        )

        if data.get("code") != 0:
            raise RuntimeError(f"国服接口返回异常: {data.get('message') or data}")

        return data

    async def _get_leaderboard(self, mode: str, force: bool = False) -> dict:
        now = time.time()
        cached = self.cache.get(mode)

        if (
            not force
            and cached
            and now - cached["fetched_at"] < self.CACHE_TTL_SECONDS
        ):
            cached["rows"] = self._merge_fake_rows(mode, cached.get("official_rows", []))
            return cached

        async with self.locks[mode]:
            cached = self.cache.get(mode)
            now = time.time()

            if (
                not force
                and cached
                and now - cached["fetched_at"] < self.CACHE_TTL_SECONDS
            ):
                cached["rows"] = self._merge_fake_rows(mode, cached.get("official_rows", []))
                return cached

            data = await self._fetch_full_cn_leaderboard(mode)
            self.cache[mode] = data
            return data

    async def _fetch_full_cn_leaderboard(self, mode: str) -> dict:
        season_id = await self._get_current_season_id()

        first = await self._fetch_cn_page(mode, season_id, 1)
        root = first.get("data") or {}
        first_rows = root.get("list") or []

        total = int(root.get("total") or len(first_rows))
        per_page = len(first_rows) or 25
        total_pages = max(1, math.ceil(total / per_page))

        official_rows = [self._normalize_row(row) for row in first_rows]

        sem = asyncio.Semaphore(6)

        async def fetch_page(page_no: int):
            async with sem:
                page_data = await self._fetch_cn_page(mode, season_id, page_no)
                return (page_data.get("data") or {}).get("list") or []

        if total_pages > 1:
            pages = await asyncio.gather(
                *[fetch_page(page_no) for page_no in range(2, total_pages + 1)]
            )

            for page_rows in pages:
                official_rows.extend(self._normalize_row(row) for row in page_rows)

        official_rows = [row for row in official_rows if row["name"]]
        official_rows.sort(
            key=lambda row: int(row["rank"])
            if str(row["rank"]).isdigit()
            else 999999
        )

        self._record_history(mode, official_rows)

        official_update = self._extract_update_time(first)
        rows = self._merge_fake_rows(mode, official_rows)

        return {
            "mode": mode,
            "season_id": season_id,
            "rows": rows,
            "official_rows": official_rows,
            "total": total,
            "official_update": official_update,
            "fetched_at": time.time(),
            "fake_only": False,
        }

    def _normalize_row(self, row: dict) -> dict:
        rank = self._first(row, "position", "rank", "ranking")
        name = self._first(row, "battle_tag", "battletag", "name", "player")
        score = self._first(row, "score", "rating", "mmr")

        return {
            "rank": rank if rank is not None else "",
            "name": str(name) if name is not None else "",
            "score": score if score is not None else "",
            "note": "",
            "fake": False,
        }

    def _first(self, row: dict, *keys: str):
        for key in keys:
            if key in row and row[key] not in (None, ""):
                return row[key]
        return None

    def _extract_update_time(self, obj) -> str:
        wanted_keys = {
            "updated_at",
            "updatedat",
            "update_time",
            "updatetime",
            "last_update",
            "lastupdate",
            "last_updated",
            "lastupdated",
            "refresh_time",
            "refreshtime",
        }

        def walk(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    key = str(k).lower()
                    if key in wanted_keys:
                        return self._format_any_time(v)

                    found = walk(v)
                    if found:
                        return found

            if isinstance(value, list):
                for item in value:
                    found = walk(item)
                    if found:
                        return found

            return ""

        return walk(obj)

    def _format_any_time(self, value) -> str:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai")).strftime(
                "%Y-%m-%d %H:%M"
            )

        if isinstance(value, str):
            s = value.strip().replace("T", " ")
            return s[:16]

        return ""

    def _format_fetch_time(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M"
        )

    def _format_rows(
        self,
        mode: str,
        rows: list[dict],
        data: dict,
        title_suffix: str,
        total_count: int,
        limit: int,
    ) -> str:
        update_time = data.get("official_update")
        if data.get("fake_only"):
            time_label = "手动名单时间"
            shown_time = self._format_fetch_time(data["fetched_at"])
        elif update_time:
            time_label = "榜单更新时间"
            shown_time = update_time
        else:
            time_label = "数据获取时间"
            shown_time = self._format_fetch_time(data["fetched_at"])

        season_part = f"，赛季ID:{data['season_id']}" if data.get("season_id") else ""

        lines = [
            f"【{self.MODE_NAMES[mode]}】天梯排行榜{title_suffix}"
            f"（{time_label}{shown_time}{season_part}）："
        ]

        if not rows:
            lines.append("没有找到匹配结果。")
            if not update_time and not data.get("fake_only"):
                lines.append("注：接口未返回官方榜单更新时间，以上为本次数据获取时间。")
            return "\n".join(lines)

        for row in rows[:limit]:
            if row.get("fake"):
                note = row.get("note") or self.DEFAULT_FAKE_NOTE
                lines.append(f"排名:{str(row['rank']).ljust(8)}（手动添加） 昵称:{row['name']}（{note}）")
                lines.append(f"积分:{row['score']}（手动添加）")
            else:
                lines.append(f"排名:{str(row['rank']).ljust(8)} 昵称:{row['name']}")
                lines.append(f"积分:{row['score']}")

        if total_count > limit:
            lines.append(f"……共命中 {total_count} 条，仅显示前 {limit} 条。")

        fake_count = len([row for row in rows[:limit] if row.get("fake")])
        if fake_count:
            lines.append("注：带“手动添加”的记录不是官方排行榜，是群友自己装逼写上去的。")

        if not update_time and not data.get("fake_only"):
            lines.append("注：接口未返回官方榜单更新时间，以上为本次数据获取时间。")

        return "\n".join(lines)

    def _help_text(self) -> str:
        return (
            "炉石战棋榜用法：\n"
            "\n"
            "查人：\n"
            "战棋榜 某人                    按昵称关键词查询；命中双打/单打哪个就显示哪个\n"
            "战棋榜 id 某人                 精确查询昵称；命中后显示所在排名区间\n"
            "战棋榜 查 关键词               模糊查询昵称关键词\n"
            "战棋榜 涨跌 某人               查询某人在双打和单打中的排名涨跌\n"
            "\n"
            "当前排行榜：\n"
            "战棋榜 双打排行                显示双打前20名\n"
            "战棋榜 双打排行 50             显示双打前50名\n"
            "战棋榜 单打排行                显示单打前20名\n"
            "战棋榜 单打排行 50             显示单打前50名\n"
            "\n"
            "涨跌榜：\n"
            "战棋榜 最强                    显示双打+单打排名上升最多的玩家\n"
            "战棋榜 最强 20                 显示双打+单打排名上升最多前20名\n"
            "战棋榜 最强 bg 20              只显示单打排名上升最多前20名\n"
            "战棋榜 最菜                    显示双打+单打排名下降最多的玩家\n"
            "战棋榜 最菜 20                 显示双打+单打排名下降最多前20名\n"
            "战棋榜 最菜 bg 20              只显示单打排名下降最多前20名\n"
            "\n"
            "单模式查询：\n"
            "战棋榜 duo 关键词              只查双打昵称关键词\n"
            "战棋榜 bg 关键词               只查单打昵称关键词\n"
            "战棋榜 id duo 某人             只精确查询双打昵称\n"
            "战棋榜 id bg 某人              只精确查询单打昵称\n"
            "战棋榜 涨跌 duo 某人           只查某人的双打涨跌\n"
            "战棋榜 涨跌 bg 某人            只查某人的单打涨跌\n"
            "\n"
            "数据更新：\n"
            "战棋榜 更新                    手动刷新并记录双打+单打榜单\n"
            "战棋榜 更新 duo                只刷新并记录双打榜单\n"
            "战棋榜 更新 bg                 只刷新并记录单打榜单\n"
            "\n"
            "手动榜：\n"
            "战棋榜 add duo 昵称 排名 积分 [备注]    添加一条双打手动记录\n"
            "战棋榜 add bg 昵称 排名 积分 [备注]     添加一条单打手动记录\n"
            "战棋榜 fake [数量]                      查看手动榜\n"
            "战棋榜 delfake duo 昵称                 删除双打手动记录\n"
            "战棋榜 delfake bg 昵称                  删除单打手动记录\n"
            "\n"
            "其他：\n"
            "战棋榜 设置                    查看后台配置说明"
        )

    async def terminate(self):
        if self.auto_snapshot_task and not self.auto_snapshot_task.done():
            self.auto_snapshot_task.cancel()
            try:
                await self.auto_snapshot_task
            except asyncio.CancelledError:
                pass

        if self.session and not self.session.closed:
            await self.session.close()
