# 服务目标违约归责庭

本项目离线模拟服务目标合同、失败归因与赔付结算，使用 Python 3.11+ 标准库和 SQLite。它处理合成数据，不接真实监控或支付系统。

三个业务入口为 `slo`、`cause`、`credit`，换汇结算入口为 `fx`。目标和请求分母先封存；失败样本随后按请求 UID 去重并结合拓扑归因；赔付只有在归因未争议、拓扑代际未改变时才能结算。拓扑修订会令未结赔付冻结，已结金额需单独冲正。`fx` 在合同币种与客户收款币种不同时工作：报价带币对、整数比例（ppm）、生效区间和面额；软占用（hold）是未承诺额度，争议或拓扑升代时释放；硬锁（lock）是锁汇承诺，冻结期只暂停、额度不释放，多张收据可共享一把锁。整张固定收款的汇票必须用成组锁价（group_lock）覆盖全部面额，且只有每条腿都被外部回执确认（confirm）才宣告履约；只确认一笔时合同不履约，已确认的额度也不释放。收据记录锁定价汇率快照、客户实收最小货币单位和不可退费用，冲正（reverse）按原收据面额和原币对核销，折算差额与费用不会变成下一笔可用面额。每个入口各有正常和失败 fixture，`path.json` 复核跨系统后果，`credit_fx.json` 演示 8 单位赔付的分段锁价（先锁 5、浮动补 3）。

```sh
python -m sloattrib demo --db demo.sqlite
python -m sloattrib replay --db demo.sqlite
python -m unittest discover -s tests -v
```

独立执行时先在同一个新数据库运行 `slo`、`cause`、`credit` 的正常 fixture，再运行对应失败 fixture，例如 `python -m sloattrib slo --db state.sqlite --fixture fixtures/slo_normal.json`。重复事件 ID 且正文相同返回原决定；同 ID 异正文返回 `event_identity_conflict`。报告可从 `events` 查看，重放会逐表重建并比较业务状态。
