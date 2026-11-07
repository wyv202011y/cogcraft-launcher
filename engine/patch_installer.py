#!/usr/bin/env python3
"""
cogcraft 拼装引擎 —— NeoForge 安装器改写器

问题: NeoForge 官方安装器把下载地址写死在 install_profile.json 和 version.json 里,
      共 70 个 url 字段, 分属两个域名:
        maven.neoforged.net/releases/   (51 个) —— 大陆不可达, 全部候选 IP 连不上
        libraries.minecraft.net/        (19 个) —— 能连但很慢, 实测 197 KB/s
      安装器自带的 --mirror 参数实测无效 (只对 Forge 自家域名生效)。

做法: 解包安装器 jar, 把两个 JSON 里的下载前缀改写成国内镜像, 重新打包, 再执行
      --installClient。安装器内部的 10 个 processor 会从改写后的地址下载全部构件。

      ⚠ 不能一律指向单一镜像: 实测(2026-11-07 对 70 个路径逐个 HEAD 探测)
        · 阿里云 = Maven Central 镜像, 覆盖 32/51 个 neoforged 路径 + 18/19 个
          MC 库, 同一文件比 bmclapi 快 55 倍(asm-util: 2.80 MB/s vs 51 KB/s, 且 0 跳转)
        · 但它完全没有 neoforged 自家构件 —— net/neoforged/*、cpw/mods/*、
          net/minecraftforge/srgutils、com/mojang/logging 全部 404
      所以按路径分流: 第三方库走阿里云, neoforged 专属构件仍走 bmclapi。

产物: 一份被改写的安装器 jar, 以及一个可复用的 patch 函数
"""

import zipfile, json, re, shutil, sys, os

OLD_PREFIX = "https://maven.neoforged.net/releases/"
MC_PREFIX = "https://libraries.minecraft.net/"

ALIYUN = "https://maven.aliyun.com/repository/public/"
BMCLAPI = "https://bmclapi2.bangbang93.com/maven/"

# 阿里云(= Maven Central 镜像)确实没有的构件, 必须留在 bmclapi。
# 清单来自逐个 HEAD 实测, 不是猜的:
#   net/neoforged/*        19 个 (installertools / neoform / mergetool / JarJar* /
#                                accesstransformers / fancymodloader / coremods / neoforge)
#   cpw/mods/*              3 个 (bootstraplauncher / modlauncher / securejarhandler)
#   net/minecraftforge/     1 个 (srgutils)
#   com/mojang/logging/     1 个 (MC 官方库域名下的 Mojang 私有包)
BMCLAPI_ONLY = (
    "net/neoforged/",
    "cpw/mods/",
    "net/minecraftforge/",
    "com/mojang/logging/",
)

TARGETS = ("install_profile.json", "version.json")

# 路径字符集: 不含引号和反斜杠, 也不吃空白, 避免把 JSON 结构替换坏
_RE_NEO = re.compile(re.escape(OLD_PREFIX) + r'([^"\s]+)')
_RE_MC = re.compile(re.escape(MC_PREFIX) + r'([^"\s]+)')


def mirror_for(path: str) -> str:
    """按构件路径选择镜像: neoforged 专属走私服, 其余第三方库走阿里云"""
    return (BMCLAPI if path.startswith(BMCLAPI_ONLY) else ALIYUN) + path


def patch_json_text(text: str) -> tuple[str, dict]:
    """替换 JSON 文本里的下载前缀，返回 (新文本, 统计)"""
    stats = {"aliyun": 0, "bmclapi": 0}

    def _sub(m):
        url = mirror_for(m.group(1))
        stats["aliyun" if url.startswith(ALIYUN) else "bmclapi"] += 1
        return url

    text = _RE_NEO.sub(_sub, text)
    text = _RE_MC.sub(_sub, text)
    return text, stats


def patch_installer(src_jar: str, dst_jar: str) -> dict:
    """生成改写后的安装器"""
    stats = {"files_patched": [], "aliyun": 0, "bmclapi": 0}
    tmp = dst_jar + ".tmp"

    with zipfile.ZipFile(src_jar, "r") as zin, \
         zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)

            if item.filename in TARGETS:
                try:
                    text = data.decode("utf-8")
                    new_text, st = patch_json_text(text)
                    n = st["aliyun"] + st["bmclapi"]
                    if n > 0:
                        json.loads(new_text)          # 校验仍然是合法 JSON
                        data = new_text.encode("utf-8")
                        stats["files_patched"].append(
                            f"{item.filename} (阿里云 {st['aliyun']} / bmclapi {st['bmclapi']})")
                        stats["aliyun"] += st["aliyun"]
                        stats["bmclapi"] += st["bmclapi"]
                except Exception as e:
                    stats["files_patched"].append(f"{item.filename} 跳过: {e}")

            zout.writestr(item, data)

    os.replace(tmp, dst_jar)
    stats["dst_size"] = os.path.getsize(dst_jar)
    return stats


if __name__ == "__main__":
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else src.replace(".jar", "-cn.jar")
    st = patch_installer(src, dst)
    print(f"源: {src}  ({os.path.getsize(src)} B)")
    print(f"产物: {dst}  ({st['dst_size']} B)")
    print(f"替换总数: {st['aliyun'] + st['bmclapi']}"
          f"  (阿里云 {st['aliyun']} / bmclapi {st['bmclapi']})")
    for f in st["files_patched"]:
        print(f"  - {f}")
