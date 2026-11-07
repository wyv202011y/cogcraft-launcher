// 跑分执行与安装启动
// 拆自原 ui/index.html 第 1228-1291 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

async function runBench(){
  try {
    const b = await api.benchmark();
    if (b && b.ok) applyBench(b);            // 命中缓存：直接就有结果
  } catch (e) {}
}

async function applyBench(b){
  if (!b || !b.ok) return;
  if (S) S.bench = b;
  if ($('wizard').classList.contains('on')) renderWizard();
  try { render(await api.state()); } catch (e) {}
}

// ══════════ 安装入口（主按钮与引导共用）══════════
async function startInstall(){
  if (busy) return;
  resetSteps();
  const r = await api.start();
  if (r && r.ok === false){ toast(r.error); return; }
  busy = true;
  $('btnMain').disabled = true;
  $('btnMain').textContent = '准备中';
  setBar('indet');
  startPolling();
}

// ═══ 启动 ═══
(async function init(){
  try {
    // 先等后端桥接把方法挂上（macOS 上桥分两步注入，只等对象存在不够）
    const bridged = await waitMethod('bootstrap', 20000);
    if (!bridged && !MOCK_API) {
      $('pText').textContent = '启动器后端没有响应（桥接未就绪），请重启应用';
      setBar('error');
      $('btnMain').textContent = '重试';
      $('btnMain').disabled = false;
      $('btnMain').onclick = () => location.reload();
      return;
    }
    const st = await api.bootstrap();
    render(st);
    resetSteps();
    // 开发/截图用：index.html?panel=1 直接展开设置面板
    if (location.search.indexOf('panel=1') >= 0) $('panel').classList.add('on');
    // 首次使用（或 ?onboard=1）走引导
    const done = ((st.settings || {}).onboarded) === true;
    if (!done || location.search.indexOf('onboard=1') >= 0) openWizard();
    // 后台跑性能实测：用户看第 1 页的时间就够它跑完，体感零等待
    if (!(st.bench && st.bench.ok)) runBench();
    // 每次启动都查一遍启动器更新（用户明确要求；查不到就当没事）
    checkUpdatesOnBoot();
  } catch (e) {
    const emsg = (e && (e.message || e.name)) ? (e.message || e.name) : String(e);
    $('pText').textContent = '启动器初始化失败：' + emsg;
    $('btnMain').textContent = '重试';
    $('btnMain').disabled = false;
    // 错误落盘到后端 launcher.log —— 用户机器上没有控制台，日志是唯一排障入口
    try { api.client_error('bootstrap 失败: ' + emsg + '\n' + ((e && e.stack) || '')); } catch (_) {}
  }
})();

// ═══ 开发/截图用的假桥（浏览器直接打开时生效）═══
// 截图/调试：如果外部注入了真实 state（ui/dev-state.json），就用真实数据
