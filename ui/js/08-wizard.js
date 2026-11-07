// 首次引导向导（昵称/硬件检测/选版本/加群/就绪）
// 拆自原 ui/index.html 第 973-1227 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

const WNAME = ['欢迎', '你的昵称', '硬件检测', '选择版本', '加入交流群', '准备就绪'];
// 群信息：与官网导航栏同源（463960828），改群时这里和站上一起改
const QQ_GROUP = '463960828';
const QQ_URL = 'https://qm.qq.com/q/xqjGbFAQIU';
const LAST = WNAME.length - 1;
// 服务器用这个 ID 认人，所以规则跟 Minecraft 用户名一致，额外允许中文
const NAME_RE = /^[\u4e00-\u9fa5A-Za-z0-9_]{2,16}$/;
let wStep = 0;

function openWizard(){ wStep = 0; $('wizard').classList.add('on'); renderWizard(); }
function closeWizard(){ $('wizard').classList.remove('on'); }

function renderWizard(){
  const st = S || {};
  const mem = st.memory || {};
  const pend = st.pending || {};
  const variants = st.variants || [];
  const cur = st.variant || (variants[0] || {}).id;
  const server = st.server || {};

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
                         : (wStep === LAST ? '开始安装' : '下一步');

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
    // 为什么放在最前面：服务器拿这个 ID 认人，装完再改等于换人。
    // 事先确认一遍，比事后在设置里翻找强。
    // 只在玩家自己填过时才预填：没填过时后端给的是系统用户名（server / Administrator），
    // 那东西当游戏 ID 又丑又容易不合规，不如留空让他自己起。
    const cfg = st.settings || {};
    const curName = cfg.player_name_set ? (cfg.player_name || '') : '';
    body.innerHTML =
      '<div class="wsec">先给自己起个名字</div>' +
      '<div class="wname">' +
        '<input id="wNameInput" maxlength="16" spellcheck="false" autocomplete="off" ' +
          'placeholder="游戏内名字，例如 Steve" value="' + esc(curName) + '">' +
        '<span class="cnt" id="wNameCnt">' + curName.length + '/16</span>' +
      '</div>' +
      '<div class="wname-err" id="wNameErr"></div>' +
      '<div class="wsub" style="margin-top:8px">' +
        '服务器用这个名字认你，进服之后改名等于换人（存档、背包、权限都带不走）。<br>' +
        '<span style="color:#4a5260">支持中文、字母、数字、下划线，2~16 个字符；' +
        '不要空格和符号。之后在设置里也能改。</span></div>';
    bindNameStep();
  }

  if (wStep === 2){
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

  if (wStep === 3){
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

  if (wStep === 4){
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

  if (wStep === 5){
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

function bindNameStep(){
  const inp = $('wNameInput'), cnt = $('wNameCnt'), err = $('wNameErr'), next = $('wNext');
  if (!inp) return;
  const check = function(){
    const v = (inp.value || '').trim();
    cnt.textContent = (inp.value || '').length + '/16';
    const ok = NAME_RE.test(v);
    err.textContent = (v && !ok) ? '只能用中文、字母、数字、下划线，长度 2~16' : '';
    err.className = 'wname-err' + ((v && !ok) ? ' on' : '');
    next.disabled = !ok;                     // 不合格就不让往下走
    return ok;
  };
  inp.oninput = check;
  inp.onkeydown = function(e){ if (e.key === 'Enter' && check()) next.click(); };
  check();
  setTimeout(function(){ try { inp.focus(); inp.select(); } catch (e) {} }, 80);
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
  if (wStep === 1){                       // 离开昵称步：先存进设置再走
    const inp = $('wNameInput');
    const nm = (inp ? inp.value : '').trim();
    if (!NAME_RE.test(nm)) return;        // 按钮已 disabled，这里是双保险
    try { render(await api.set_player(nm)); } catch (e) {}
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
