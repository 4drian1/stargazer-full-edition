(() => {
  const root = document.getElementById('root');
  const pages = [['strategy', '策略规则']];
  let current = '';
  let main;
  const frame = document.createElement('iframe');
  frame.id = 'trial-module-frame';
  frame.title = '规则与复盘工作区';
  frame.style.cssText = 'display:none;border:0;width:100%;height:calc(100vh - 98px);';
  const reviewModes = document.createElement('div');
  reviewModes.id = 'trial-review-modes';
  reviewModes.hidden = true;
  reviewModes.innerHTML = '<button type="button" class="outline-btn" data-review-mode="">交易复盘</button><button type="button" class="outline-btn" data-review-mode="review">回放与因子研究</button>';
  reviewModes.addEventListener('click', event => {
    const button = event.target.closest('[data-review-mode]');
    if (button) show(button.dataset.reviewMode, root.querySelector('.main-nav'));
  });
  const overview = document.createElement('section');
  overview.id = 'trial-overview';
  overview.innerHTML = `
    <div class="trial-stats">
      ${['数据更新至', '选股日 P / 回放日 D', '最近运行', '选股模型', '数据来源'].map((name, i) => `
        <div class="panel trial-stat"><div class="muted">${name}</div><b data-value="${i}">读取中…</b><small data-note="${i}"></small></div>`).join('')}
    </div>
    <div class="trial-overview-tools">
      <section class="panel">
        <div class="section-title">模型开关</div>
        <div class="trial-controls">
          <label><input type="checkbox" id="trial-selection" disabled> Tarot 影子评分</label>
          <span class="muted">用于规则回放</span>
          <button class="outline-btn" id="trial-save-model" disabled>保存开关</button>
        </div>
      </section>
      <section class="panel">
        <div class="section-title">数据源与推送</div>
        <div class="trial-controls">
          <button class="outline-btn" data-settings="data">配置数据来源</button>
          <button class="outline-btn" data-settings="push">推送配置</button>
          <span id="trial-push-channel" class="muted"></span>
        </div>
      </section>
    </div>
    <div class="muted" role="status" id="trial-overview-status"></div>`;

  const dialog = document.createElement('dialog');
  dialog.id = 'trial-settings';
  dialog.setAttribute('aria-labelledby', 'trial-settings-title');
  dialog.innerHTML = '<div class="trial-dialog-head"><strong id="trial-settings-title"></strong><button class="outline-btn">关闭</button></div><iframe title="功能配置"></iframe>';
  dialog.querySelector('button').onclick = () => dialog.close();
  dialog.addEventListener('close', () => {
    dialog.querySelector('iframe').src = 'about:blank';
    if (overview.isConnected) loadOverview();
  });
  document.body.append(dialog);

  function dateText(value) {
    return String(value || '未同步').replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3');
  }

  async function loadOverview() {
    const status = overview.querySelector('#trial-overview-status');
    try {
      const response = await fetch('/api/trial/overview', {cache:'no-store'});
      const data = await response.json();
      if (!response.ok) throw Error(data.error || '完整版模块暂不可用');
      const {state, last, push} = data;
      const modelEnabled = state.model_switch.selection;
      const values = [dateText(state.data_through), dateText(state.p_day)+' / '+dateText(state.monitor_day),
                      last.run_id || '尚未运行', modelEnabled ? '已开启' : '已关闭',
                      state.source.source === 'stockdb' ? 'StockDB' : '本地通达信'];
      const lastNote = last.run_id ? `筛中 ${last.selected_n ?? '—'} · 冻结 ${last.frozen_n ?? '—'} · 触位 ${last.touches ?? '—'}` : '';
      const notes = ['规则快照', '历史回放窗口', lastNote, 'Tarot 影子评分', ''];
      values.forEach((value, i) => {
        overview.querySelector(`[data-value="${i}"]`).textContent = value;
        overview.querySelector(`[data-note="${i}"]`).textContent = notes[i];
      });
      const selection = overview.querySelector('#trial-selection');
      selection.checked = modelEnabled;
      selection.disabled = overview.querySelector('#trial-save-model').disabled = false;
      overview.querySelector('#trial-push-channel').textContent =
        ({none:'仅本地收件箱', dingtalk:'钉钉机器人', pushplus:'Pushplus'}[push.channel] || '渠道待核验');
      status.textContent = '';
    } catch (error) {
      status.textContent = error.message;
    }
  }

  overview.addEventListener('click', async event => {
    const settings = event.target.closest('[data-settings]')?.dataset.settings;
    if (settings) {
      dialog.dataset.settings = settings;
      dialog.querySelector('strong').textContent = settings === 'data' ? '数据来源与更新' : '推送配置';
      dialog.querySelector('iframe').src = trialRulesUrl + '/trial-module/' + settings;
      dialog.showModal();
    } else if (event.target.id === 'trial-save-model') {
      const status = overview.querySelector('#trial-overview-status');
      event.target.disabled = true;
      try {
        const response = await fetch('/api/trial/model-switch', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({selection:overview.querySelector('#trial-selection').checked})
        });
        if (!response.ok) throw Error('保存开关失败');
        await loadOverview();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        event.target.disabled = false;
      }
    }
  });

  function show(page, nav) {
    current = page;
    for (const tab of nav.querySelectorAll('[data-trial-page]')) {
      const selected = tab.dataset.trialPage === page;
      tab.classList.toggle('active', selected);
      tab.setAttribute('aria-pressed', String(selected));
    }
    nav.classList.toggle('trial-module-open', page === 'strategy');
    reviewModes.hidden = page === 'strategy' || !(main.querySelector('.review-layout'));
    for (const button of reviewModes.querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.reviewMode === page));
    }
    frame.style.display = page ? 'block' : 'none';
    main.hidden = Boolean(page);
    if (page) {
      const url = trialRulesUrl + '/trial-module/' + page;
      if (frame.src !== url) frame.src = url;
    }
  }

  function mount() {
    const nav = root.querySelector('.main-nav');
    const content = root.querySelector('main.main-content');
    if (!nav || !content) return;
    if (main !== content) {
      main = content;
      main.before(reviewModes);
      main.after(frame);
      main.hidden = Boolean(current);
    }
    const heading = main.querySelector('.page-heading');
    const title = heading?.querySelector('h1');
    const onOverview = title?.textContent.trim() === '总览';
    const onReview = title?.textContent.trim() === '复盘' || title?.textContent.trim() === '复盘研究';
    if (onReview && title.textContent !== '复盘研究') title.textContent = '复盘研究';
    reviewModes.hidden = !onReview || current === 'strategy';
    for (const button of reviewModes.querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.reviewMode === current));
    }
    for (const button of nav.querySelectorAll('button')) {
      if (button.textContent.trim() !== '复盘') continue;
      const label = [...button.childNodes].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim() === '复盘');
      if (label) label.textContent = '复盘研究';
    }
    if (onOverview && overview.previousElementSibling !== heading) {
      heading.after(overview);
      loadOverview();
    } else if (!onOverview && overview.isConnected) {
      overview.remove();
    }
    if (nav.querySelector('[data-trial-page]')) return;
    for (const [page, label] of pages) {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.dataset.trialPage = page;
      tab.textContent = label;
      tab.setAttribute('aria-pressed', 'false');
      tab.addEventListener('click', () => show(page, nav));
      nav.append(tab);
    }
    nav.addEventListener('click', event => {
      const tab = event.target.closest('button');
      if (tab && current && !tab.dataset.trialPage) show('', nav);
    }, true);
  }

  const style = document.createElement('style');
  style.textContent = `
    main[hidden]{display:none!important}
    .trial-module-open button.active:not([data-trial-page]){background:transparent;color:#9ba1a7;font-weight:normal}
    .topbar>.brand,.topbar>.top-status{flex:none;min-width:0}
    .topbar>.main-nav{min-width:0;overflow-x:auto}
    .topbar>.main-nav button{flex:0 0 auto;white-space:nowrap}
    .topbar>.main-nav svg{flex:none}
    .main-content>*{min-width:0;max-width:100%}
    .main-content .panel,.main-content .table-wrap,.main-content .side-stack{min-width:0;max-width:100%}
    .main-content .table-wrap{overflow-x:auto}
    .main-content td small,.main-content .cell-wrap{overflow-wrap:anywhere}
    .main-content .sg-corvus-grid{grid-template-columns:minmax(0,1fr)}
    .main-content .sg-assistant{grid-template-columns:minmax(160px,205px) minmax(0,1fr)}
    .main-content .sg-chat{min-width:0}
    .main-content .plan-layout:has(>.side-stack:empty){grid-template-columns:minmax(0,1fr)}
    .main-content .plan-layout>.side-stack:empty{display:none}
    #trial-review-modes:not([hidden]){max-width:1800px;margin:0 auto;display:flex;gap:8px;padding:14px 24px 0}
    #trial-review-modes button[aria-pressed=true]{color:var(--orange2);border-color:var(--orange)}
    .statusbar{overflow-x:auto;white-space:nowrap}
    #trial-overview{display:grid;gap:12px;margin-bottom:14px}
    .trial-stats,.trial-overview-tools{display:grid;gap:12px}
    .trial-stats{grid-template-columns:repeat(5,minmax(0,1fr))}
    .trial-overview-tools{grid-template-columns:repeat(2,minmax(0,1fr))}
    .trial-stat{padding:12px 14px;overflow-wrap:anywhere}
    .trial-stat b,.trial-stat small{display:block;margin-top:4px}
    .trial-stat small{color:var(--muted)}
    .trial-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:12px 14px}
    #trial-overview-status:empty{display:none}
    #trial-settings{padding:0;width:min(850px,calc(100vw - 24px));max-width:100%;background:var(--bg);color:inherit;border:1px solid #3b4045;border-radius:5px}
    #trial-settings::backdrop{background:#000a}
    #trial-settings iframe{display:block;width:100%;height:min(72vh,640px);border:0}
    #trial-settings[data-settings=data]{width:min(660px,calc(100vw - 24px))}
    #trial-settings[data-settings=data] iframe{height:min(208px,65vh)}
    .trial-dialog-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 12px;border-bottom:1px solid #3b4045}
    @media(max-width:1370px){
      .topbar{height:auto;min-height:58px;flex-wrap:wrap;gap:5px 12px;padding:7px 14px 3px}
      .topbar>.main-nav{order:3;width:100%;height:40px;scrollbar-width:thin}
      .topbar>.top-status{max-width:68%;overflow-x:auto}
    }
    @media(max-width:1050px){
      .holdings-layout,.watch-layout,.review-layout,.assistant-layout,.plan-layout,.plan-layout>.side-stack{grid-template-columns:minmax(0,1fr)}
      .assistant-right{grid-template-columns:minmax(0,1fr)}
      .trial-stats{grid-template-columns:repeat(3,minmax(0,1fr))}
    }
    @media(max-width:740px){
      .overview-grid,.trial-overview-tools{grid-template-columns:minmax(0,1fr)}
      .main-content .sg-assistant{grid-template-columns:minmax(0,1fr)}
      .trial-stats{grid-template-columns:repeat(2,minmax(0,1fr))}
    }`;
  document.head.append(style);
  new MutationObserver(mount).observe(root, {childList:true, subtree:true});
  mount();
})();
