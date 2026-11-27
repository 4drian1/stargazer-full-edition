# Stargazer Agent 接入

MCP 和钉钉共用工具层与 SQLite 任务账本。查询同步返回，选股、回放、回测、因子挖掘、导入及补缺先受理并返回任务 ID。后台复用完整版的现有执行入口，任务结果与板块盘中通知各用自己的记录。账本路径为 `run/state/agent/jobs.sqlite3`。

## 第一期 · 本地 MCP

候选包自带 MCP SDK 1.30.0 与 Stream SDK 0.24.3。源码环境可以安装 `requirements-agent.txt`。

将 `kimi_mcp.example.json` 中的 `stargazer` 条目合并到 Kimi Code 的 `mcp.json`，重新开启会话后用 `/mcp` 检查连接。Kimi 将 Python 作为子进程启动，走 stdio。MCP 子进程可独立于完整版网页运行。[Kimi Code MCP 配置](https://github.com/MoonshotAI/kimi-code/blob/main/docs/zh/customization/mcp.md) · [MCP Python SDK v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)

- 查询工具 `status`、`plan`、`positions`、`review`、`task_status`。
- 后台工具 `select_postclose`、`run_replay`、`backtest`、`mine_factors`、`import_data`、`fill_gaps`。
- 选股完成后按返回的 `run_id` 查看计划。回测则使用独立的历史回放 `run_id`，需要已有分钟数据。持仓和回测成交都来自模拟账本。
- 任务记录快照指纹、适用的规则版本及规则 SHA。回测另外绑定历史运行清单。后台启动时再次核对绑定。相同本地请求复用任务 ID，运行失败后可以重新提交。
- 运行长任务时保留 MCP 客户端会话。Windows 客户端退出时可回收进程树。工作进程退出后，账本将任务标为中断，结果可按任务 ID 查询。

用于演示的指令可以直接写成「查状态，按默认规则跑一次盘后选股并查任务，随后用它返回的运行 ID 展示冻结计划。再对同一规则执行一次有分钟数据的历史回放，读取回放的运行 ID，回测并汇报模拟成交。」

## 第二期 · 钉钉 Stream

在企业内部应用中添加机器人能力、订阅 Stream 消息并发布，再把机器人加入目标群。将 `dingtalk_stream.env.example` 复制到本机候选包的 `data/dingtalk_stream.env`，填入 AppKey、AppSecret、成员 `senderStaffId` 白名单和群 `conversationId` 白名单。多个 ID 用英文逗号分隔。双击 `start_dingtalk_stream.bat` 启动，现有盘中推送继续使用它自己的配置。[钉钉官方 Stream Python SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk-python)

- 群里 @ 机器人并发送「帮助」「状态」「选股」「计划」「持仓」「复盘」「回测」「回放 YYYYMMDD」「挖掘」「数据更新」「数据补缺 YYYYMMDD」或「任务 任务ID」。也可以用「回测 运行ID」指定一次完成的历史回放。
- 长任务在原群发受理回执，完成后用同一次会话的回执通道发摘要。原会话已过期或机器人重启时，可用新的「任务 任务ID」消息查持久化结果。
- 数据更新、联网补缺和推送测试要求同一人、同一群二次确认，确认码有效期 5 分钟。群回调按消息 ID 去重，群内只允许发起人用任务 ID 查其任务。
- 「推送测试」会向完整版已配置的机器人通道发消息。确认该通道适合本次群测后，可把 `STARGAZER_DINGTALK_ALLOW_PUSH_TEST` 配为 `1`。普通任务结果走原群会话，Stream 应用密钥保存在独立配置文件。

## 验收

在实际目录的隔离 Python 环境中运行 `tests/test_agent_integration.py`。`tests/smoke_agent_stdio.py` 从本机通达信和已归档的回放中切出真实小样本，使用官方 SDK 建立 stdio 会话，联调查状态、选股、查冻结计划、回放、复盘及回测。冒烟的任务账本、规则发布记录和运行结果写入 `_tmp/agent_stdio`。需要可用的当前选股日线和默认历史回放分钟数据。

钉钉凭证齐备后，先在白名单群验证「帮助」「选股」「任务 任务ID」和同群完成回执，再用另一成员或群验证权限。该阶段记录真实连接与回执。

2026-11-26 实测使用 49 只股票的通达信切片。选股冻结 31 只，历史回放产出 49 条复盘样本，按该回放运行 ID 回测得到 34 个信号、33 笔模拟成交。MCP 枚举 11 个工具，重复选股请求复用同一个任务 ID。钉钉调用层使用模拟回调及模拟传输验收，等待企业应用凭证完成实群联调。
