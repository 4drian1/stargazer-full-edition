"""Local stdio server for the Full Edition tools (MCP Python SDK v1)."""
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from full import mcp_tools as tools

server = FastMCP('stargazer',instructions=(
    '查询使用同步工具。选股、回放、回测与数据任务使用 task_status 查询最终状态。'
    '盘后选股生成规则计划；历史回测需要已完成的历史回放 run_id。持仓是模拟结果。'))
READ = ToolAnnotations(readOnlyHint=True,openWorldHint=False)
LOCAL = ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False)
NETWORK = ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=True)
RunId = Annotated[str,Field(pattern='^[a-f0-9]{16}$',description='已有本地运行的 16 位 ID')]
TaskId = Annotated[str,Field(pattern='^[a-f0-9]{32}$',description='受理回执中的 32 位任务 ID')]
Days = Annotated[list[int],Field(min_length=1,max_length=20,description='YYYYMMDD 格式的交易日')]
Rule = Annotated[str,Field(description='已发布规则名或 rule@version；省略使用完整版默认规则')]
Result = dict[str,Any]


@server.tool(annotations=READ)
def status() -> Result:
    """查看行情快照指纹、盘后选股日期及默认历史回放日期。"""
    return tools.status()


@server.tool(annotations=LOCAL)
def select_postclose(rule: Rule | None = None) -> Result:
    """受理盘后规则选股与计划冻结，返回任务 ID。"""
    return tools.submit('select',{'rule':rule})


@server.tool(annotations=READ)
def plan(run_id: RunId | None = None) -> Result:
    """读取冻结规则计划。传选股回执的 run_id 可精确查询。"""
    return tools.plan(run_id)


@server.tool(annotations=READ)
def positions() -> Result:
    """查询最近回放的模拟持仓。"""
    return tools.positions()


@server.tool(annotations=READ)
def review(run_id: RunId | None = None) -> Result:
    """查询已完成历史回放的复盘摘要与样本。"""
    return tools.review(run_id)


@server.tool(annotations=LOCAL)
def run_replay(days: Days | None = None, rule: Rule | None = None,
               with_tarot_shadow: Annotated[bool,Field(description='是否追加 Tarot 影子评分')] = False) -> Result:
    """受理历史策略回放，返回任务 ID。完成后取 run_id 回测。"""
    return tools.submit('replay',dict(days=days,rule=rule,with_tarot_shadow=with_tarot_shadow))


@server.tool(annotations=LOCAL)
def backtest(run_id: RunId) -> Result:
    """对指定已完成的历史回放受理回测，返回任务 ID。"""
    return tools.submit('backtest',dict(run_id=run_id))


@server.tool(annotations=LOCAL)
def mine_factors(days: Days | None = None, rule: Rule | None = None,
                 purge: Annotated[int,Field(ge=1,le=20,description='训练窗与测试日之间的交易日间隔')] = 1) -> Result:
    """受理隔离的因子挖掘，返回任务 ID。"""
    return tools.submit('mine',dict(days=days,rule=rule,purge=purge))


@server.tool(annotations=LOCAL)
def import_data(source: Annotated[Literal['configured_tdx','import_dir'],
                                 Field(description='已配置的通达信目录或完整版固定导入目录')] = 'configured_tdx',
                include_minutes: Annotated[bool,Field(description='是否一同导入五分钟历史供回放')] = False) -> Result:
    """从已配置通达信或固定导入目录导入数据，默认同步日线，返回任务 ID。"""
    return tools.submit('import_data',dict(source=source,include_minutes=include_minutes))


@server.tool(annotations=NETWORK)
def fill_gaps(days: Days) -> Result:
    """受理指定交易日行情联网补缺，返回任务 ID。"""
    return tools.submit('fill_gaps',dict(days=days))


@server.tool(annotations=READ)
def task_status(task_id: TaskId) -> Result:
    """读取任务状态、绑定输入与完成结果。"""
    return tools.task_status(task_id)


if __name__ == '__main__':
    server.run(transport='stdio')
