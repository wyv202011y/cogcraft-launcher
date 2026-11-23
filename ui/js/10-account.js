// 顶栏账号胶囊 + 全屏账号面板
// 引导第 3 步也能绑（大板块）；这里管「已经 onboard 之后」的查看 / 绑定 / 切换账号。
// 刻意不用小浮层：验证码要玩家复制粘贴，挤在小卡片里既难看清也难选中。
'use strict';

let acctCd = null, acctPoll = null;

function acctAuth(){ return (S && S.auth) || {}; }

function acctEl(tag, cls, text){
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function stopAcctTimers(){
  if (acctCd){ clearInterval(acctCd); acctCd = null; }
  if (acctPoll){ clearInterval(acctPoll); acctPoll = null; }
}

// ── 胶囊：圆形头像（首字母）+ 名字 ──
function renderAcctChip(){
  const chip = $('acctChip');
  if (!chip) return;
  const a = acctAuth();
  if (!a.logged_in){ chip.hidden = true; closeAcctPanel(); return; }
  const nm = String(a.name || '账号');
  chip.innerHTML = '';
  chip.appendChild(acctEl('span', 'av', (nm[0] || '?').toUpperCase()));
  chip.appendChild(acctEl('span', '', nm));
  chip.hidden = false;
}

function openAcctPanel(){
  const p = $('acctPanel');
  if (!p || !acctAuth().logged_in) return;
  p.classList.add('on');
  renderAcctPanel();
}

function closeAcctPanel(){
  const p = $('acctPanel');
  if (p) p.classList.remove('on');
  stopAcctTimers();
}

function bindDoneBlock(qqNum, onDone){
  const done = acctEl('div', 'binddone');
  done.appendChild(acctEl('div', 'binddone-ok', '✓'));
  done.appendChild(acctEl('div', 'binddone-t', '已绑定 QQ ' + qqNum));
  done.appendChild(acctEl('div', 'binddone-s', '以后服务器有通知会直接发到这里'));
  const back = acctEl('button', 'btn sub', '完成');
  back.style.marginTop = '18px';
  back.onclick = onDone;
  done.appendChild(back);
  return done;
}

// ── 面板主视图 ──
async function renderAcctPanel(){
  const body = $('acctBody');
  if (!body) return;
  const a = acctAuth();
  stopAcctTimers();
  const sub = $('acctHeadSub');
  if (sub) sub.textContent = a.name || '';

  let bind = { bound: false };
  try {
    const r = await api.auth_bind_status();
    if (r && typeof r === 'object') bind = r;
  } catch (e){}

  body.innerHTML = '';

  // 账号
  const info = acctEl('div', 'card');
  info.appendChild(acctEl('div', 'lab', '游戏 ID'));
  info.appendChild(acctEl('div', 'acct-id', a.name || ''));
  if (a.uuid) info.appendChild(acctEl('div', 'acct-uuid', a.uuid));
  const acts = acctEl('div', 'acct-actions');
  const sw = acctEl('button', 'btn sub', '切换账号');
  sw.onclick = async () => {
    closeAcctPanel();
    try { await api.auth_logout(); } catch (e){}
    try { render(await api.state()); } catch (e){}
    if (typeof window.__wiz === 'function') window.__wiz(1);   // 回引导的登录那一步
  };
  acts.appendChild(sw);
  info.appendChild(acts);
  body.appendChild(info);

  // QQ
  const qq = acctEl('div', 'card');
  qq.appendChild(acctEl('div', 'lab', 'QQ 绑定'));
  const row = acctEl('div', 'acct-bindrow');
  row.appendChild(acctEl('span', 'dot' + (bind.bound ? ' ok' : ''), ''));
  row.appendChild(acctEl('span', 't', bind.bound ? ('已绑定 ' + bind.qq_number) : '还没有绑定'));
  qq.appendChild(row);

  if (!bind.bound){
    const acts2 = acctEl('div', 'acct-actions');
    const b = acctEl('button', 'btn sub', '绑定 QQ 号');
    b.onclick = () => renderBindView(body);
    acts2.appendChild(b);
    qq.appendChild(acts2);
  }
  body.appendChild(qq);

  // 回主界面
  const close = acctEl('button', 'btn sub', '返回');
  close.style.marginTop = '6px';
  close.onclick = () => closeAcctPanel();
  body.appendChild(close);
}

// ── 面板里的验证码大页 ──
async function renderBindView(body){
  stopAcctTimers();
  body.innerHTML = '';
  body.appendChild(acctEl('div', 'bindwait', '正在获取验证码…'));

  let r = null;
  try { r = await api.auth_bind_code(); } catch (e){}

  if (!r || r.ok === false){
    body.innerHTML = '';
    body.appendChild(acctEl('div', 'binderr', (r && r.error) || '验证码没拿到，稍后再试。'));
    const back = acctEl('button', 'btn sub', '返回');
    back.onclick = () => renderAcctPanel();
    body.appendChild(back);
    return;
  }
  if (r.already_bound){ await renderAcctPanel(); return; }

  const line = '绑定' + String(r.code || '');
  body.innerHTML = '';

  const big = acctEl('div', 'bindbig');
  big.appendChild(acctEl('div', 'bindbig-lbl', '把这行整条发到 QQ 群'));
  big.appendChild(acctEl('div', 'bindbig-code', line));
  const cd = acctEl('div', 'bindbig-cd', '');
  cd.id = 'acctBigCd';
  big.appendChild(cd);
  body.appendChild(big);

  const ops = acctEl('div', 'bindops');
  const cp = acctEl('button', 'btn sub', '复制');
  cp.onclick = async () => {
    try { await api.copy_text(line); cp.textContent = '已复制'; }
    catch (e){ cp.textContent = '请手动复制'; }
    setTimeout(() => { if (cp.textContent !== '复制') cp.textContent = '复制'; }, 1600);
  };
  const ag = acctEl('button', 'btn sub', '换一个');
  ag.onclick = () => renderBindView(body);
  const bk = acctEl('button', 'btn sub', '返回');
  bk.onclick = () => renderAcctPanel();
  ops.appendChild(cp); ops.appendChild(ag); ops.appendChild(bk);
  body.appendChild(ops);

  startAcctCountdown(Number(r.expires_in || 300) || 300);
}

function startAcctCountdown(ttl){
  stopAcctTimers();
  const end = Date.now() + ttl * 1000;

  const tick = () => {
    const el = $('acctBigCd');
    if (!el) return;
    const left = Math.max(0, Math.round((end - Date.now()) / 1000));
    el.textContent = left > 0
      ? ('验证码 ' + Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0') + ' 内有效')
      : '这枚验证码过期了，点「换一个」';
    if (left <= 0) stopAcctTimers();
  };
  tick();
  acctCd = setInterval(tick, 1000);

  // 机器人不回话，成功与否全靠这里轮询出来
  let n = 0;
  acctPoll = setInterval(async () => {
    if (++n % 3 !== 0) return;
    let s = null;
    try { s = await api.auth_bind_status(); } catch (e){ return; }
    if (s && s.bound){
      stopAcctTimers();
      const body = $('acctBody');
      if (body){
        body.innerHTML = '';
        body.appendChild(bindDoneBlock(s.qq_number, () => renderAcctPanel()));
      }
    }
  }, 1000);
}

// ── 事件 ──
(function(){
  const wire = () => {
    const chip = $('acctChip');
    if (chip) chip.onclick = () => { openAcctPanel(); };
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
})();

// 预览用：?acct=1&panel=acct 自动开面板、&bind=1 直接停在验证码那屏
(function(){
  let q; try { q = new URLSearchParams(location.search); } catch (e){ return; }
  if (q.get('acct') !== '1') return;
  const want = q.get('bind') === '1' ? 'bind' : (q.get('panel') === 'acct' ? 'main' : null);
  if (!want) return;

  let tries = 0;
  const t = setInterval(async () => {
    if (++tries > 150){ clearInterval(t); return; }
    if (!S || !acctAuth().logged_in) return;
    clearInterval(t);
    openAcctPanel();
    if (want === 'bind'){
      const body = $('acctBody');
      if (body) await renderBindView(body);
    }
  }, 200);
})();
