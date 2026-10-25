# cogcraft 启动器

像素工坊（cogcraft）专属的 Minecraft 客户端启动器。

玩家装好这个启动器后，它会从服务器取一份 JSON 清单，自动从各个官方源
（Modrinth / CurseForge / FTB）下载并拼装出完整客户端，然后直接进服。
不提供多版本管理 —— 只装本服需要的东西。

**本仓库不含任何模组本体**，全部文件都从各模组作者的官方发布源下载。

## 特性

- **一条命令进服**：首次安装后只需点「开始游戏」
- **自带 Java 21**，玩家不用自己装 Java，也就不会装错版本
- **下载源四分法**：Modrinth 优先 → CurseForge → FTB → 服务器自托管，自托管比例压到 1.2%
- **性能微基准**：用游戏同款 Java 实测这台机器跑 MC 有多快，据此自动选高配版 / 流畅版。
  不查 CPU 型号表 —— 同型号不同散热能差一倍，实测比查表准
- **崩溃自动归类**：把 OOM / 模组冲突 / 图形 / Mixin / JVM 分门别类，
  提取疑似元凶模组，方便定位

## 结构

| 路径 | 作用 |
|---|---|
| `app.py` | pywebview 外壳 + `Api` 桥（界面 ↔ 引擎） |
| `engine/net.py` | 统一下载层：IPv4 优先、原子写、退避重试、连接池、限流退避 |
| `engine/manifest.py` | 清单拉取与校验 |
| `engine/pipeline.py` | 六步拼装流水线 |
| `engine/hardware.py` | 跨平台硬件探测 |
| `engine/jvm.py` | 内存档位与 JVM 参数（Aikar G1） |
| `engine/launch.py` | 版本合并与启动命令拼装 |
| `engine/bench.py` + `bench/Bench.java` | 性能微基准 |
| `engine/crash.py` | 崩溃收集与归类 |
| `ui/index.html` | 界面（单文件，无构建链） |
| `tools/build_manifest.py` | 生成分发清单 |

## 本地运行

```bash
pip install pywebview
python3 app.py
```

只调界面的话，直接用浏览器打开 `ui/index.html`（内置示例数据）：
加 `?onboard=1` 看首次引导，加 `?panel=1` 看设置面板。

## 打包

`.github/workflows/build-macos.yml` 在 GitHub Actions 的 macOS runner 上构建
（PyInstaller 不支持交叉编译，Linux 上打不出 macOS 的 `.app`）。

产物 `cogcraft-macos-arm64.zip` / `-x64.zip` 内含自带 JRE 21。

> 未做代码签名，首次打开需右键 →「打开」。

## 许可证

**GNU General Public License v3.0**，全文见 [LICENSE](LICENSE)。

### 关于 HMCL

本项目为**独立实现**，未使用 [HMCL](https://github.com/HMCL-dev/HMCL) 的任何源码
（HMCL 是 Kotlin/Java 项目，本项目引擎为 Python）。HMCL 同样以 GPL-3.0 发布，
本项目也采用 GPL-3.0，因此在许可上完全兼容。

### 免责声明

本项目与 Mojang Studios、Microsoft 均无关联。Minecraft 是 Mojang Studios 的商标。
所有第三方模组的版权归其各自作者所有，本项目不重新分发任何模组文件。
