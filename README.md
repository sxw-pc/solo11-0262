# 服务目标违约归责庭

本项目离线模拟服务目标合同、失败归因与赔付结算，使用 Python 3.11+ 标准库和 SQLite。它处理合成数据，不接真实监控或支付系统。

三个业务入口为 `slo`、`cause`、`credit`。目标和请求分母先封存；失败样本随后按请求 UID 去重并结合拓扑归因；赔付只有在归因未争议、拓扑代际未改变时才能结算。拓扑修订会令未结赔付冻结，已结金额需单独冲正。每个入口各有正常和失败 fixture，`path.json` 复核跨系统后果。

```sh
python -m sloattrib demo --db demo.sqlite
python -m sloattrib replay --db demo.sqlite
python -m unittest discover -s tests -v
```

独立执行时先在同一个新数据库运行 `slo`、`cause`、`credit` 的正常 fixture，再运行对应失败 fixture，例如 `python -m sloattrib slo --db state.sqlite --fixture fixtures/slo_normal.json`。重复事件 ID 且正文相同返回原决定；同 ID 异正文返回 `event_identity_conflict`。报告可从 `events` 查看，重放会逐表重建并比较业务状态。

## 合成多数派寄存器

`replicas` 登记每个演练分组的成员和在线状态。`register vote` 可为一个组内位置记录成员对提案及值的选票，`register apply` 仅为获得多数成员选票的提案真正写入该组位置。成员之后离线不会使已持久化的投票失效。

位置首次被投票时，其选民范围和多数门槛（`n//2+1`）即被冻结到 `register_slot_scopes`：节点之后重连、被撤出当前名单、或以同一节点名进入新组，都不能改写旧位置已有的票权。每个逻辑位置在 `register_positions` 只有一条对外状态，跨分组重放事件后仍只发布一个结果。

### 换组与投票权交割

管理员用 `replicas handover`（`mode` 为 `fulfill` 或 `withdraw`）选择一次交割方式，交接单按旧 epoch 唯一且可重入；`seal` 封存旧投票权，`complete` 初始化新组，重复命令返回同一决定，部署顺序可多次中断。

- 履行路线（`fulfill`）：交接期间旧组仍可收票，先用旧组把仍能当选的在途提案结清（含确定性回执 `receipt:slot<槽位>:<提案>`），再 `complete`。新组对该位置的投票和写入一律被 `old_position_unresolved` / `position_already_settled` 拒绝。
- 撤回路线（`withdraw`）：交接单开立即关停旧投票权。各节点用 `register confirm_no_vote` 留下可追查的末次无票确认；`withdraw_apply` 在全部冻结选民均已交代（投票或确认无票）且提案确未达多数时，把位置置为 `vacant`，给原客户带回执的 `proposal_withdrawn`；只要还有节点 `unaccounted`，就返回 `outcome_unverified`，新组不能抢写。位置腾空后新组才能真正写入自己的值。
- 在路上的旧票：封存后送达的选票须走 `deliver_late_vote` 并附 `cast_before_seal` 证明；重跑普通 `vote` 只会得到 `voting_closed`，二者结果明确区分。已确认无票的节点不能再补交票。若迟到票使旧提案达到冻结多数，`withdraw_apply`/`apply` 仍会结清旧提案，新组永远写不出第二个值。客户端回执丢失时重发 `apply` 拿回同一回执，不会产生第二条写入。

查询入口为 `register read`（逻辑位置对外结果）、`proposal_status`（原客户的提案与回执状态）和 `voter_state`（各冻结选民的末次投票状态）。

合成演练 fixture 用信封步骤 `{"subsystem": ..., "command": ..., "event_id": ...}` 在一个文件内驱动两个入口：

```sh
python -m sloattrib register --db handover.sqlite --fixture fixtures/register_handover_fulfill.json
python -m sloattrib register --db handover.sqlite --fixture fixtures/register_handover_withdraw.json
python -m sloattrib register --db handover.sqlite --fixture fixtures/register_handover_pending.json
python -m sloattrib replay --db handover.sqlite
```

三个 fixture 分别演练履行继承（对外读出 2）、撤回后新组写出 Q=9、以及仅拿到部分无票确认时迟到票结清 P 的待交割场景。两入口走相同事件重放接口，重放逐表重建并比较业务状态。
