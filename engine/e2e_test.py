#!/usr/bin/env python3
"""端到端测试：对着线上真实清单做完整文件同步。"""
import sys, json, os, shutil, tempfile, time, hashlib

R = "/home/server/Raid_5/个人文件夹/王樱博/启动器"
sys.path.insert(0, R + "/engine")
import manifest as M
import pipeline as P

LOG = "/home/server/.hermes/cache/scratch/e2e_test.log"

def out(msg):
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

open(LOG, "w").close()

out("═══ 1. 从线上真实拉清单 ═══")
t = time.time()
online = M.fetch("https://www.cogcraft.top/client/manifest.json")
online = M.validate(online)
out(f"  拉取+校验通过 {time.time()-t:.2f}s，离线缓存={online.get('_offline', False)}")

test_mf = json.loads(json.dumps(online))
for v in test_mf["variants"]:
    v["files"] = [f for f in v["files"] if f["source"] == "self"]
out(f"  测试集 full={len(test_mf['variants'][0]['files'])} simple={len(test_mf['variants'][1]['files'])}")

GD = os.path.join(tempfile.gettempdir(), "cog_e2e")
shutil.rmtree(GD, ignore_errors=True)
os.makedirs(GD, exist_ok=True)

def prog(step, pct, msg):
    if msg and (int(pct * 100) % 20 == 0 or pct >= 1.0):
        out(f"  [{step:<10}] {pct*100:5.1f}%  {msg}")

out("\n═══ 2. 首次安装（目录为空）═══")
pipe = P.Pipeline(GD, test_mf, "full", "java", prog)
t = time.time()
try:
    pipe.step_files()
    out(f"  ✅ 完成，{time.time()-t:.1f} 秒")
except Exception as e:
    out(f"  ❌ {str(e)[:500]}")

v = test_mf["variants"][0]
out(f"\n═══ 3. 独立复核 {len(v['files'])} 个文件 ═══")
bad = []
for f in v["files"]:
    p = os.path.join(GD, f["path"].replace("/", os.sep))
    if not os.path.exists(p):
        bad.append(("缺失", f["path"]))
    elif hashlib.sha1(open(p, "rb").read()).hexdigest() != f["sha1"]:
        bad.append(("哈希不符", f["path"]))
out(f"  {'✅ 全部存在且 sha1 正确' if not bad else f'❌ {len(bad)} 个问题: {bad[:3]}'}")
tot = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(GD) for f in fs)
out(f"  落地 {tot/1048576:.1f} MB")

out("\n═══ 4. 二次运行（应全部跳过）═══")
pipe2 = P.Pipeline(GD, test_mf, "full", "java", prog)
t = time.time()
pipe2.step_files()
out(f"  耗时 {time.time()-t:.2f} 秒")

out("\n═══ 5. full → simple 切换 ═══")
pipe3 = P.Pipeline(GD, test_mf, "simple", "java", prog)
pipe3.step_files()
trash = os.path.join(GD, ".cogcraft", "removed")
n_removed = sum(len(fs) for _, _, fs in os.walk(trash)) if os.path.isdir(trash) else 0
expect_mods = len([f for f in test_mf["variants"][1]["files"]
                   if f["path"].startswith("mods/")])
mods_left = len(os.listdir(os.path.join(GD, "mods"))) if os.path.isdir(os.path.join(GD, "mods")) else 0
out(f"  移入暂存区 {n_removed} 个，剩余模组 {mods_left} 个（测试集内期望 {expect_mods}）")
out(f"  {'✅ 切换正确' if mods_left == expect_mods else '❌ 数量不符'}")

out("\n═══ 6. 配置文件是否按版本区分 ═══")
for vid in ("full", "simple"):
    p = os.path.join(GD, "config", "sodium-options.json")
    out(f"  当前 config/sodium-options.json 存在={os.path.exists(p)}")

shutil.rmtree(GD, ignore_errors=True)
out("\n  测试目录已清理")
out("__DONE__")
