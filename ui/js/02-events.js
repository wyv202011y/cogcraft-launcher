// 后端事件分发、轮询、toast 提示
// 拆自原 ui/index.html 第 451-558 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

function handleEvent(kind, data){
  if (kind === 'progress') {
    setProgress(data.pct, data.text, data.label);
    if (data.step === 'done') {
      // 全部收尾：把步骤全打勾，等一下再退回就绪界面
      [].forEach.call($('steps').children, el => {
        el.classList.remove('doing', 'fail');
        el.classList.add('done');
        el.querySelector('.ms').textContent = '';
      });
      setTimeout(() => document.getElementById('app').classList.remove('busy'), 900);
    } else {
      updateSteps(data.step, data.pct, data.text);
    }
  } else if (kind === 'error') {
    failSteps();
    busy = false; ready = false;
    setBar('error');
    const full = data.text || '出错了';
    $('pText').textContent = full;
    $('pText').classList.toggle('detail', full.length > 40 || full.includes('\n'));
    $('pPct').textContent = '';
    $('btnMain').disabled = false;
    $('btnMain').textContent = '重试';
    $('pill').className = 'pill err';
    $('pill').textContent = '❌ 失败';
    $('tbState').textContent = '出错了';
    console.error(data.detail || '');
  } else if (kind === 'bench') {
    applyBench(data);
  } else if (kind === 'phase') {
    const p = data.phase;
    if (p === 'launching' || p === 'running') {
      document.getElementById('app').classList.remove('busy');
    }
    if (p === 'launching') {
      setBar('indet');
      $('pText').textContent = '正在启动游戏…';
      $('pPct').textContent = '';
      $('btnMain').disabled = true;
      $('btnMain').textContent = '启动中';
      $('pill').className = 'pill';
      $('pill').textContent = '🎮 启动中';
    } else if (p === 'running') {
      busy = true;
      setBar('done');
      $('pText').textContent = '游戏运行中';
      $('pPct').textContent = '';
      $('btnMain').disabled = false;
      $('btnMain').textContent = '游戏运行中';
      $('pill').className = 'pill ok';
      $('pill').textContent = '● 游戏中';
      $('tbState').textContent = '游戏运行中';
    } else if (p === 'exited') {
      busy = false;
      $('btnMain').disabled = false;
      $('btnMain').textContent = '进入游戏';
      $('pill').className = 'pill';
      $('pill').textContent = data.text || '已退出';
      $('tbState').textContent = '就绪';
      if (data.oom === 'heap' && data.suggest_mb) {
        $('pText').innerHTML = '内存不足导致崩溃，建议把内存调到 <b>' +
          data.suggest_mb + ' MB</b> 后重试';
        setBar('error');
        toast('已自动记录崩溃原因');
      } else if (data.oom === 'system') {
        $('pText').textContent = data.text;
        setBar('error');
      } else {
        setBar('done');
        $('pText').textContent = data.text;
      }
    }
  }
}

// 依旧挂上推送通道：能推就推（Windows 上通常可用），推不到也无所谓，轮询在跑
window.__onEvent = handleEvent;

// ── 轮询兜底 ──
// macOS 的 WKWebView 下，Python 后台线程调 window.evaluate_js 会被静默吞掉，
// 界面因此永远收不到进度、卡死在「准备中」。改成主动拉：300ms 一次，按 seq 去重。
let pollTimer = null, pollSeq = 0, pollTicks = 0;
function startPolling(){
  stopPolling();
  pollSeq = 0; pollTicks = 0;
  pollTimer = setInterval(async () => {
    pollTicks++;
    if (pollTicks > 24000) { stopPolling(); return; }   // 约 2 小时兜底，别无限轮询
    let p = null;
    try { p = await api.poll(); } catch (e) { return; }
    if (!p) return;
    if (p.seq > pollSeq) { pollSeq = p.seq; handleEvent(p.kind, p.data || {}); }
    const k = p.kind, d = p.data || {};
    const finished = (k === 'error') ||
                     (k === 'phase' && (d.phase === 'running' || d.phase === 'exited'));
    if (finished) stopPolling();
  }, 300);
}
function stopPolling(){ if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

function toast(t){
  $('pill').className = 'pill';
  $('pill').textContent = t;
}

// ── 启动器自更新：在启动器里下完、换好、自己重启 ──
// 以前点了只是把浏览器拉起来跳官网，玩家还得自己下包解压覆盖 —— 那一步现在没了。
