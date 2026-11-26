# 完整版第一轮 · 纯规则强势回调闭环 · 验收报告（2026-11-20）

## 结论
**PASS**。真实行情（market_data vipdoc，P=20261117 → D=20261118）上跑通
「本地行情 → 规则选股 → 预览/冻结（含黄金档参考）→ 次日逐根输入 → 模拟提醒 →
重启续跑 → 复盘」全链，逐项满足第一轮验收标准。

## 交付物
- 开发根：`D:\stargazer\stagazer_r\full_edition\`（独立 git 仓库，首次提交 584d005；主仓库按原约定忽略 stagazer_r，误提入的暂存已撤除）
- 运行目录：`full_edition/run/`（自有 state/inbox/locks/runs，端口 17891 预留）
- 快照：`full_edition/snapshot/`（5,574 只日线 91 交易日 + D 日全 universe 5 分钟线，指纹覆盖数据文件）
- 缺数样本：`full_edition/snapshot_holes/`（40 只丢分钟 + 40 只丢量额字段，固定种子）
- 复跑命令：
  ```
  cd D:\stargazer\stagazer_r\full_edition
  python -m full.cli run --rule strong_pullback@v1 --days 20261118     # 全链
  python -m full.cli diff-selection --day 20261117 --field ret_60d --op '>=' --value 0.20
  python -m full.cli diff-trigger --day 20261118 --field decline_from_prev_close --op '>=' --value 0.05
  ```

## 验收逐项
| 项 | 结果 |
|---|---|
| 盘后阈值编辑 → 候选变化 | ret_60d 0.10→0.20：257 → 99，lost 158 只逐一点名，gained 空（单调符合预期） |
| 盘中条件编辑 → 提醒变化 | decline_from_prev_close 0.035→0.05：touch 109→83，收件箱回执 218→166 |
| 缺线/迟到显示原因 | 缺数样本：候选 600757 登记 NO_MINUTE_FILE_FOR_DAY；指标覆盖按字段报缺失（ma5 36 缺，原因 INSUFFICIENT_COMPLETED_5M_BARS）；完整快照重跑同一日期恢复判定 |
| 重启/重放不重复发送 | 重启复核 TERMINAL_KEEP、不重发；218 条回执去重键 0 重复 |
| 断网/无密钥/无权重 | full/*.py 无网络/密钥引用（扫描为空）；全链本地确定性计算，无模型加载 |
| 同冻结数据两遍逐笔对齐 | 两个独立运行根 9 个产物文件哈希全同（候选、黄金档、触发时间、回执、复盘明细） |

## 基线与隔离（开工=收工）
- 正式 EXE sha256 前 16：`50f4b60516b71007`（开工=收工，未动）
- v1.1 冻结件：`tools/freeze_v1.py --verify` ok（7 快照 + 82 回放 + 6 报告，开工=收工）
- 插曲记录：一次 sys.path 顺序错误让探针写进了 v1.1 的 lab_data，经 git 核对为同字节写入（零实质差异），已修正 import 顺序并复核
- 规则版本：完整版发布的 strong_pullback@v1 SHA `e7550f44…` 与 v1.1 冻结件一致

## 链上真实数字（20261118）
- 选股（P=11-17）：universe 5,574 → 候选 257 → 冻结 256（1 只无盘前参考收盘价被剔并记录）
- 冻结：每只候选带 ATR 观察区 + 黄金档参考（197/256 有已确认波段回撤档，59 只无合格波段如实为空，标注 TEMPORARY_REFERENCE_ONLY）
- 监控（D=11-18）：12,288 根 bar、109 次触位、收件箱 218 条回执（TOUCH+指标补充）
- 复盘账本：样本、NO_FILL 盈亏审计、标签覆盖全部落盘

## 已知边界（如实）
- 价格口径 RAW 不复权（qfq_factor=1），除权日涨幅畸变未调整；corporate_events 未填充（本规则不用）
- 板块成分/强势池快照为空，板块相关规则暂不可发布（快照 limitation 已标注）
- 黄金档为现行价格规则参考（golden_ladder_v3），非生产 tarot 冻结口径
- 正式版内置策略与地图的同切片对照：待接（方案要求的对照记录，建议下一轮用同一 snapshot 跑生产选股输出对比）
