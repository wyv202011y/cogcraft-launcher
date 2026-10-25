#!/usr/bin/env python3
"""
cogcraft 拼装引擎 —— NeoForge 安装器改写器

问题: NeoForge 官方安装器把 maven.neoforged.net 写死在 install_profile.json 和
      version.json 里, 而该域名在中国大陆不可达 (DNS 被污染 + 全部候选 IP 连不上)。
      安装器自带的 --mirror 参数实测无效 (只对 Forge 自家域名生效)。

做法: 解包安装器 jar, 把两个 JSON 里的 maven.neoforged.net 前缀改写成 BMCLAPI 镜像,
      重新打包, 再执行 --installClient。安装器内部的 10 个 processor 会从改写后的
      地址下载 installertools / neoform / binarypatcher 等全部构件。

产物: 一份被改写的安装器 jar, 以及一个可复用的 patch 函数
"""

import zipfile, json, shutil, sys, os

OLD_PREFIX = "https://maven.neoforged.net/releases/"
NEW_PREFIX = "https://bmclapi2.bangbang93.com/maven/"

TARGETS = ("install_profile.json", "version.json")


def patch_json_text(text: str) -> tuple[str, int]:
    """替换 JSON 文本里的域名前缀，返回 (新文本, 替换次数)"""
    n = text.count(OLD_PREFIX)
    return text.replace(OLD_PREFIX, NEW_PREFIX), n


def patch_installer(src_jar: str, dst_jar: str) -> dict:
    """生成改写后的安装器"""
    stats = {"files_patched": [], "replacements": 0}
    tmp = dst_jar + ".tmp"

    with zipfile.ZipFile(src_jar, "r") as zin, \
         zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)

            if item.filename in TARGETS:
                try:
                    text = data.decode("utf-8")
                    new_text, n = patch_json_text(text)
                    if n > 0:
                        json.loads(new_text)          # 校验仍然是合法 JSON
                        data = new_text.encode("utf-8")
                        stats["files_patched"].append(f"{item.filename} ({n} 处)")
                        stats["replacements"] += n
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
    print(f"替换总数: {st['replacements']}")
    for f in st["files_patched"]:
        print(f"  - {f}")
