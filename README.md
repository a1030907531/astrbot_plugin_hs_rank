# astrbot_plugin_hs_rank

炉石传说国服战棋排行榜查询插件。

## v1.7.4 变更

- 174：最菜榜不再把掉出榜单玩家排进榜单，改为末尾 `PS：以下人掉出榜单不统计`。
- 最强/最菜榜继续保留 1. 2. 3. 编号和积分变化显示。

- 最强/最菜涨跌榜带 1.2.3 编号；最强按排名上升优先、积分增加辅助排序。


- 数据文件迁移到 `AstrBot/data/plugin_data/astrbot_plugin_hs_rank/`，避免污染 `/AstrBot/data` 根目录。
- 首次启动会自动把旧的 `/AstrBot/data/plugin_data/astrbot_plugin_hs_rank/hsrank_state.json` 复制迁移到新目录。
- 历史记录、手动榜、后续卡牌缓存都建议放在这个插件数据目录。

- 修复 `/hsrank`、`/炉石榜`、`/炉石排行` 这类 command 入口和 regex 入口重复命中导致重复回复的问题。
- 现在无斜杠中文入口只保留 `战棋榜 XX`。
- 默认查人时只显示命中的模式：只有双打显示双打，只有单打显示单打，双打单打都有才显示两个。
- 新增每日自动记录。
- 后台配置开启 `enable_auto_snapshot` 后，插件启动会每天在指定时间自动刷新并记录排行榜。
- 自动记录不调用大模型，不烧 token。
- 支持随机延迟，避免固定时间集中请求。

## 主要命令

```text
战棋榜 某人
战棋榜 id 某人
战棋榜 查 关键词
战棋榜 涨跌 某人
战棋榜 更新
```

## 当前排行榜

```text
战棋榜 双打排行
战棋榜 双打排行 50
战棋榜 单打排行
战棋榜 单打排行 50
```

默认 20 行。

## 涨跌榜

```text
战棋榜 最强
战棋榜 最强 20
战棋榜 最强 bg 20
战棋榜 最菜
战棋榜 最菜 20
战棋榜 最菜 bg 20
```

- 最强 = 排名上升最多。
- 最菜 = 排名下降最多。
- 至少需要两天/两次不同日期的记录。

## 自动记录后台配置

安装后在 AstrBot WebUI 插件配置里设置：

```text
enable_auto_snapshot: true
auto_snapshot_time: 09:10
auto_snapshot_modes: ["duo", "bg"]
auto_snapshot_jitter_minutes: 5
```

说明：

- 不烧 token，因为只是 Python 定时请求排行榜接口。
- 默认关闭，需要后台手动开启。
- 建议每天 1 次。
- 不建议设置成高频刷新。

## 历史记录是否会丢？

默认不会因为删除旧插件目录而丢。

历史记录默认在：

```text
/AstrBot/data/plugin_data/astrbot_plugin_hs_rank/hsrank_state.json
```

只要你不是删除这个文件，或者清空整个 `/AstrBot/data`，涨跌历史就会保留。
