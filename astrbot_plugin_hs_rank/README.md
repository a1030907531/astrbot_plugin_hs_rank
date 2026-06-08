# astrbot_plugin_hs_rank

作者：Scoy

炉石传说国服战棋排行榜查询插件。

## v1.7.0 变更

- 新增每日自动记录。
- 后台配置开启 `enable_auto_snapshot` 后，插件启动会每天在指定时间自动刷新并记录排行榜。
- 自动记录不调用大模型，不烧 token。
- 支持随机延迟，避免固定时间集中请求。

## 主要命令

```text
战棋榜 Scoy
战棋榜 id Scoy
战棋榜 查 Scoy
战棋榜 涨跌 Scoy
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
/AstrBot/data/hsrank_state.json
```

只要你不是删除这个文件，或者清空整个 `/AstrBot/data`，涨跌历史就会保留。
