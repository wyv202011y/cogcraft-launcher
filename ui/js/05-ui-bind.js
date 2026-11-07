// 主界面控件事件绑定（开始/设置/重置/内存滑块/昵称输入）
// 拆自原 ui/index.html 第 875-912 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

$('btnMain').onclick = async () => {
  if (busy) return;
  if (!ready) { startInstall(); return; }
  const r = await api.launch();
  if (r && r.ok === false) { toast(r.error); } else { startPolling(); }
};

$('btnSettings').onclick = () => $('panel').classList.add('on');
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
