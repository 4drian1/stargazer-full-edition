"""Price-only first touch and optional independently computed indicator facts."""
from datetime import datetime
import json
import sys

from full import vipdoc_reader as tdx  # exposes the project's existing readers
from rules_lab.paths import ROOT
from push_compact_v1 import strong_touch, indicator_lines
from index_map_layers_v1 import next_lower_band
from strong_sector_watch_v1 import candidate_events


def make_event(row, quote, observed, boards, source):
    # Older local pools already applied the ATR tolerance but omitted the metadata.
    bands = [dict(b,primary=bool(b.get('primary')),source_waves=int(b.get('source_waves') or 0),
                  tolerance_basis=b.get('tolerance_basis') or '0.2_PREVIOUS_DAILY_ATR') for b in row['positions']]
    event = candidate_events(dict(row,pool=row.get('pool') or 'MONITOR',positions=bands),
                             quote,int(observed.strftime('%Y%m%d')),int(quote['time']))
    if not event or event[0]['kind']!='STRONG_STOCK':
        return None
    band = event[0]['band']
    lo,hi = band['lower'],band['upper']
    following = next_lower_band(bands,min(lo,quote['price']))
    return dict(code=row['code'],name=row.get('name') or quote.get('name'),price=quote['price'],
                at=quote['time'],observed_at=observed.isoformat(timespec='seconds'),lower=lo,upper=hi,
                next_band={k:following[k] for k in ('lower','upper')} if following else None,
                priority='P1' if band['primary'] else 'P2',boards=boards,kind='STRONG_ZONE_TOUCH',source=source,
                quote=quote,delivery='PENDING',indicator_status='PENDING',attempts=0)


def first_card(event):
    indicator = event.get('indicator') or {}
    card = strong_touch(dict(code=event['code'],name=event['name'],board='、'.join(event['boards']),
                            touch_quote_at=event['at'],level_lower=event['lower'],level_upper=event['upper'],
                            price=event['price'],priority_label=event['priority'],recorded_touch=True,
                            next_lower_band=event['next_band'],period_pairs=indicator.get('periods')))
    return dict(title=f"Stargazer 完整版 · {event['code']} {card['title']}",body=card['body'],
                includes_indicators=bool(indicator.get('periods')))


def supplement_card(event):
    indicator = event['indicator']
    ratio = indicator.get('volume_ratio')
    lines = [f"【触价补充 · {event['code']}】",'首次触价时间: '+event['observed_at'],
             *indicator_lines({'period_pairs':indicator['periods']}),
             '量能比率: '+(f'{ratio:.2f}' if ratio is not None else '待核')]
    return dict(title=f"Stargazer 完整版 · 触价补充 · {event['code']}",body='\n'.join(lines))


def _valid_volume(row):
    v,a = row['raw_volume'],row['amount']
    return (v==a==0) or (v>0 and row['low']*.99<=a/v<=row['high']*1.01)


def _five(native):
    return [dict(day=r['day'],t=r['ts']%1000000,complete=True,nominal_end=r['ts'],volume=r['raw_volume'],
                 **{k:r[k] for k in ('open','high','low','close')}) for r in native]


def _live_five(minutes, quote, day, cutoff):
    from tdx_native_reader_v1 import grid_5min
    groups = {}
    grid = grid_5min(day)
    latest = minutes[-1]['date']
    for r in minutes:
        stamp = r['date']
        end = next(ts for ts in grid if ts>=stamp)
        groups.setdefault(end,[]).append(r)
    if any(end<cutoff and end>latest for end in grid):
        raise ValueError('LIVE_5M_PREFIX_NOT_READY')
    next_end = next((end for end in grid if end>=cutoff),None)
    if next_end and (next_end>cutoff or latest<cutoff):
        price = quote['price']
        groups.setdefault(next_end,[]).append(dict(date=cutoff,open=price,high=price,low=price,close=price,volume=0))
    return [dict(day=day,t=min(end,cutoff)%1000000,nominal_end=end,complete=end<=latest,
                 open=group[0]['open'],high=max(r['high'] for r in group),low=min(r['low'] for r in group),
                 close=group[-1]['close'],volume=sum(r['volume'] for r in group) if end<=latest else None)
            for end,group in groups.items()]


def compute_indicators(event):
    from tdx_native_reader_v1 import parse_lc5, validate_day, grid_5min
    from strong_low_indicators_v1 import pair
    directory = str(ROOT.parents[1]/'stargazer_ml/v5_hybrid/green_common_profile')
    if directory not in sys.path:
        sys.path.append(directory)
    from live_five_preview_v1 import fifteen
    from public_stock_minutes_v1 import fetch,parse

    cutoff = int(event['at'])
    day = cutoff//1000000
    days = [d for d in tdx.calendar_days() if d<day][-20:]
    if not days:
        raise ValueError('CALENDAR_UNAVAILABLE')
    previous = days[-1]
    code = event['code']
    venue = 'sh' if code.startswith(('60','68')) else 'sz'
    file = tdx.vipdoc_root()/venue/'fzline'/(venue+code+'.lc5')
    grouped = {}
    for bar in parse_lc5(file.read_bytes()):
        if bar['day'] in days:
            grouped.setdefault(bar['day'],[]).append(bar)
    verified = {d:rows for d,rows in grouped.items() if validate_day(rows,d)['eligible']}
    if previous not in verified or abs(verified[previous][-1]['close']-event['quote']['prev'])>.011:
        raise ValueError('PREVIOUS_DAY_5M_UNVERIFIED')
    history = _five([b for d in days[-5:] for b in verified.get(d,[])])
    raw,_url = fetch(code)
    raw_rows = json.loads(raw)
    if not isinstance(raw_rows,list):
        raise ValueError('PUBLIC_STOCK_SCHEMA')
    stamps = [int(datetime.strptime(b['day'],'%Y-%m-%d %H:%M:%S').strftime('%Y%m%d%H%M%S')) for b in raw_rows]
    latest = max((s for s in stamps if s//1000000==day and s<=cutoff),default=0)
    if not latest or (datetime.strptime(str(cutoff),'%Y%m%d%H%M%S')-datetime.strptime(str(latest),'%Y%m%d%H%M%S')).total_seconds()>120:
        raise ValueError('LIVE_MINUTE_PREFIX_NOT_READY')
    current = parse(raw,code,previous,latest,event['quote']['prev'])
    bars = history+_live_five(current,event['quote'],day,cutoff)
    periods = {}
    for period, values in [(5,bars),(15,fifteen(bars))]:
        value = pair(values,previous,day,period)
        if value.get('status')=='KNOWN':
            value.update(provisional=any(b['day']==day and not b.get('complete',True) for b in values))
            periods[str(period)] = value
    if not periods:
        raise ValueError('INDICATORS_NOT_READY')
    ratio = None
    slots = [ts for ts in grid_5min(day) if ts<=latest]
    if len(verified)==20 and slots:
        upto = len(slots)
        samples = [r for d in days for r in verified[d][:upto]]
        if all(_valid_volume(r) for r in samples):
            mean = sum(r['raw_volume'] for r in samples)/20
            if mean>0:
                ratio = sum(r['volume'] for r in current if r['date']<=slots[-1])/mean
    return {'periods':periods,'volume_ratio':ratio,'cutoff':cutoff,'minute_last':latest}
