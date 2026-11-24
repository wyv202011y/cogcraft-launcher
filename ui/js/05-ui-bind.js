// 主界面控件事件绑定（开始/设置/重置/内存滑块/昵称输入）
// 拆自原 ui/index.html 第 875-912 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

$('btnMain').onclick = async () => {
  if (busy) return;
  if (!ready) { startInstall(); return; }
  const r = await api.launch();
  if (r && r.ok === false) { toast(r.error); } else { startPolling(); }
};

// 同一个按钮开、也同一个按钮关：只 add 不 remove 的话，第二次点等于没点
$('btnSettings').onclick = () => {
  const on = $('panel').classList.toggle('on');
  $('btnSettings').classList.toggle('on', on);   // 按钮自身也跟着亮/灭
};
$('btnOnboard').onclick = () => { $('panel').classList.remove('on'); openWizard(); };
$('btnClosePanel').onclick = () => $('panel').classList.remove('on');
$('btnReset').onclick = async () => {
  const st = await api.reset_settings();
  render(st);
};

let memTimer = null;
$('memRange').oninput = (e) => {
  const val = parseInt(e.target.value, 10);
  $('memBig').textContent = val;
  e.target.style.setProperty('--fill',
    ((val - e.target.min) / (e.target.max - e.target.min) * 100) + '%');
  clearTimeout(memTimer);
  memTimer = setTimeout(async () => {
    const st = await api.set_memory(val);
    render(st);
  }, 450);
};

let nameTimer = null;
$('playerName').oninput = (e) => {
  clearTimeout(nameTimer);
  nameTimer = setTimeout(async () => {
    await api.set_player(e.target.value);
  }, 500);
};


// ══════════ 下载详情：步骤清单 ══════════


// ══════════ 测试模式（Phase 5）══════════
// 切换是**后端说了算**：set_mode 会校验档位，normal 玩家就算手改 settings.json 也切不过去。
if ($('modeSeg')){
  $('modeSeg').onclick = async (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (busy) { toast('正在准备客户端，等它跑完再切模式'); return; }
    try {
      render(await api.set_mode(b.dataset.mode));
    } catch (err) {
      toast((err && err.message) ? err.message : '切换模式失败');
    }
  };
}
if ($('testStripX')){
  $('testStripX').onclick = () => {
    $('testStrip').classList.remove('show');
    _testStripClosed = true;        // 收起后本次会话不再出现
  };
}
