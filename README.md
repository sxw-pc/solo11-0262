# 服务目标违约归责庭

本项目离线模拟服务目标合同、失败归因与赔付结算，使用 Python 3.11+ 标准库和 SQLite。它处理合成数据，不接真实监控或支付系统。

三个业务入口为 `slo`、`cause`、`credit`。目标和请求分母先封存；失败样本随后按请求 UID 去重并结合拓扑归因；赔付只有在归因未争议、拓扑代际未改变时才能结算。拓扑修订会令未结赔付冻结，已结金额需单独冲正。每个入口各有正常和失败 fixture，`path.json` 复核跨系统后果。

```sh
python -m sloattrib demo --db demo.sqlite
python -m sloattrib replay --db demo.sqlite
python -m unittest discover -s tests -v
```

独立执行时先在同一个新数据库运行 `slo`、`cause`、`credit` 的正常 fixture，再运行对应失败 fixture，例如 `python -m sloattrib slo --db state.sqlite --fixture fixtures/slo_normal.json`。重复事件 ID 且正文相同返回原决定；同 ID 异正文返回 `event_identity_conflict`。报告可从 `events` 查看，重放会逐表重建并比较业务状态。

## 跨结算期总额分摊

同一合成事故横跨相邻结算期时，`credit` 子系统可批准一份受总额约束的分摊（`apportion`，或带 `total` 的 `split_period`）。各周期的有责份额直接取自 `cause.liable_uid_detail`：即已 `decided` 事故窗口内可核实的有责请求 UID。跨分区（source）重复见到同一 UID 只计一次；同一 UID 同时出现在相邻两期则标记为 `conflicted`，其整数份额先计入有责总额，但挂入具名余额 `unattributed`，在归期证明（`confirm_attribution` / `attribute_uid`）到达前不形成任何周期债权。

分摊以整数最小支付单位（`unit`，默认 1）按 UID 权重用最大余数法分配整单位，并受各周期 `cap` 约束。三份具名余额与两本周期账共同保住总额：

- `dust`：不足一个最小支付单位的尾数（`total % unit`）；
- `unattributed`：归期冲突/归期未定 UID 对应的份额；
- `cap_blocked`：超过周期上限、无法记账的份额。

`balances`（别名 `reconcile`）返回 `ledgers`（各周期有效债权本金）、`signed`（已签收金额）、`balances`、`unallocated` 与 `conserved`。守恒式为：有效债权 + `unallocated`（具名余额 + 已授权未出账）+ 冲正净额 = 总额。已存在签收回执（或周期已 `close_period`）后，`reapportion` 以 `period_already_signed` 拒绝用新权重重新分摊。

两种周期关闭合同：

- `contract: "next_bill"`：关闭时只签收证据一致支持的份额，已签收账本不可回写；迟来的归期证明为已签收周期生成下一张 `…:forward:<proof>` 草稿账。
- `contract: "return_period"`：归期确认后为原期生成 `…:catchup:<proof>` 补账单；确认前该份额不在任何周期授权内，无法在别期开单。

旧事故复审降额用 `downgrade`（别名 `reassess` / `review_adjust`，可给 `remove_uids`、`reduce` 或更低的 `total`）。它只消耗具名余额、未签收草稿债权和已明确冲正的债权；未冲正的已签收金额会使操作以 `downgrade_locked_by_signed` 被拒。

独立 fixture `slo_apportion.json`、`cause_apportion.json`、`credit_apportion.json` 构造总额 5、两期上限 2/4、`uid-5` 跨期归期冲突、一期签收后才收到归期证明的用例：

```sh
rm -f apportion_new.sqlite
python -m sloattrib slo    --db apportion_new.sqlite --fixture fixtures/slo_apportion.json
python -m sloattrib cause  --db apportion_new.sqlite --fixture fixtures/cause_apportion.json
python -m sloattrib credit --db apportion_new.sqlite --fixture fixtures/credit_apportion.json
python -m sloattrib replay --db apportion_new.sqlite   # {"verified": true, "decisions": 25}
```

`tests/test_apportion.py` 核对两本账与未分配余额在每个检查点都守恒，并覆盖触顶、最小单位尾数、两种合同、签收锁定与降额规则。
