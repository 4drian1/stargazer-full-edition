"""挖掘验收：去重/防前视(purge)/候选门槛 三件套。"""
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))


def main():
    from full.mining import dedup, learn_directions, mine
    from full.factor_lib import _spearman
    # 1) 去重：构造高相关因子对，弱的应被剔除
    panel = {1: {k: {'x': float(i + 1), 'y': float(2 * (i + 1))}
                 for i, k in enumerate('abcdefgh')}}
    labels = {1: {'a': .1, 'b': .2, 'c': -.3, 'd': .05, 'e': .15, 'f': -.1, 'g': .2, 'h': -.05}}
    dirs = {'x': 1, 'y': 1}
    keep = dedup(panel, labels, [1], ['x', 'y'], dirs, thresh=0.7)
    assert len(keep) == 1, keep
    print('dedup OK: corr pair collapsed to', keep)

    # 2) 全流水线：purge 防前视 + 门槛拦截
    r = mine('strong_pullback@v1', factor_names=['mom_5d', 'mom_20d', 'macd_hist_d',
                                                 'volratio_5_20', 'drawdown_20h'],
             p_days=[20261111, 20261112, 20261113, 20261116, 20261117], purge=1)
    assert r['ok'], r
    for f in r['folds']:
        if f.get('skipped'):
            continue
        td = f['test_day']
        for tr in f['train_days']:
            assert tr < td, '前视：训练日晚于测试日'
        # purge=1：测试日前一交易日不得入训练
        lab = r['labeled_days']
        idx = lab.index(td)
        if idx >= 1:
            assert lab[idx - 1] not in f['train_days'], 'purge 被违反'
    print('walk-forward purge OK')
    for c in r['candidates']:
        if c['folds_n'] < 2:
            assert not c['promotable'], '门槛失效：1 折不应晋级'
    print('promote gates OK (folds<2 blocked):', [(c['factors'], c['folds_n']) for c in r['candidates']])
    print('ROUND12_MINING_PASS')


if __name__ == '__main__':
    main()
