// 安装六步进度指示器
// 拆自原 ui/index.html 第 913-954 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

const STEP_DEF = [
  ['disk',      '检查磁盘空间'],
  ['loader',    '安装模组加载器'],
  ['libraries', '补齐运行库'],
  ['assets',    '下载游戏资源'],
  ['files',     '同步模组与配置'],
];

function resetSteps(){
  $('steps').innerHTML = STEP_DEF.map(([k, n]) =>
    '<div class="stp" data-k="' + k + '"><i class="ic"></i>' +
    '<span class="nm">' + n + '</span><span class="ms"></span></div>').join('');
}

function updateSteps(step, pct, msg){
  const idx = STEP_DEF.findIndex(d => d[0] === step);
  if (idx < 0) return;
  document.getElementById('app').classList.add('busy');
  const kids = $('steps').children;
  for (let i = 0; i < kids.length; i++){
    const el = kids[i];
    el.classList.remove('doing', 'fail');
    if (i < idx){ el.classList.add('done'); el.querySelector('.ms').textContent = ''; }
    if (i === idx){
      el.classList.add(pct >= 1 ? 'done' : 'doing');
      el.querySelector('.ms').textContent = msg || '';
    }
  }
}

function failSteps(){
  const kids = $('steps').children;
  for (let i = 0; i < kids.length; i++){
    if (kids[i].classList.contains('doing')){
      kids[i].classList.remove('doing');
      kids[i].classList.add('fail');
      return;
    }
  }
}

// 性能实测评分的展示块
