// 主界面状态渲染：进度条、按钮态、字节格式化
// 拆自原 ui/index.html 第 708-874 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

function setProgress(pct, text, label){
  const pt = $('pText'); if (pt) pt.classList.remove('detail');
  busy = true;
  if (label) $('tbState').textContent = label;
  if (text) $('pText').textContent = text;
  if (typeof pct === 'number' && pct > 0) {
    setBar('fill');
    $('barFill').style.width = (pct * 100).toFixed(1) + '%';
    $('pPct').textContent = Math.round(pct * 100) + '%';
  } else {
    setBar('indet');
  }
  $('btnMain').disabled = true;
}

function setBar(mode){
  const bar = $('bar');
  bar.classList.toggle('indet', mode === 'indet');
  if (mode === 'indet' || mode === 'error' || mode === 'done') {
    $('barFill').style.width = '0%';
  }
  if (mode === 'done') {
    $('barFill').style.width = '100%';
    $('barFill').style.background = 'linear-gradient(90deg,#3f7d55,#5fb87c)';
  }
  if (mode === 'error') {
    $('barFill').style.width = '100%';
    $('barFill').style.background = '#e06c6c';
  }
  if (mode === 'fill') {
    $('barFill').style.background = 'linear-gradient(90deg,#4f7cff,#6fa0ff)';
  }
}

// ═══ 渲染 ═══
function render(st){
  S = st;
  const mf = st.minecraft || {}, ml = st.modloader || {}, srv = st.server || {};

  $('fMc').textContent = mf.version || '—';
  $('fNf').textContent = ml.version || '—';
  if (srv.name) $('srvName').textContent = srv.name;
  if (srv.address) $('srvAddr').textContent = srv.address;

  const v = (st.variants || []).find(x => x.id === st.variant);
  $('fMods').textContent = v ? v.mods_count : '—';
  $('fRam').textContent = st.memory ? (st.memory.xmx_mb + ' MB') : '—';

  // 连接状态
  const chip = $('connChip'), ctext = $('connText');
  if (st.manifest_error) {
    chip.querySelector('.dot').className = 'dot warn';
    ctext.textContent = '离线';
  } else if (st.offline) {
    chip.querySelector('.dot').className = 'dot warn';
    ctext.textContent = '离线缓存';
  } else {
    chip.querySelector('.dot').className = 'dot ok';
    ctext.textContent = '已连接';
  }

  // 按钮状态
  const pending = st.pending || { files: 0, bytes: 0 };
  busy = false;
  if (pending.files > 0) {
    ready = false;
    $('btnMain').textContent = '开始准备 (' + pending.files + ' 个文件)';
    setBar('fill'); $('barFill').style.width = '0%';
    $('pText').textContent = '需要下载 ' + fmtBytes(pending.bytes);
    $('pPct').textContent = '';
    $('btnMain').disabled = false;
    $('pill').className = 'pill'; $('pill').textContent = '待准备';
    $('tbState').textContent = '就绪';
  } else {
    ready = true;
    $('btnMain').textContent = '进入游戏';
    $('btnMain').disabled = false;
    setBar('done');
    $('pText').textContent = '客户端已就绪';
    $('pPct').textContent = '100%';
    $('pill').className = 'pill ok'; $('pill').textContent = '● 已就绪';
    $('tbState').textContent = '就绪';
  }

  // 设置面板
  $('pHw').textContent = (st.hardware_text || '').replace(/\n/g, '  ·  ');
  const mem = st.memory || {};
  const ceiling = mem.ceiling_mb || 16384;
  const range = $('memRange');
  range.max = Math.max(2048, Math.ceil(ceiling / 512) * 512);
  range.value = mem.xmx_mb || 4096;
  $('memBig').textContent = mem.xmx_mb || '—';
  range.style.setProperty('--fill', ((range.value - range.min) / (range.max - range.min) * 100) + '%');
  $('tMin').textContent = '1024';
  $('tMax').textContent = '安全上限 ' + ceiling;

  let h = '本机物理内存 <b>' + Math.round((mem.total_ram_mb || 0) / 1024) + ' GB</b>，'
        + '游戏最多可用 <b>' + ceiling + ' MB</b>，其余留给系统和其它程序。';
  (mem.warnings || []).forEach(w => {
    h += '<br><span class="' + (w.level === 'danger' ? 'err' : 'warn') + '">⚠ ' + w.text + '</span>';
  });
  $('memHint').innerHTML = h;

  // 版本列表
  const rows = $('variantRows');
  rows.innerHTML = '';
  (st.variants || []).forEach(x => {
    const on = x.id === st.variant;
    const auto = x.id === st.variant_auto;
    const d = document.createElement('div');
    d.className = 'row';
    d.innerHTML = '<div><div class="nm">' + x.name +
      (auto ? ' <span style="font-size:10px;color:#5fb87c">· 推荐</span>' : '') +
      '</div><div class="ds">' + x.mods_count + ' 个模组 · 至少 ' + x.min_ram_mb + ' MB 内存</div></div>' +
      '<div class="right"><div class="seg"><button class="' + (on ? 'on' : '') + '">' +
      (on ? '使用中' : '切换') + '</button></div></div>';
    d.querySelector('button').onclick = async () => {
      const st2 = await api.set_variant(x.id);
      render(st2);
    };
    rows.appendChild(d);
  });

  $('playerName').value = (st.settings || {}).player_name || '';
  // 服务器地址：留空 = 自动（内网可达走内网，否则公网）
  // 注意变量名别用 srv —— render() 里后面已经有一个 const srv，重名会让整个脚本语法失败
  const srvInfo = st.server || {};
  const srvInput = $('srvAddr');
  srvInput.value = (st.settings || {}).server_address || '';
  srvInput.placeholder = srvInfo.address_public || '自动';
  const srcMap = { manual: '手动指定', lan: '内网直连', 'public': '公网' };
  const srcTxt = srcMap[srvInfo.source] || '';
  $('srvHint').textContent = (st.settings || {}).server_address
    ? '手动：' + (srvInfo.address || '—')
    : ((srvInfo.source === 'lan')
        ? '自动 · 内网 ' + srvInfo.address + '（公网域名在内网连不上）'
        : '留空 = 自动 · 当前 ' + (srvInfo.address || '—') + (srcTxt ? '（' + srcTxt + '）' : ''));
  srvInput.onchange = async () => {
    const st2 = await api.set_server_address(srvInput.value.trim());
    render(st2);
  };

  const aj = (st.settings || {}).auto_join !== false;
  [].forEach.call($('autoJoinSeg').children, b => {
    b.classList.toggle('on', (b.dataset.v === '1') === aj);
    b.onclick = async () => {
      await api.set_auto_join(b.dataset.v === '1');
      const st2 = await api.state();
      render(st2);
    };
  });

  const j = st.java || {};
  $('javaHint').innerHTML = j.path
    ? (j.bundled ? '✓ 使用启动器内置 64 位 Java' : '使用系统 Java：<span class="mono">' + j.path + '</span>')
    : '<span class="err">未找到 Java，无法启动</span>';

  $('jvmArgs').textContent = (mem.jvm_args || []).join(' ') || '—';
}

function fmtBytes(n){
  if (!n) return '0 MB';
  if (n > 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
  return Math.round(n / 1048576) + ' MB';
}

// ═══ 交互 ═══
