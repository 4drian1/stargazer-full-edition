"""Real SDK stdio through existing engines on an isolated, small TDX replay slice."""
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from full.build_snapshot import MINUTE_COLUMNS,_gz_write
from full.vipdoc_reader import calendar_days,minute_bars
from full.mcp_tools import status
from rules_lab.paths import ROOT,SNAPSHOT,STATE,sha256_file


def fixture(folder):
    current = status()
    day = current['replay_day']
    snapshot = folder/'snapshot'
    snapshot.mkdir(parents=True)
    source = json.loads(gzip.decompress((SNAPSHOT/'daily.json.gz').read_bytes()))
    historical = next((f for f in (STATE/'runs').glob(f'*/events/{day}.json')),None)
    if not historical:
        raise ValueError('Generate one historical replay before using this real-data smoke test')
    rows = json.loads(historical.read_bytes())['events']
    selected = list(dict.fromkeys(r['code'] for r in rows.values()))[:35]
    plan = next((f for f in (STATE/'plans').glob(f'*/freeze/{current["observation_day"]}.json')),None)
    if plan:
        selected.extend(c['code'] for c in json.loads(plan.read_bytes())['candidates'][:20])
    observed = {}
    daily = {}
    for code in dict.fromkeys(selected):
        stem = ('sh' if code.startswith(('60','68')) else 'sz')+code
        bars = minute_bars(stem,day)
        if bars and code in source['codes']:
            daily[code] = source['codes'][code]
            observed[code] = [[b[k] for k in MINUTE_COLUMNS] for b in bars]
    assert len(observed)>=20 and current['daily_through']>=current['selection_data_day'], current
    _gz_write(snapshot/'daily.json.gz',dict(source,codes=daily))
    _gz_write(snapshot/'minutes.json.gz',dict(columns=MINUTE_COLUMNS,days={str(day):observed}))
    for name in ('calendar.json','boards.json','reference_pool.json','corporate_events.json.gz'):
        shutil.copyfile(SNAPSHOT/name,snapshot/name)
    calendar = json.loads((snapshot/'calendar.json').read_bytes())
    calendar['days'] = list(map(str,calendar_days()))
    (snapshot/'calendar.json').write_text(json.dumps(calendar),encoding='utf-8')
    man = dict(contract='RULES_LAB_SNAPSHOT_MANIFEST_V1',source='agent-smoke TDX subset',
               p_day=json.loads((SNAPSHOT/'snapshot_manifest.json').read_bytes())['p_day'],
               monitor_day=day,minute_days=1,daily_through=current['daily_through'],
               data_sha256={p.name:sha256_file(p) for p in snapshot.iterdir()})
    man['snapshot_fingerprint'] = hashlib.sha256(json.dumps(man,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    (snapshot/'snapshot_manifest.json').write_text(json.dumps(man),encoding='utf-8')
    return snapshot,len(observed)


async def main():
    folder = ROOT/'_tmp'/'agent_stdio'/uuid4().hex
    snap, universe = fixture(folder)
    env = dict(os.environ,RULES_LAB_SNAPSHOT_DIR=str(snap),RULES_LAB_STATE_DIR=str(folder/'state'),
               RULES_LAB_RUN_ROOT=str(folder/'state'/'runs'),STARGAZER_AGENT_LEDGER=str(folder/'jobs.sqlite3'))
    env.pop('RULES_LAB_RUN_ID_OVERRIDE',None)
    params = StdioServerParameters(command=sys.executable,args=['-B','-m','full.mcp_server'],env=env,cwd=ROOT)
    async with stdio_client(params) as (r,w), ClientSession(r,w) as session:
        await session.initialize()
        schemas = (await session.list_tools()).tools
        assert len(schemas)==11 and all(t.inputSchema.get('type')=='object' for t in schemas)

        async def invoke(name, args=None):
            result = await session.call_tool(name,args or {})
            assert not result.isError,(name,result.content)
            return result.structuredContent

        async def completed(task, expected='SUCCEEDED'):
            assert task['ok'],task
            for _ in range(180):
                r = await invoke('task_status',{'task_id':task['task_id']})
                if r['status'] in ('FAILED','INTERRUPTED','SUCCEEDED'):
                    assert r['status']==expected,r
                    return r['result']
                await asyncio.sleep(.5)
            raise RuntimeError('task did not finish: '+task['task_id'])

        state = await invoke('status')
        task = await invoke('select_postclose')
        duplicate = await invoke('select_postclose')
        assert duplicate['task_id']==task['task_id'] and duplicate['reused']
        chosen = await completed(task)
        plan = await invoke('plan',{'run_id':chosen['run_id']})
        assert plan['ok'] and plan['count']==chosen['count'] and plan['count']>0,plan
        replay = await completed(await invoke('run_replay',{'days':[state['replay_day']]}))
        review = await invoke('review',{'run_id':replay['run_id']})
        assert review['ok'] and review['total']>0,review
        backtest = await completed(await invoke('backtest',{'run_id':replay['run_id']}))
        assert backtest['run_id']==replay['run_id'] and backtest['summary']['signals']>0,backtest
        absent = await completed(await invoke('run_replay',{'days':[state['observation_day']]}),'FAILED')
        assert 'MISSING_REPLAY_MINUTES' in absent['error'],absent
        summary = dict(snapshot=state['snapshot'],sample_stocks=universe,plan_count=plan['count'],
                       replay_day=state['replay_day'],replay_run=replay['run_id'],review_samples=review['total'],
                       backtest_signals=backtest['summary']['signals'],filled=backtest['summary']['filled'],
                       tool_schemas=len(schemas),dedup=True,missing_session_rejected=True)
        (folder/'acceptance.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        print('MCP_STDIO_PASS',json.dumps(summary),folder/'acceptance.json')


if __name__ == '__main__':
    asyncio.run(main())
