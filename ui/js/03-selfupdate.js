// 启动器自更新（检查/准备/应用/回滚）与右上角更新胶囊
// 拆自原 ui/index.html 第 559-707 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

let suState = { phase:'idle', pct:0, ready:false, version:'', text:'', error:null };
let lastUpdate = null;
let suFastTimer = null;

async function refreshSelfUpdate(){
  try { const s = await api.get_update_progress(); if (s) suState = s; } catch (e) {}
  if (lastUpdate) renderUpdate(lastUpdate);
}

// 下载时进度要跟得上，20 秒一次太钝；只在下载/解包期间加速，完了自动停
function suFastLoop(){
  if (suFastTimer) return;
  suFastTimer = setInterval(async () => {
    await refreshSelfUpdate();
    const ph = (suState || {}).phase;
    if (ph !== 'downloading' && ph !== 'unpacking'){
      clearInterval(suFastTimer); suFastTimer = null;
    }
  }, 600);
}

async function doPrepareUpdate(){
  try {
    const r = await api.prepare_update();
    if (r && r.ok){ toast('正在下载更新…'); suFastLoop(); return; }
    // 这台机器没法自更新（还在 dmg 里、目录不可写…）：说清原因并给手动下载
    toast((r && r.error) || '这台机器无法自动更新');
    if (r && r.fallback){ try { await api.open_download_page(); } catch (e) {} }
  } catch (e) { toast('准备更新失败：' + e); }
}

async function doApplyUpdate(){
  try {
    const r = await api.apply_update();
    if (r && r.ok){ toast('正在重启完成更新…'); return; }
    toast((r && r.error) || '无法完成更新，请稍后重试或到官网手动下载');
  } catch (e) { toast('更新失败：' + e); }
}

function retrySelfUpdate(){
  suState = { phase:'idle', pct:0, ready:false, version:'', text:'', error:null };
  doPrepareUpdate();
}

// ── 更新提示胶囊：启动器本体 + 服务器下发的游戏文件，两种都走这一处 ──
function renderUpdate(u){
  const chip = $('upChip');
  if (!chip) return;
  const L = (u && u.launcher) || {};
  const F = (u && u.files) || {};
  const filesPending = (Number(F.changed) || 0) + (Number(F.removed) || 0);

  // ① 启动器本体有新版本：点一下就在启动器内下载、替换自己、自动重启，
  //    不再把人踢去浏览器手动下包（那一步正是玩家最烦的）。
  chip.classList.remove('ready', 'busy');
  if (L.available){
    const su = suState || {};
    const sameVer = su.version && L.latest && su.version === L.latest;

    // 正在重启换包：别再让人点
    if (su.phase === 'applying'){
      chip.hidden = false;
      chip.classList.add('busy');
      chip.textContent = '⟳ 正在重启…';
      chip.title = '启动器正在退出并替换文件，完成后会自动打开';
      chip.onclick = null;
      return;
    }

    // 包已就绪：绿胶囊，点一下重启完成更新
    if (su.ready && sameVer){
      chip.hidden = false;
      chip.classList.add('ready');
      chip.textContent = '⟳ 重启完成更新';
      chip.title = '新版本 ' + (su.version || '') + ' 已下载并校验通过，点一下重启完成更新。'
                 + '你的设置、存档路径、证书都会保留。';
      chip.onclick = doApplyUpdate;
      return;
    }

    // 下载/解包中：把进度画在胶囊上
    if (su.phase === 'downloading' || su.phase === 'unpacking'){
      chip.hidden = false;
      chip.classList.add('busy');
      const pct = Math.max(0, Math.min(100, Math.round((Number(su.pct) || 0) * 100)));
      chip.textContent = (su.phase === 'unpacking' ? '⟳ 解包 ' : '↓ ') + pct + '%';
      chip.title = su.text || '正在准备更新';
      chip.onclick = null;
      return;
    }

    // 失败过：明说失败，并给手动下载的退路
    if (su.phase === 'failed'){
      chip.hidden = false;
      chip.classList.toggle('must', !!L.mandatory);
      chip.textContent = '↑ 自动更新失败，点此手动下载';
      chip.title = (su.error || '') + '（也可以稍后再点一次重试）';
      chip.onclick = () => { retrySelfUpdate(); };
      return;
    }

    // 默认：点一下开始下载
    const mb = Math.round((Number((L.pkg || {}).size) || 0) / 1048576);
    chip.hidden = false;
    chip.classList.toggle('must', !!L.mandatory);
    chip.textContent = (L.mandatory ? '↑ 必须更新到 ' : '↑ 有新版本 ') + (L.latest || '');
    chip.title = '点一下在启动器内直接更新'
               + (mb ? ('（约 ' + mb + 'MB）') : '')
               + '：下载 → 校验 → 重启完成，不用去官网手动下包';
    chip.onclick = doPrepareUpdate;
    return;
  }

  // ② 服务器刚下发了新的模组/配置：下次启动游戏时 pipeline 会自动同步，
  //    运行中被映射的 jar 换不掉，所以这里只提示，不假装能热更
  if (filesPending > 0){
    chip.hidden = false;
    chip.classList.remove('must');
    chip.textContent = '↻ ' + filesPending + ' 个文件待更新';
    chip.title = '服务器下发了新内容（' + (F.manifest_updated || '刚刚') + '），'
               + '下次启动游戏时会自动下载';
    chip.onclick = () => toast('新内容会在下次点「启动游戏」时自动同步，无需手动操作');
    return;
  }

  chip.hidden = true;
}

// 启动时查一次 + 之后常驻刷新。
// 后端每 5 分钟带 If-None-Match 重查一次清单（没变就只有 304，几乎零流量）；
// 这里只读后端内存里的结果，完全不发网络 —— 所以可以放得很密而不影响任何人。
async function checkUpdatesOnBoot(){
  try { await api.check_updates_async(); } catch (e) {}
  for (let i = 0; i < 20; i++){
    await new Promise(r => setTimeout(r, 1000));
    let finished = false;
    try {
      const u = await api.get_update();
      if (u && u.update){ lastUpdate = u.update; renderUpdate(u.update); finished = !u.checking; }
    } catch (e) { break; }
    if (finished) break;
  }
  setInterval(async () => {
    try { const u = await api.get_update(); if (u && u.update) lastUpdate = u.update; }
    catch (e) {}
    await refreshSelfUpdate();        // 内部会把 lastUpdate 重新画一遍（含自更新进度）
  }, 20000);
}

