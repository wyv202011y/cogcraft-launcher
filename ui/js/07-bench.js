// 硬件跑分展示块
// 拆自原 ui/index.html 第 955-972 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

function benchBlock(b){
  if (!b || !b.ok){
    return '<div class="brow"><div class="bsc wait"><div class="bspin"></div>' +
           '<b>正在跑性能测试…</b></div>' +
           '<div class="bm"><span>约 3 秒，用游戏同款 Java 实测</span></div></div>';
  }
  const r = b.ratios || {}, raw = b.raw || {};
  return '<div class="brow">' +
    '<div class="bsc"><b>' + (b.score || 0).toFixed(2) + '</b><i>× 综合性能</i></div>' +
    '<div class="bm">' +
      '<span>单核 <b>' + (r.int || 0).toFixed(2) + '×</b></span>' +
      '<span>内存延迟 <b>' + (r.mem || 0).toFixed(2) + '×</b></span>' +
      '<span>GC <b>' + (r.gc || 0).toFixed(2) + '×</b></span>' +
      '<span>最长停顿 <b>' + (raw.stall_ms || 0).toFixed(1) + 'ms</b></span>' +
    '</div></div>';
}

// ══════════ 首次引导 ══════════
