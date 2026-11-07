// 全局状态、后端桥代理 api、$ 选择器
// 拆自原 ui/index.html 第 394-450 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

'use strict';

// ═══ 与 Python 的桥 ═══
// 绝不能在解析时把 api 固定死：pywebview 的桥是页面加载完之后才注入的
// （官方要求监听 pywebviewready）。macOS 的 WKWebView 注入更晚，写死就会
// 永远落到 mockApi()，表现为「进不了引导页 + 点开始准备后永久停在准备中 +
// 界面数据全是假的」。这里改成惰性代理 + 显式等待。
let REAL_API = null;
const MOCK_API = (location.search.indexOf('mock=1') >= 0) ? mockApi() : null;

// ⚠️ pywebview 是**分两步**注入的（看 webview/util.py 的 inject_pywebview）：
//   第一步 run_js(api.js) 建出 window.pywebview = { api: {} } —— 方法还是空的；
//   第二步 run_js(finish.js) 调 _createApi() 挂上全部方法，随后才派发 pywebviewready。
// 所以「window.pywebview 存在」≠「方法可用」。必须等到**具体那个方法**是函数为止，
// 否则 macOS 上会在两步之间抢跑，报「后端桥接未就绪：bootstrap」。
function refreshApi(){
  if (window.pywebview && window.pywebview.api){ REAL_API = window.pywebview.api; return REAL_API; }
  return null;
}
function hasMethod(name){
  const a = refreshApi();
  return !!(a && typeof a[name] === 'function');
}
async function waitMethod(name, ms){
  // ⚠️ 不能写 `ms || 20000`：传 0（浏览器预览模式下「别等，直接用 mock」）会被
  // 当成假值回落成 20 秒，于是每个 API 调用都白卡 20 秒，页面永远停在「正在初始化」。
  const budget = (typeof ms === 'number' && ms >= 0) ? ms : 20000;
  const t0 = Date.now();
  while (true){
    if (hasMethod(name)) return true;
    if (Date.now() - t0 > budget) return false;
    await new Promise(r => setTimeout(r, 100));
  }
}
const api = new Proxy({}, {
  get(_t, name){
    return async function(...args){
      if (!hasMethod(name)){
        // 桥还没挂上这个方法（或是在浏览器里直接打开页面）
        const ok = await waitMethod(name, MOCK_API ? 0 : 20000);
        if (!ok){
          if (MOCK_API && typeof MOCK_API[name] === 'function') return MOCK_API[name](...args);
          throw new Error('启动器后端没就绪（' + String(name) + ' 不可用），请重启应用');
        }
      }
      return REAL_API[name](...args);
    };
  }
});

let S = null;          // 后端 state
let busy = false;
let ready = false;

const $ = (id) => document.getElementById(id);

// ── 事件接收（由 app.py 调 window.__onEvent）──
