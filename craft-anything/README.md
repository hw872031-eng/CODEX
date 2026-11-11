# craft-anything（镜像安装）

上游：[storytold/craft-anything](https://github.com/storytold/craft-anything) · 锁定版本 **v0.6.0**（2026-11-09 发布）

这个目录存在的理由：云端环境每次会被重置，把安装包放在自己的 GitHub 账号下，任何一台机器一条命令就能重新装回来，不依赖上游仓库是否还在。

## 一条命令安装（Linux x86_64）

```sh
curl -sSL https://raw.githubusercontent.com/hw872031-eng/CODEX/main/craft-anything/install.sh | sh
```

装到 `~/.local/opt/craft-anything-0.6.0`，命令软链到 `~/.local/bin`：

| 命令 | 用途 |
|---|---|
| `craft-anything` | 桌面界面 |
| `craft-anything-cli` | 命令行（`--version` 可验证安装） |

可选环境变量：`CRAFT_VERSION`（版本）、`CRAFT_DEST`（安装目录）、`CRAFT_BIN`（软链目录）。

脚本先从 `SHA256SUMS.txt` 比对哈希，不一致就中止，不会解压任何东西；已装过同版本会直接跳过下载。

## 已镜像 / 未镜像

| 项 | 状态 |
|---|---|
| `craft-anything-0.6.0-linux-x86_64.tar.gz`（66.2 MB） | 已镜像，sha256 `cbc65972a8eb4c89ed74b99bcf14aa5ea0f7aa9da709def38da79ccacc5e07a0` |
| `SHA256SUMS.txt` | 已镜像，与上游逐字节相同 |
| Windows MSI / portable、macOS dmg、aarch64、AppImage、deb/rpm/flatpak | 未镜像 |

aarch64 会自动回落到上游地址下载。

## 装完还缺一半

它本身不含构建能力。要真正“重建”软件，需要一个已经登录的编程代理（Claude Code 等）；只用 API key 目前只能问答和分析，不能构建。

## 注意

- 重建过程会在这台机器上执行安装脚本（`npm install`、`cargo build` 等），第三方包会运行它自己的安装钩子。只在你信任的环境里跑。
- 镜像只保证“和上游是同一份字节”，不保证上游代码本身的行为。
- 上游的 `SHA256SUMS.txt` 带 GPG 分离签名（`release-signing-key.asc`），这里只做了 sha256 比对。
