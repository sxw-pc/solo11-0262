# 服务目标违约归责庭

本项目离线模拟服务目标合同、失败归因与赔付结算，使用 Python 3.11+ 标准库和 SQLite。它处理合成数据，不接真实监控或支付系统。

三个业务入口为 `slo`、`cause`、`credit`。目标和请求分母先封存；失败样本随后按请求 UID 去重并结合拓扑归因；赔付只有在归因未争议、拓扑代际未改变时才能结算。拓扑修订会令未结赔付冻结，已结金额需单独冲正。每个入口各有正常和失败 fixture，`path.json` 复核跨系统后果。

```sh
python -m sloattrib demo --db demo.sqlite
python -m sloattrib replay --db demo.sqlite
python -m unittest discover -s tests -v
```

独立执行时先在同一个新数据库运行 `slo`、`cause`、`credit` 的正常 fixture，再运行对应失败 fixture，例如 `python -m sloattrib slo --db state.sqlite --fixture fixtures/slo_normal.json`。重复事件 ID 且正文相同返回原决定；同 ID 异正文返回 `event_identity_conflict`。报告可从 `events` 查看，重放会逐表重建并比较业务状态。

## 合成机架液冷

`coolant` 注册合成泵和机架供回阀，每仿真机架回路需同时打开供、回两阀。一台泵挂载的完整回路过多时，该泵不能用于提交机架服务。`rack serve` 按实际当前泵和阀状态登记请求，并在记录中写明真正供冷的泵。无真实设施接入。

### 阀门三阶段凭据与泵保护

阀门动作不是一次状态翻转，而是三类不同事件：`queue`（命令排队）、`start`（执行器开始）、`arrive`（阀位实际到位）。只有 `arrive` 才改变真实阀位，因此命令下发顺序不能推定现场顺序；先到的到位回执即使缺少开始回执也直接成立，迟到的开始回执只得到 `phase_superseded`。

带压供阀在同回路回阀到位之前实际打开，会立即触发该泵保护停转（`pump_tripped`），停转与供阀命令同事件留痕。之后晚到的回阀不能消除停转：泵保持 `tripped`，其所有回路失去供冷资格，直到 `permit` 签发有效许可、现场不再有“供开回未开”的危险状态，并由真实 `reset` 复位；许可一次有效，复用即拒绝。`actuate` 是把三阶段压缩到一个观察事件的现场简写，仍走同一套物理规则。

### 应急支路与双泵同接

第二台泵要与基准泵同接一个机架，必须持有该机架物理独立应急支路的预登记（`register_branch`）、业务授权（`grant_branch`）并在两阀实际接通且未超容量后通过 `validate_branch`。只下发未到位的应急阀不能声称连续供冷；未登记、未授权或未验证的额外泵在共享机架时被排除，每台泵仍独立受自身容量约束。

### 两类换路工单（`workorder`）

工单本身不发阀令，只持久化阶段，并依据**实际到位且仍适用**的回路判断下一阶段是否成立，因此完成回执乱序或重复推进都可重入：

- `bypass`：先授权并验证应急泵 K 维持 A，再卸载 P 的 A 回路，最后把 B 接到运行中的 P。切换途中到达的 A 请求由 K 实际服务。
- `downtime`：不启用 K，先 `drain` 阻断 A 的新请求（请求得到 `request_blocked` 拒绝凭据），卸载 P，再接 B；真实无服务时间由 `rack_outages` 按阻断时刻和负责人 `accept_outage` 时刻记录，不可事后补造。

两类工单都以 B 接入可运行的 P 收尾。被拒绝的 `serve` 按 UID 持久化在 `rack_rejections`，重放同一请求只返回原始拒绝决定，冷却恢复也不会把历史拒绝改写成成功。所有命令与结果都进入 `events`，`replay` 逐事件重放并逐表比对业务状态。
