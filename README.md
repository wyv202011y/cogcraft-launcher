# cogcraft 启动器

像素工坊（cogcraft）专属客户端启动器。玩家下载后从服务器取一份 JSON 清单，
自动从第三方直链下载并拼装完整客户端，直接进入 `cogcraft.top:25565`。

不提供版本管理——只装本服所需的资源。

## 结构

| 路径 | 作用 |
|---|---|
| `app.py` | pywebview 外壳 + `Api` 桥（界面与引擎之间） |
| `engine/` | 拼装引擎：`net` 下载层 · `manifest` 清单 · `pipeline` 流水线 · `hardware` 硬件探测 · `jvm` 内存与参数 · `launch` 启动 · `bench` 性能微基准 |
| `ui/index.html` | 界面（单文件，A 主题） |
| `tools/build_manifest.py` | 生成分发清单 |

## 本地跑（开发）

```bash
pip install pywebview
python3 app.py
```

只界面调试：直接用浏览器打开 `ui/index.html`（内置假数据，加 `?onboard=1` 看引导、
`?panel=1` 看设置面板）。

## 打 mac 包

`.github/workflows/build-macos.yml` 在 GitHub Actions 的 macOS runner 上构建
（PyInstaller 不能交叉编译）。产出 `cogcraft-macos-arm64.zip` 与 `-x64.zip`，
内含自带 JRE 21，无需玩家自己装 Java。

未做代码签名，首次打开需右键 → 打开。
