# 服务目标违约归责庭

本项目离线模拟服务目标合同、失败归因与赔付结算，使用 Python 3.11+ 标准库和 SQLite。它处理合成数据，不接真实监控或支付系统。

三个业务入口为 `slo`、`cause`、`credit`。目标和请求分母先封存；失败样本随后按请求 UID 去重并结合拓扑归因；赔付只有在归因未争议、拓扑代际未改变时才能结算。拓扑修订会令未结赔付冻结，已结金额需单独冲正。每个入口各有正常和失败 fixture，`path.json` 复核跨系统后果。

```sh
python -m sloattrib demo --db demo.sqlite
python -m sloattrib replay --db demo.sqlite
python -m unittest discover -s tests -v
```

独立执行时先在同一个新数据库运行 `slo`、`cause`、`credit` 的正常 fixture，再运行对应失败 fixture，例如 `python -m sloattrib slo --db state.sqlite --fixture fixtures/slo_normal.json`。重复事件 ID 且正文相同返回原决定；同 ID 异正文返回 `event_identity_conflict`。报告可从 `events` 查看，重放会逐表重建并比较业务状态。

## 合成服务名称和客户端访问

`dns` 的 publish 设置一个逻辑服务名、地址、服务身份及缓存有效时长。lookup 在有效期内返回同一客户端的缓存。`gateway` 的 listen 在地址上注册模拟服务，deliver 按缓存地址记录实际投递。缓存到期刻需重新查名。所有地址和请求均为离线合成标识。
