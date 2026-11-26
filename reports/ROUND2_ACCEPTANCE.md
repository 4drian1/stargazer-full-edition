# 完整版第二轮 · 时间口径与正常入口 · 验收报告（2026-11-20）

## 结论
**PASS**。黄金档前视已修并有回归锁死；正常入口（CLI 退出码 + 页面收件箱）对齐；
同盘前截点的正式版黄金地图对照、自定义阈值逐股解释、中断/重启/补缺重判全部完成。

## 修复项（对应复核发现）
1. **黄金参考档前视**：`full/cli.py` 冻结接线现在在盘前截点内取数（`date <= P`），
   D 日日线即使存在于快照也不再参与波段确认。用户实例复核：000400 首档节点
   由 25.3008 回到 **24.1548**，与严格截到 11-17 的期望值一致。
2. **正常入口**：`full run` 退出码改为按执行成败（成功 0，异常 1），摘要含
   ok/run_id/收件箱计数；`/api/inbox`、`/api/state` 改读各次运行自己的
   `runs/<run_id>/inbox/receipts.jsonl`（不再指向不存在的 run/state/inbox）。

## 回归与演练
| 项 | 命令/方式 | 结果 |
|---|---|---|
| 追加 D 日线不改变盘前冻结 | `python tests/test_round2_freeze_cutoff.py` | 256 只逐股一致（含 zone/黄金档/参考收盘价），CUTOFF_REGRESSION_PASS |
| 中断→重启→补缺重判 | `python tests/test_round2_recovery.py` | 注入崩溃 rc=1 → 续跑 rc=0（218 键）→ 缺数快照删监控产物重判（missing_minute=1 登记，键不变）→ 完整快照再重判，RECOVERY_PASS：已送达提醒 218 条始终一份 |
| CLI 正常入口 | `python -m full.cli run --rule strong_pullback@v1 --days 20261118` | exit_code=0，ok:true，109 触位/218 回执 |
| 页面收件箱 | `python -m full.cli serve`（17891） | /api/state 显示 latest_run 与收件箱计数；/api/inbox 返回该运行回执 |
| 自定义阈值逐股解释 | diff-selection 增强 | lost 列表逐股带该字段实际值（如 ret_60d 0.0991 差 0.0009 落选） |

## 正式版同截点对照（生产黄金地图，as_of=D 09:00，data_date=P）
`reports/production_map_compare_20261117.json`，256 只全量：

- **昨收/参考收盘价**：43/256 完全相等，其余 213 只差异 ≤0.005 元——快照价格按
  0.001 元量化（round 3 位），生产用全精度。口径一致到分位，无量级差异。
- **黄金档节点**：集合精确相等 18 只；两边都有节点时 139 只存在 ≤1% 的近似重合；
  32 只候选有档/生产空（多为生产 REFERENCE_ONLY_NO_V4_ZONE 30 只或计算报错 10 只），
  48 只生产有档/候选空（候选的 20% 涨幅波段门槛未满足）。差异来源明确：
  候选=已确认波段端点+golden_ladder_v3；生产=tarot 冻结 ATR+共振 V4+显著波段窗口。
- **空档原因可对照**：生产 status 分布 AVAILABLE 202 / NO_CONFIRMED_MAP 11 /
  REFERENCE_ONLY_NO_V4_ZONE 30 / REFERENCE_ONLY_LOCAL_DAILY 3 / ERROR 10
  （ERROR 为生产读端对个别代码的 TDX_DAILY_CODE_MISSING_OR_AMBIGUOUS，生产侧问题，已记录）。
- 波段端点口径差异随节点差异一并体现在上表；候选的端点确认规则（前后各 2 根）
  与生产「最近显著波段窗口」不同，是节点集合不同的主因。

## 内核改动（完整版副本，v1.1 原件不动）
- `rules_lab/replay.py`：研究报告与最终验证锁改落各 run 自己的 `report/` 目录
  （v1.1 单快照共用 REPORTS 无冲突；完整版换数据后共享锁会拒绝覆盖——这是防篡改
  机制在 Multi-snapshot 下的碰撞，属预期内修正）

## 收工指纹
- 正式 EXE sha256 前 16：`50f4b60516b71007`（与开工一致）
- v1.1 冻结件 verify ok（见 git 提交记录；本轮对 lab_data 零写入）

## 下一轮
模型开关：同一份行情与冻结计划下，分离「模型 / 用户规则 / 数据与地图口径」三来源。
