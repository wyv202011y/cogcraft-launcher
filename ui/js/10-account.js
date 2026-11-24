// 顶栏账号胶囊 + 全屏账号面板
// 引导第 3 步也能绑（大板块）；这里管「已经 onboard 之后」的查看 / 绑定 / 切换账号。
// 刻意不用小浮层：验证码要玩家复制粘贴，挤在小卡片里既难看清也难选中。
'use strict';

// ══════════════════════════════════════════════════════════════
// QQ 绑定 —— 唯一实现。引导第 3 步（#bindArea）与账号面板（#acctBody）共用。
// 以前 08-wizard.js 和这里各写了一份几乎一样的流程，改一处漏一处，2026-11-24 合并。
//
// 兜底（计划 §5.6）：机器人不在线就不发码、直接给工单；120 秒没动静出
// 「重新获取」；再来一轮还不成同时给「去提交工单」。工单地址由服务端下发。
// ══════════════════════════════════════════════════════════════
let bindCdTimer = null, bindPollTimer = null, bindRetryTimer = null;
let bindSkipCount = 0;        // 已经「重新获取」过几次：决定要不要出工单入口
const BIND_RETRY_SECONDS = 120;   // 等这么久还没动静才提示重新获取，太早提示等于催玩家

function bindStopTimers(){
  if (bindCdTimer){ clearInterval(bindCdTimer); bindCdTimer = null; }
  if (bindPollTimer){ clearInterval(bindPollTimer); bindPollTimer = null; }
  if (bindRetryTimer){ clearTimeout(bindRetryTimer); bindRetryTimer = null; }
}

function bindEsc(v){
  return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
    return ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' })[c];
  });
}

function bindBtn(parent, text, onclick){
  const b = acctEl('button', 'btn sub', text);
  b.onclick = () => onclick(b);
  parent.appendChild(b);
  return b;
}

// 工单入口：地址由服务端下发（已带 gameid）。拿不到就不出按钮 ——
// 客户端里绝不留第二个兜底 URL，否则又是两套来源。
function bindTicketBtn(parent, url){
  if (!url) return null;
  return bindBtn(parent, '去提交工单', async () => {
    try { await api.open_url(url); } catch (e){}
  });
}

function bindDone(box, qq, opts){
  box.innerHTML =
    '<div class="binddone">' +
      '<div class="binddone-ok">✓</div>' +
      '<div class="binddone-t">已绑定 QQ ' + bindEsc(qq) + '</div>' +
      '<div class="binddone-s">以后服务器有通知会直接发到这里</div>' +
    '</div>';
  if (opts && opts.onDone){
    const ops = acctEl('div', 'bindops');
    bindBtn(ops, '完成', () => opts.onDone());
    box.appendChild(ops);
  }
}

// ── 唯一实现：引导第 3 步与账号面板都走这里 ──
// opts: {onBack, onDone, onSkip, backText}
async function bindStart(box, opts){
  opts = opts || {};
  bindStopTimers();
  if (!box) return;
  box.innerHTML = '<div class="bindwait">正在获取验证码…</div>';

  let st = null;
  try { st = await api.auth_bind_status(); } catch (e){}
  if (st && st.bound) { bindDone(box, st.qq_number, opts); return; }

  let r = null;
  try { r = await api.auth_bind_code(); } catch (e){}

  if (!r || r.ok === false){
    box.innerHTML = '<div class="binderr">' +
      bindEsc((r && r.error) || '验证码没拿到，稍后再试。') + '</div>';
    const ops = acctEl('div', 'bindops');
    box.appendChild(ops);
    bindBtn(ops, '重试', () => bindStart(box, opts));
    bindTicketBtn(ops, r && r.ticket_url);
    if (opts.onBack) bindBtn(ops, opts.backText || '返回', () => opts.onBack());
    return;
  }
  if (r.already_bound) { bindDone(box, r.qq_number, opts); return; }

  // 机器人不在线就根本没码可发：告诉他走工单，别让他拿着一枚没人认的码干等
  if (r.robot_online === false){
    box.innerHTML = '<div class="binderr">QQ 机器人这会儿不在线，验证码发出去也没人处理。<br>' +
      '可以直接提交工单，我们看到会手动给你绑上。</div>';
    const ops = acctEl('div', 'bindops');
    box.appendChild(ops);
    bindTicketBtn(ops, r.ticket_url);
    bindBtn(ops, '再试一次', () => bindStart(box, opts));
    if (opts.onBack) bindBtn(ops, opts.backText || '返回', () => opts.onBack());
    return;
  }

  bindShowCode(box, r, opts);
}

function bindShowCode(box, r, opts){
  const line = '绑定' + String(r.code || '');
  box.innerHTML =
    '<div class="bindbig">' +
      '<div class="bindbig-lbl">把这行整条发到 QQ 群</div>' +
      '<div class="bindbig-code">' + bindEsc(line) + '</div>' +
      '<div class="bindbig-cd" id="bindBigCd"></div>' +
    '</div>' +
    '<div class="bindops" id="bindOps"></div>' +
    '<div class="bindops" id="bindOpsLater" style="display:none"></div>' +
    (opts.onSkip ? '<div class="bindskip"><a id="bindSkip">先不绑，直接下一步</a></div>' : '');

  const ops = $('bindOps');
  bindBtn(ops, '复制', async (b) => {
    try { await api.copy_text(line); b.textContent = '已复制'; }
    catch (e){ b.textContent = '请手动复制'; }
    setTimeout(() => { b.textContent = '复制'; }, 1600);
  });
  bindBtn(ops, '换一个', () => bindStart(box, opts));
  if (opts.onBack) bindBtn(ops, opts.backText || '返回', () => opts.onBack());
  if (opts.onSkip) $('bindSkip').onclick = () => opts.onSkip();

  bindRetryTimer = setTimeout(() => {
    const later = $('bindOpsLater');
    if (!later) return;
    later.style.display = 'flex';
    bindBtn(later, '重新获取验证码', () => {
      bindSkipCount++;
      bindStart(box, opts);
    });
    // 第二次还没成，就别再让他反复试了，给工单
    if (bindSkipCount > 0) bindTicketBtn(later, r.ticket_url);
  }, BIND_RETRY_SECONDS * 1000);

  bindCountdown(box, opts, Number(r.expires_in || 300) || 300);
}

function bindCountdown(box, opts, ttl){
  const end = Date.now() + ttl * 1000;
  const tick = () => {
    const el = $('bindBigCd');
    if (!el){ bindStopTimers(); return; }
    const left = Math.max(0, Math.round((end - Date.now()) / 1000));
    el.textContent = left > 0
      ? ('验证码 ' + Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0') + ' 内有效')
      : '这枚验证码过期了，点「重新获取」';
    if (left <= 0 && bindCdTimer){ clearInterval(bindCdTimer); bindCdTimer = null; }
  };
  tick();
  bindCdTimer = setInterval(tick, 1000);

  // 机器人不回复，成功与否全靠这里轮询出来
  let n = 0;
  bindPollTimer = setInterval(async () => {
    if (++n % 3 !== 0) return;
    let s = null;
    try { s = await api.auth_bind_status(); } catch (e){ return; }
    if (s && s.bound){
      bindStopTimers();
      bindDone(box, s.qq_number, opts);
    }
  }, 1000);
}

function bindResetSession(){
  bindSkipCount = 0;
  bindStopTimers();
}


function acctAuth(){ return (S && S.auth) || {}; }

function acctEl(tag, cls, text){
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
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
  bindStopTimers();
}

// ── 面板主视图 ──
async function renderAcctPanel(){
  const body = $('acctBody');
  if (!body) return;
  const a = acctAuth();
  bindStopTimers();
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

// ── 面板里的绑定页：流程在 bindStart()（上面），这里只提供容器与去向 ──
async function renderBindView(body){
  bindSkipCount = 0;
  await bindStart(body, {
    onBack: () => renderAcctPanel(),
    onDone: () => renderAcctPanel(),
    backText: '返回',
  });
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
