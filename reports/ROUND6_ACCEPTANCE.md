# 完整版第六轮 · 规则编辑界面 + 独立 Windows 测试包 · 验收报告（2026-11-20）

## 结论
**PASS**。规则工作流（编辑→预览→发布→全流程）在 UI 与 API 两级测通；
独立 Windows 候选包交付并可直接打开测试。

## 打包前顺手修（复核发现）
1. 影子开关 + 程序自动选日期：`--with-tarot-shadow` 未显式给 `--days` 时影子不运行
   ——改为以主流程结果 `rep['days']` 为准。已验证：开关打开自动选日 → 影子运行并落盘。
2. 正常路径验收断言：零准入/零人工复核会触发 AssertionError——断言降级为
   `summary.acceptance` 状态记录（评分完成 / 有效零结果 / 零准入零复核），
   界面按日展示，不再中断运行。

## 1. 可编辑规则（复用指标目录与解释器）
- 盘后筛选与盘中触发分区编辑；加删条件、AND/OR 一级分组；
  每个因子显示单位/周期/粒度/可用时间（来自 factor_catalog）；
  服务端契约校验 + 因子登记校验（contract.py 同一套）。
- 缺数据/未知值由解释器三值语义给出「待核验」及原因（UNKNOWN 不静默当 0）。
- 草稿持久化在 run/state/rules（import_rule → 草稿；publish → 新版本），
  重启后可找回；已冻结计划绑定原 rule_ref+sha 不受影响。

## 2. 草稿 → 预览 → 发布流程（实测）
`tests/test_round6_ui_flow.py`（API 级驱动真实 Web 服务）：
- 改盘后 ret_60d 0.10→0.20、盘中 decline_from_prev_close 0.035→0.05；
- 预览：候选 257→99，lost 名单逐只带字段值；gained 空；
- 发布 `strong_pullback_draft@v1` 后实跑：候选 99 == 预览 99；
- 收件箱回执 218→96（盘中条件收紧生效）；
- 重启后发布版本仍在；冻结计划仍指向原版本。

## 3. 独立 Windows 测试包
- 位置：`D:\stargazer\stagazer_r\full_edition_package\`
- 内容：完整版代码 + 内置冻结快照（P=20261117/D=20261118）+ 独立 venv
  （flask/pandas/numpy/pyarrow 已装）+ `start.bat`（双击开浏览器）+ README。
- 首次启动自动导入并发布内置强势回调规则 v1；
- 冒烟：包内 `full.webapp` 起服务（api/state、api/factors、api/rules 全通），
  包内 CLI 全链跑通（13 产物）；
- 写入仅限包内 run/，删除即卸载。

## 收工指纹
正式 EXE sha256 前 16：`50f4b60516b71007`（不变）；v1.1 冻结件 verify ok。

## 下一步
日常数据导入与缺口补齐（纯规则版使用闭环）；多日影子对照继续积累。

## 重出记录（同日，响应包测试复核）
四条缺陷修复并回归（`tests/test_round6_ui_flow.py` 全绿，UI_FLOW_PASS）：
1. 运行按钮现携带 rule_ref——验收改走 /api/run 按钮路径，run_manifest.rule_ref
   实测为新版本（strong_pullback_draft@v8），候选 99==预览、回执 96。
2. 预览补盘中差异：scratch 监控重放给出回执增减（218→96），不发布草稿。
3. 草稿可从界面重开：下拉含 [草稿]，载入后继续编辑（实测 ret_60d=0.20 保留）。
4. 黄金档 vendor 内置化（包脱离主仓库可跑纯规则）；/api/tarot_status 探测
   repo+sklearn，界面顶部如实显示可用性；包内 venv 已装 sklearn；
   包级冒烟：纯规则全链在包内 venv 独立跑通，Tarot 状态 available=true
   （本机有主仓库；README 如实写明 venv 需本机 Python 3.10+）。
候选包已重出：`stagazer_r\full_edition_package\`。
