// 首次引导向导（昵称/硬件检测/选版本/加群/就绪）
// 拆自原 ui/index.html 第 973-1227 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

const WNAME = ['欢迎', '登录账号', '绑定 QQ', '硬件检测', '选择版本', '加入交流群', '准备就绪'];
let authMode = 'login';   // 'login' | 'register'，登录步内部切换用
// 群信息：与官网导航栏同源（463960828），改群时这里和站上一起改
const QQ_GROUP = '463960828';
const QQ_URL = 'https://qm.qq.com/q/xqjGbFAQIU';
const LAST = WNAME.length - 1;
// 服务器用这个 ID 认人，所以规则跟 Minecraft 用户名一致，额外允许中文
const NAME_RE = /^[\u4e00-\u9fa5A-Za-z0-9_]{2,16}$/;
let wStep = 0;

function openWizard(){
  wStep = 0;
  // 预览用：?wstep=N 直接停在某一步、?auth=register 预置注册态
  // （和 README 里 ?onboard=1 / ?panel=1 同一套做法，只影响界面预览）
  try{
    const q = new URLSearchParams(location.search);
    const ws = parseInt(q.get('wstep') || '0', 10);
    if (!isNaN(ws) && ws >= 0 && ws <= LAST) wStep = ws;
    if (q.get('auth') === 'register') authMode = 'register';
  } catch (e){}
  $('wizard').classList.add('on');
  renderWizard();
}
function closeWizard(){ $('wizard').classList.remove('on'); }

function renderWizard(){
  const st = S || {};
  const mem = st.memory || {};
  const pend = st.pending || {};
  const variants = st.variants || [];
  const cur = st.variant || (variants[0] || {}).id;
  const server = st.server || {};
  const authState = st.auth || {};

  $('wStepName').textContent = WNAME[wStep];
  const dotsBox = $('wDots');
  if (dotsBox.children.length !== WNAME.length){
    dotsBox.innerHTML = WNAME.map(function(){ return '<i></i>'; }).join('');
  }
  const dots = dotsBox.children;
  for (let i = 0; i < dots.length; i++){
    dots[i].className = i < wStep ? 'done' : (i === wStep ? 'on' : '');
  }
  $('wBack').style.visibility = wStep === 0 ? 'hidden' : 'visible';
  $('wNext').disabled = false;               // 昵称那步会按校验结果重新禁掉
  $('wNext').textContent = wStep === 0 ? '开始配置'
                         : (wStep === 1
                              ? (authState.logged_in ? '继续'
                                 : (authMode === 'register' ? '注册并继续' : '登录并继续'))
                              : (wStep === LAST ? '开始安装' : '下一步'));

  const body = $('wBody');
  body.className = 'wbody' + (wStep === 0 ? '' : ' lft');

  if (wStep === 0){
    body.innerHTML =
      '<div class="mark" style="width:74px;height:74px;border-radius:19px">' +
        '<span style="font-size:28px">cc</span></div>' +
      '<div class="wtitle" style="margin-top:22px">欢迎来到 cogcraft</div>' +
      '<div class="wsub">这是像素工坊的专属客户端，只需要点几次，' +
        '剩下的下载和配置它自己会做完。<br>' +
        '不需要你装 Java、不需要你去找整合包、也不需要挑版本。</div>' +
      '<div class="wsub" style="margin-top:16px;color:#4a5260;font-size:11.5px">' +
        '整个过程约 10~15 分钟，取决于你的网速。中途关掉也没关系，' +
        '下次打开会接着下。</div>';
  }

  if (wStep === 1){
    // 账号放在昵称原来的位置：服务端开 online-mode 后，叫什么名字由账号决定，
    // 不再让玩家单独填。老玩家用原来的游戏名注册，UUID 不变，存档和背包都还在。
    if (authState.logged_in){
      body.innerHTML =
        '<div class="wsec">已经登录了</div>' +
        '<div class="acct-card">' +
          '<span class="av">' + esc((authState.player_name || '?').charAt(0).toUpperCase()) + '</span>' +
          '<div class="acct-meta">' +
            '<div class="acct-name">' + esc(authState.player_name || '') + '</div>' +
            '<div class="acct-sub">cogcraft 账号</div>' +
          '</div>' +
        '</div>' +
        '<div class="wsub" style="margin-top:18px">进游戏后用的就是这个 ID。' +
          '<br><span style="color:#4a5260">要换人点下面的「切换账号」，换账号要重新输密码。</span></div>';
      $('wNext').textContent = '继续';
    } else {
      const isReg = authMode === 'register';
      body.innerHTML =
        '<div class="wsec">' + (isReg ? '创建你的账号' : '先登录你的账号') + '</div>' +
        (isReg
          ? '<div class="wsub" style="margin-top:2px">这个游戏 ID 会一直跟着你，进服后大家看到的就是它。' +
            '<br><span style="color:#4a5260">以前玩过的老玩家，用原来的游戏名注册，存档和背包都还在。</span></div>'
          : '<div class="wsub" style="margin-top:2px">用你在服务器上的游戏 ID 和密码登录。' +
            '<br><span style="color:#4a5260">第一次来？点下面的「注册一个」。</span></div>') +
        '<div class="wform">' +
          '<input id="wAuthName" maxlength="16" spellcheck="false" autocomplete="off" placeholder="游戏 ID">' +
          '<input id="wAuthPwd" type="password" maxlength="64" placeholder="密码">' +
          (isReg ? '<input id="wAuthPwd2" type="password" maxlength="64" placeholder="再输一次密码">' : '') +
        '</div>' +
        '<div class="wform-err" id="wAuthErr"></div>' +
        '<a class="wlink" id="wAuthSwitch">' + (isReg ? '已有账号？去登录' : '没有账号？注册一个') + '</a>';
      bindAuthStep();
    }
  }

  if (wStep === 2){
    body.innerHTML =
      '<div class="wsec">绑定你的 QQ</div>' +
      '<div class="wsub">绑定之后，服务器通知、活动、处理问题都能直接找到你。<br>' +
        '<span style="color:#4a5260">不绑也能继续用，以后在设置里随时补。</span></div>' +
      '<div class="bindarea" id="bindArea"><div class="bindwait">正在获取验证码…</div></div>';
    fillBindArea();
  }

  if (wStep === 3){
    const hw = st.hardware || {};
    const cpu = hw.cpu || {}, ram = hw.ram || {};
    const gpu = (hw.gpu || [])[0] || {};
    const disk = hw.disk || {}, java = hw.java || {};
    const reason = st.variant_reason || '硬件满足推荐要求';
    body.innerHTML =
      '<div class="wsec">你的电脑</div>' +
      '<div class="hwbox">' +
        '<div class="hwrow"><span class="k">处理器</span><span class="v">' +
          (cpu.name || '未识别') + (cpu.threads ? '（' + cpu.threads + ' 线程）' : '') +
        '</span></div>' +
        '<div class="hwrow"><span class="k">内存</span><span class="v">' +
          Math.round((ram.total_mb || 0) / 1024) + ' GB' +
          (ram.avail_mb ? '，可用 ' + Math.round(ram.avail_mb / 1024) + ' GB' : '') +
        '</span></div>' +
        '<div class="hwrow"><span class="k">显卡</span><span class="v">' +
          (gpu.name || '未识别独立显卡') +
          (gpu.vram_mb ? '（' + gpu.vram_mb + ' MB 显存）' : '') + '</span></div>' +
        '<div class="hwrow"><span class="k">磁盘</span><span class="v">' +
          (disk.type || '未知') +
          (disk.free_mb ? '，剩余 ' + Math.round(disk.free_mb / 1024) + ' GB' : '') +
        '</span></div>' +
        '<div class="hwrow"><span class="k">Java</span><span class="v">' +
          (java.major ? java.major + '（' + java.bits + ' 位）' : '未安装 · 启动器自带，无需你操心') +
        '</span></div>' +
      '</div>' +
      benchBlock(st.bench) +
      '<div class="wsub" style="margin-top:18px">根据这些信息，我们建议你用 <b>' +
        ((variants.find(v => v.id === st.variant_auto) || {}).name || '高配版') +
        '</b>。<br><span style="color:#4a5260">' + reason + '</span></div>';
  }

  if (wStep === 4){
    body.innerHTML =
      '<div class="wsec">选一个版本</div>' +
      '<div class="vcards">' + variants.map(v => {
        const auto = v.id === st.variant_auto;
        return '<div class="vcard' + (v.id === cur ? ' on' : '') + '" data-v="' + v.id + '">' +
          '<div class="vt">' + v.name + (auto ? '<em>推荐</em>' : '') + '</div>' +
          '<div class="vd">' + (v.desc || '') + '</div>' +
          '<div class="vm">' + v.mods_count + ' 个模组 · 至少 ' + v.min_ram_mb + ' MB 内存</div>' +
          '</div>';
      }).join('') + '</div>' +
      '<div class="wsub" style="margin-top:20px;font-size:11.5px;color:#4a5260">' +
        '两个版本用的是同一个客户端，随时可以在设置里切换，存档通用。</div>';
    [].forEach.call(body.querySelectorAll('.vcard'), c => {
      c.onclick = async () => {
        const st2 = await api.set_variant(c.dataset.v);
        render(st2);
        renderWizard();
      };
    });
  }

  if (wStep === 5){
    body.innerHTML =
      '<div class="wsec">装之前先进个群</div>' +
      '<div class="qqbox">' +
        '<div class="qqtop"><span class="qqq">QQ</span>' +
          '<div><div class="qqn">像素工坊 · 玩家交流群</div>' +
          '<div class="qqd">开服公告、版本更新、崩溃求助都在这里</div></div></div>' +
        '<div class="qqnum">' + QQ_GROUP + '</div>' +
        '<div class="qqbtn">' +
          '<button class="btn" id="qqCopy">复制群号</button>' +
          '<button class="btn primary" id="qqOpen">打开加群页</button>' +
        '</div>' +
        '<div class="qqhint" id="qqHint"></div>' +
      '</div>' +
      '<div class="wsub" style="margin-top:16px">' +
        '装好之后如果崩了、卡了、进不去服，在群里说一声比一个人瞎试快得多。<br>' +
        '<span style="color:#4a5260">不着急也可以先跳过，装完再进群。</span></div>';
    bindQQStep();
  }

  if (wStep === 6){
    const v = variants.find(x => x.id === cur) || {};
    const memMb = mem.xmx_mb || '—';
    body.innerHTML =
      '<div class="wsec">确认一下</div>' +
      '<div class="wsum">' +
        '<div class="rw"><span class="k">游戏昵称</span><span class="v">' +
          esc((st.settings || {}).player_name || '—') + '</span></div>' +
        '<div class="rw"><span class="k">服务器</span><span class="v">' +
          (server.name || '像素工坊') + ' <em>' + (server.address || '') + '</em></span></div>' +
        '<div class="rw"><span class="k">客户端版本</span><span class="v">' +
          (v.name || '—') + ' <em>' + (v.mods_count || '—') + ' 个模组</em></span></div>' +
        '<div class="rw"><span class="k">游戏内存</span><span class="v">' + memMb +
          ' MB <em>' + (mem.gc ? mem.gc + ' GC' : '') + '</em></span></div>' +
        '<div class="rw"><span class="k">需要下载</span><span class="v">' +
          (pend.files || 0) + ' 个文件 <em>' + fmtBytes(pend.bytes || 0) + '</em></span></div>' +
        '<div class="rw"><span class="k">安装到</span><span class="v mono" ' +
          'style="font-size:11px;color:#79828f">' +
          (((st.settings || {}).game_dir) || '—') +
          (st.data_in_app
            ? '<br><em style="font-size:10.5px;color:#4a5260">都在 cogcraft.app 内部，' +
              '删掉 app 会一起清空</em>'
            : '') +
          '</span></div>' +
      '</div>' +
      '<div class="wsub" style="margin-top:18px;font-size:11.5px;color:#4a5260">' +
        '点「开始安装」后就可以去干别的了，装完会自动进游戏。</div>';
  }
}

// ══════════ 引导新增两步的交互（昵称 / 加群）══════════
function esc(v){
  return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

// ── 登录 / 注册 ──────────────────────────────

function bindAuthStep(){
  const nameInp = $('wAuthName'), pwdInp = $('wAuthPwd'), pwd2Inp = $('wAuthPwd2');
  const sw = $('wAuthSwitch');

  // 切换登录/注册：整体重画这一步。输入不保留 —— 两套表单状态混在一起更容易出 bug。
  if (sw){
    sw.onclick = function(){
      authMode = (authMode === 'login') ? 'register' : 'login';
      renderWizard();
    };
  }

  if (nameInp){
    nameInp.onkeydown = function(e){ if (e.key === 'Enter' && pwdInp) pwdInp.focus(); };
  }
  if (pwdInp){
    pwdInp.onkeydown = function(e){
      if (e.key !== 'Enter') return;
      if (pwd2Inp) pwd2Inp.focus(); else submitAuth();
    };
  }
  if (pwd2Inp){
    pwd2Inp.onkeydown = function(e){ if (e.key === 'Enter') submitAuth(); };
  }

  setTimeout(function(){ try { if (nameInp) nameInp.focus(); } catch (e) {} }, 80);
}

function authSay(msg){
  const err = $('wAuthErr');
  if (!err) return;
  err.textContent = msg || '';
  err.className = 'wform-err' + (msg ? ' on' : '');
}

// 登录步提交。成功 true，失败把原因写在表单下面。
async function submitAuth(){
  const nameInp = $('wAuthName'), pwdInp = $('wAuthPwd'), pwd2Inp = $('wAuthPwd2');
  const btn = $('wNext');
  const name = (nameInp ? nameInp.value : '').trim();
  const pwd = pwdInp ? pwdInp.value : '';
  const isReg = authMode === 'register';

  if (!name){ authSay('先填游戏 ID'); if (nameInp) nameInp.focus(); return false; }
  if (!pwd){ authSay('密码不能为空'); if (pwdInp) pwdInp.focus(); return false; }
  if (isReg){
    if (pwd.length < 8){ authSay('密码至少 8 位'); if (pwdInp) pwdInp.focus(); return false; }
    if (pwd2Inp && pwd2Inp.value !== pwd){ authSay('两次输入的密码不一样'); pwd2Inp.focus(); return false; }
  }

  authSay('');
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = isReg ? '正在注册…' : '正在登录…';

  try{
    const r = isReg ? await api.auth_register(name, pwd) : await api.auth_login(name, pwd);
    if (!r || !r.ok){
      authSay((r && r.error) || '没成功，再试一次');
      btn.disabled = false;
      btn.textContent = oldText;
      return false;
    }
    try { S = await api.state(); } catch (e) {}
    renderWizard();            // 成功后会走「已登录卡片」分支
    return true;
  } catch (e){
    authSay('连接失败，检查一下网络');
    btn.disabled = false;
    btn.textContent = oldText;
    return false;
  }
}

// 退出登录：清本地 token，回到登录页重输
async function doLogout(){
  try { await api.auth_logout(); } catch (e) {}
  try { S = await api.state(); } catch (e) {}
  renderWizard();
}

function bindQQStep(){
  const hint = $('qqHint');
  const say = function(t){
    hint.textContent = t; hint.className = 'qqhint on';
    clearTimeout(hint._t);
    hint._t = setTimeout(function(){ hint.className = 'qqhint'; }, 2800);
  };
  $('qqCopy').onclick = async function(){
    // 冻结包里是 file:// 环境，navigator.clipboard 常被判为非安全上下文，所以留一条退路
    let ok = false;
    try { await navigator.clipboard.writeText(QQ_GROUP); ok = true; } catch (e) {}
    if (!ok){
      try {
        const ta = document.createElement('textarea');
        ta.value = QQ_GROUP;
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        ok = document.execCommand('copy');
        document.body.removeChild(ta);
      } catch (e) {}
    }
    say(ok ? '群号已复制，去 QQ 里粘贴搜索' : '复制没成功，手动记一下：' + QQ_GROUP);
  };
  $('qqOpen').onclick = async function(){
    try { await api.open_url(QQ_URL); say('已在浏览器打开加群页'); }
    catch (e) { say('打不开就手动搜群号：' + QQ_GROUP); }
  };
}

$('wBack').onclick = () => { if (wStep > 0){ wStep--; renderWizard(); } };
$('wNext').onclick = async () => {
  if (wStep === 1 && !(S && S.auth && S.auth.logged_in)){
    // 没登录就点「登录并继续」= 提交登录；失败就停在原地，不往下走
    const ok = await submitAuth();
    if (!ok) return;
  }
  if (wStep < LAST){ wStep++; renderWizard(); return; }
  $('wNext').disabled = true;
  $('wNext').textContent = '正在开始…';
  try { await api.finish_onboard(); } catch (e) {}
  closeWizard();
  $('wNext').disabled = false;
  startInstall();
};

// 截图/调试用：window.__wiz(2) 直接跳到第 3 步
window.__wiz = (n) => { wStep = n; $('wizard').classList.add('on'); renderWizard(); };
window.__busy = () => document.getElementById('app').classList.add('busy');
window.__steps = updateSteps;

// 后台跑性能实测：后端立即返回（可能 pending），结果通过 bench 事件推回来，
// 这样界面不会被 3.4 秒的测试卡住。


// ══════════════ 绑定 QQ（引导第 3 步的大板块）══════════════
// 流程在 10-account.js 的 bindStart() 里 —— 那是唯一实现（引导与账号面板共用）。
// 这里只给容器和「先不绑」的去向，别再往这里长第二份。
async function fillBindArea(){
  const box = $('bindArea');
  if (!box) return;
  if (!(S && S.auth && S.auth.logged_in)){
    box.innerHTML = '<div class="binderr">这一步需要先登录账号。</div>';
    return;
  }
  bindSkipCount = 0;
  await bindStart(box, { onSkip: () => { wStep++; renderWizard(); } });
}
