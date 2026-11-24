// 官网入口（Phase 3）：链接全部来自清单 links 段，改地址不用发新版客户端。
//
// 两层校验：
//   后端 app.py 的 state() 已经把非 https 的整条滤掉（连 "_说明" 这种备注也滤了），
//  这里再校验一次 —— 拿到的东西仍不当成可信输入，界面代码自己也可能被改。
//
// 打开动作不新写一套：统一走 api.open_url()（01-core.js 里的 pywebview 桥），
// 和引导页「加入交流群」按钮同一条路。pywebview 里 <a target="_blank"> 会开出
// 没有地址栏的空窗口，所以必须交给系统浏览器。

'use strict';

// key → 显示名。增删链接改清单即可，这里只决定中文叫什么、按什么顺序排。
const LINK_META = {
  home:     '官网',
  donate:   '捐赠',
  announce: '公告',
  rules:    '规则',
  banlist:  '封禁名单',
  skin:     '皮肤',
  download: '下载',
  feedback: '反馈',
};

// 主屏底部那一行：全列。整个客户端只有这一处入口，
// 设置面板和账号面板里都不再重复放（同一功能不留多套）。
const LINK_FOOT = ['home', 'donate', 'announce', 'rules', 'banlist',
                   'skin', 'download', 'feedback'];

function linkTable() {
  return (typeof S !== 'undefined' && S && S.links) ? S.links : {};
}

function linkSafe(u) {
  return typeof u === 'string' && /^https:\/\/[^\s]+$/i.test(u.trim());
}

function linkOpen(u) {
  if (!linkSafe(u)) { return; }
  try {
    if (typeof api === 'undefined' || !api.open_url) { return; }
    const r = api.open_url(u);
    if (r && typeof r.catch === 'function') { r.catch(function () {}); }
  } catch (e) { /* 打不开就什么也不做，不弹窗吓人 */ }
}

function linkRender(el, keys) {
  if (!el) { return; }
  const L = linkTable();
  const box = document.createElement('span');
  keys.forEach(function (k) {
    const u = L[k];
    if (!linkSafe(u)) { return; }          // 清单没配 / 不是 https → 直接不显示
    const a = document.createElement('a');
    a.className = 'plink';
    a.textContent = LINK_META[k] || k;
    a.title = u;
    a.href = '#';                          // 有 href 才有手型光标和键盘可达
    a.onclick = function (ev) { ev.preventDefault(); linkOpen(u); };
    box.appendChild(a);
  });
  el.textContent = '';
  // 一条都没配就别塞空 <span>：留空的 el 才能命中 .plinks:empty，
  // 否则那条分隔线会孤零零挂在底栏下面
  if (box.childNodes.length) { el.appendChild(box); }
}

/** 04-render.js 每次 render(st) 都会调一次（用 typeof 兜底，脚本可能还没加载完） */
function renderLinks() {
  linkRender(document.getElementById('footLinks'), LINK_FOOT);
  // 清单里没配捐赠链接就不显示这颗心，不占顶栏位置
  const b = document.getElementById('btnDonate');
  if (b) { b.hidden = !linkSafe(linkTable().donate); }
}

// ♥ 按钮：DOM 是静态的，绑一次就够，不跟着 render 重复绑
(function () {
  const b = document.getElementById('btnDonate');
  if (b) { b.onclick = function () { linkOpen(linkTable().donate); }; }
})();
