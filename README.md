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

## 地址回收交接

地址每次易主都产生一个新的**分配代际**（`dns_allocations`），并为该 `(地址, 代际)` 派生只对本轮租用有效的租用标记。标记校验只承认本地已 mint 的行：上一轮标记晚到或重放只会得到 `stale_lease`，监听器不读取业务名、客户端意图或后台缓存表来猜测入站身份。

回收由 `recycle` 审批开启，接受两种交付：

- `exclusive`：在有证据证明旧权利耗尽前，普通监听器拒绝新所有者连接（`reclaim_pending`），无身份旧连接也不会误投到新服务。
- `compatible`：兼容入口（`listen kind=compat entry_reclaim=...`）可立即兑现带本轮标记的新连接，同时把无身份连接继续送到旧所有者自己的监听端；旧权利耗尽后用 `promote` 完成独占。

递归缓存转发用 `emit_forward`（在途）和 `accept_forward`（下游接受）记录。下游签发的有效期取自身 TTL 与上游**绝对截止**的较小值，绝不外延；在途未接受的转发会持续计入未决分支，中央缓存表一时看不到引用不能证明地址无旧客户。

分支交回通过逐项 `withdraw` 命令完成：它绑定实际关停的代际，必须枚举自己发出的转发；仍有活跃后继时只产生 `partial_surrender`。`examine_reclaim` 从逐分支记录重算耗尽证明，分批送达的确认无法合成不存在的证明；全部分支提前交回时可在自然到期前完成独占（basis 为 `early_surrender`），否则为 `natural_expiry`。所有命令幂等且可在进程重入后重放，`replay` 逐表重建并核对业务状态与每个决定。

