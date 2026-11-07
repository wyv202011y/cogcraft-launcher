// Python 桥的两种实现：realApi（真后端）/ mockApi（浏览器预览假数据）
// 拆自原 ui/index.html 第 1292-1385 行（拆分前备份 index.html.bak_before_split_*）

'use strict';

function realApi(HWD){
  const clone = (o) => JSON.parse(JSON.stringify(o));
  return {
    bootstrap: async () => clone(HWD),
    state: async () => clone(HWD),
    start: async () => ({ok:true}),
    launch: async () => ({ok:true}),
    set_variant: async (v) => { HWD.variant = v; return clone(HWD); },
    set_memory: async (m) => { if (HWD.memory) HWD.memory.xmx_mb = m; return clone(HWD); },
    set_player: async () => clone(HWD),
    set_auto_join: async () => clone(HWD),
    set_server_address: async () => clone(HWD),
    client_error: async () => ({ok:true}),
    reset_settings: async () => clone(HWD),
    finish_onboard: async () => { (HWD.settings = HWD.settings || {}).onboarded = true;
                                  return clone(HWD); },
    benchmark: async () => clone(HWD.bench || {ok:false}),
    poll: async () => ({seq:0, kind:null, data:{}}),
    check_updates_async: async () => ({ok:true}),
    get_update: async () => ({checking:false, update:null}),
    open_download_page: async () => ({ok:true, url:'https://www.cogcraft.top/#download'}),
    // 自更新：浏览器里没法替换自己，默认回「环境不支持」走手动下载；
    // 具体状态由 window.__SU 驱动，好让界面这些分支能被自动测试覆盖
    update_capability: async () => clone((window.__SU || {}).capability
      || {ok:false, reason:'浏览器预览模式不支持自动更新',
          fallback_url:'https://www.cogcraft.top/#download'}),
    prepare_update: async () => {
      const su = (window.__SU = window.__SU || {});
      su.prepareCalls = (su.prepareCalls || 0) + 1;
      return clone(su.prepareResult || {ok:false, fallback:true,
        reason:'浏览器预览模式不支持自动更新',
        fallback_url:'https://www.cogcraft.top/#download'});
    },
    get_update_progress: async () => clone((window.__SU || {}).progress
      || {phase:'idle', pct:0, ready:false, version:'', error:null}),
    apply_update: async () => {
      const su = (window.__SU = window.__SU || {});
      su.applyCalls = (su.applyCalls || 0) + 1;
      return clone(su.applyResult || {ok:false, error:'浏览器预览模式不支持自动更新'});
    }
  };
}

function mockApi(){
  if (window.__REAL_STATE) return realApi(window.__REAL_STATE);
  const HWD = {
    hardware: {
      cpu:{name:"Intel Core i5-12400F", cores:6, threads:12, mhz:4400},
      ram:{total_mb:16384, avail_mb:10000},
      gpu:[{name:"NVIDIA GeForce GTX 1660 SUPER", vram_mb:6144}],
      disk:{type:"SSD", free_mb:131072}, java:{bits:64, major:21}
    },
    hardware_text: "CPU  Intel Core i5-12400F（12 线程）\n内存 16 GB（可用 9 GB）\n显卡 NVIDIA GeForce GTX 1660 SUPER (6144 MB)\n磁盘 SSD",
    variants: [
      {id:"full", name:"高配版", mods_count:138, min_ram_mb:6144, total_files:138,
       total_bytes:450604655, desc:"完整视觉与体验，光影、远景、界面美化全开"},
      {id:"simple", name:"流畅版", mods_count:120, min_ram_mb:3072, total_files:120,
       total_bytes:421220908, desc:"关掉重负载模组，老电脑和核显也能跑得动"}
    ],
    variant:"full", variant_auto:"full", variant_reason:"硬件满足高配版要求",
    memory:{
      xmx_mb:8192, ceiling_mb:9830, total_ram_mb:16384, avail_ram_mb:10000,
      gc:"G1", warnings:[],
      jvm_args:["-Xms2048M","-Xmx8192M","-XX:+UseG1GC","-XX:+ParallelRefProcEnabled",
        "-XX:MaxGCPauseMillis=200","-XX:G1NewSizePercent=30","-XX:G1HeapRegionSize=8M",
        "-XX:+UseStringDeduplication","-Djava.net.preferIPv4Stack=true"]
    },
    pending:{files:138, bytes:450604655},
    server:{name:"像素工坊", address:"cogcraft.top:25565", auto_join:true},
    manifest_error:null, offline:false,
    java:{path:"C:\\cogcraft\\runtime\\bin\\java.exe", bundled:true},
    settings:{player_name:"Steve", auto_join:true, variant:"full", memory_mb:null,
               onboarded:true, game_dir:"C:\\Users\\Player\\AppData\\Roaming\\cogcraft\\client"},
    modloader:{type:"neoforge", version:"21.1.264"},
    minecraft:{version:"1.21.1"}
  };
  const clone = (o) => JSON.parse(JSON.stringify(o));
  return {
    bootstrap: async () => clone(HWD),
    state: async () => clone(HWD),
    start: async () => ({ok:true}),
    launch: async () => ({ok:true}),
    set_variant: async (v) => { HWD.variant = v; return clone(HWD); },
    set_memory: async (m) => { HWD.memory.xmx_mb = m; return clone(HWD); },
    set_player: async () => clone(HWD),
    set_auto_join: async () => clone(HWD),
    set_server_address: async () => clone(HWD),
    client_error: async () => ({ok:true}),
    reset_settings: async () => clone(HWD),
    finish_onboard: async () => { HWD.settings.onboarded = true; return clone(HWD); },
    benchmark: async () => clone(HWD.bench || {ok:false}),
    poll: async () => ({seq:0, kind:null, data:{}})
  };
}
