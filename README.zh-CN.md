<p align="center">
  <img src="branding/logo.svg" alt="Btrfs Harbor" width="560">
</p>

<h1 align="center">Btrfs Harbor</h1>

<p align="center">
  <strong>⚓ Snapshots in. Systems back.</strong><br>
  面向 Linux 的原生 Btrfs 备份、验证、恢复与系统复制工具。
</p>

<p align="center">
  <a href="https://github.com/GodsQuantum/btrfs-harbor/actions/workflows/harbor.yml"><img alt="Harbor CI" src="https://github.com/GodsQuantum/btrfs-harbor/actions/workflows/harbor.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-3dd7cf"></a>
  <img alt="Linux" src="https://img.shields.io/badge/platform-Linux-0B1622">
  <img alt="Btrfs" src="https://img.shields.io/badge/filesystem-Btrfs-58D6C5">
  <img alt="Tauri" src="https://img.shields.io/badge/desktop-Tauri%202-24C8DB">
  <img alt="CachyOS" src="https://img.shields.io/badge/CachyOS-ready-00A3FF">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="docs/assets/screenshots/overview.png" width="100%" alt="使用通用演示数据的 Btrfs Harbor 总览">
</p>

Btrfs Harbor 把**本机 Btrfs/Snapper 快照变成真正的异机备份**。它在一个原生桌面应用中整合了受保护的目标挂载、完整 + 增量 Btrfs send、校验、systemd 定时、分阶段恢复以及安全的 Proxmox LXC 系统复制。

同一块磁盘上的本地快照很有用，但它**不是**异机备份。Harbor 会始终明确区分这两种状态。

## ✨ 为什么选择 Harbor？

- **真正的完整 + 增量 Btrfs 备份** — 保留快照父链，而不是每次完整复制。
- **NFS / SMB / 本地 / SSH 目标** — 支持普通文件系统上的 raw stream。
- **不会静默回退到本地目录** — 写入前验证网络挂载身份。
- **内置验证** — 备份后可执行 checksum 校验。
- **原生持久化计划任务** — 每个 profile 使用独立 systemd service/timer。
- **Recovery Kit** — 在备份旁保存拓扑、软件包、Snapper 与启动信息。
- **分阶段恢复** — 先恢复和验证 staging，再考虑激活。
- **Proxmox LXC 复制** — 对已停止容器使用原生 snapshot/rollback。
- **最小权限桌面架构** — 只读 D-Bus agent + 固定 privileged helper，不提供通用 root shell。
- **English / Français / 简体中文** — UI 与文档均支持三种语言。
- **不需要 Docker runtime** — Harbor 是原生 Linux 桌面应用。

## 📦 下载与安装

最简单的试用方式是 **Portable AppImage**。它保持便携：启动时不会安装 Harbor。无需系统安装即可检测 Btrfs/Snapper、选择来源与目标、验证目标、执行**立即备份**、浏览已备份快照并进行分阶段恢复。需要 Btrfs 特权时才通过 polkit 提权。

手动备份正在运行时关闭 Harbor 窗口，Harbor 会提示备份将在**后台继续**，隐藏窗口并保留一个临时**系统托盘图标**。托盘菜单可以重新打开 Harbor 或**停止当前备份**；备份结束后托盘图标会自动消失。

只有在需要关闭 AppImage 后仍持续运行的**自动/后台备份**时才安装 Harbor，例如 systemd 计划任务、开机集成或“每次 Snapper 快照后”触发。

Release 提供：

- **Portable AppImage** — 无需安装即可手动备份和恢复。
- **通用 `.run` 安装器** — x86_64 glibc + systemd Linux，用于持久自动化。
- **DEB** — Debian / Ubuntu 及衍生发行版。
- **RPM** — Fedora / RHEL 系 / openSUSE。
- **SHA256SUMS** — 所有 Linux release 文件的校验值。

通用安装器：

```bash
curl -LO https://github.com/GodsQuantum/BTRFS-Harbor/releases/download/v0.2.4/BTRFS-Harbor-0.2.4-linux-x86_64.run
chmod +x BTRFS-Harbor-0.2.4-linux-x86_64.run
./BTRFS-Harbor-0.2.4-linux-x86_64.run
```

如果系统已经安装兼容的 **`btrfs-backup-ng` (>= 0.9.12)**，Harbor 会优先使用它；否则使用 Harbor 自带的兼容引擎。Harbor 不再替换或与用户安装的 upstream 引擎冲突。

## 🚀 在 CachyOS / Arch Linux 上从源码构建

推荐直接从 Git 仓库安装：

```bash
git clone https://github.com/GodsQuantum/btrfs-harbor.git
cd btrfs-harbor
./install-cachyos.sh
```

安装脚本会：

1. 检查当前系统是否使用 pacman；
2. 安装标准 Arch 构建依赖；
3. 以普通用户通过 `makepkg` 构建仓库中的 `PKGBUILD`；
4. 使用 pacman 安装生成的软件包；
5. 启用原生 `btrfs-harbor-agent.service`；
6. 删除临时构建目录。

安装后可从应用菜单启动 **Btrfs Harbor**，或运行：

```bash
btrfs-harbor
```

> 特权操作通过 polkit 完成。不要用 root 用户启动桌面应用。

## 🛟 第一次备份

1. 打开 Harbor。它会识别当前机器并检测 **Btrfs 子卷**、Snapper 配置以及可发送的只读快照。
2. 在“要备份的内容”中确认持久化子卷。界面优先显示易懂名称，`@`、挂载路径和 Snapper 细节作为辅助信息。
3. 选择目标：
   - **文件夹** — 只选一个目录并点击**验证**；Harbor 在内部自动识别本地/NFS/SMB。
   - **SSH 服务器** — 填写主机、用户、端口和远程目录，然后测试连接。
4. 点击**立即备份**。Portable AppImage 无需安装 Harbor 即可执行。
5. 若需要自动/后台备份，再选择频率并点击**安装 Harbor 以启用自动备份**。
6. 确认第一次发送达到 **BACKED UP** 和 **VERIFIED**。
7. 打开**恢复**，选择真实备份快照并进行 staging 恢复测试。

首次启动不会自动显示演示 IP、最近目录、机器名或私人路径。

## 🧭 状态含义

| 状态 | 含义 |
| --- | --- |
| **LOCAL** | 快照只存在于源机器 |
| **BACKED UP** | 目标中已经存在对应 stream |
| **VERIFIED** | 存储的 raw stream 已通过验证 |
| **FULL** | 独立完整 send |
| **INCREMENTAL** | 使用有效父链的增量 send |
| **RESTORE TESTED** | staging 恢复测试已经成功 |

## ♻️ 恢复快照

**恢复**页会读取真实备份仓库，并按时间从新到旧列出可用快照。选择子卷、目标和精确快照，然后恢复到独立的 **Btrfs staging** 位置；Harbor 会验证接收到的数据。

- 非 root 子卷可以在当前系统运行时恢复到 staging。
- Harbor 不会直接覆盖正在运行的 `/`。系统/root 恢复必须进入 **rescue/live ISO** 流程并结合 Recovery Kit。
- Recovery Kit 保存系统拓扑、`fstab`/`crypttab`、Snapper 配置、软件包清单以及 boot/initramfs 信息。
- 之后仍可使用 Btrfs Assistant 检查或回滚本地 Snapper 快照，但接收 Harbor 的异机备份并不依赖它。

Btrfs 快照不会自动包含 EFI 分区、分区表或所选子卷之外的机密数据，因此 Harbor 显示恢复覆盖范围，而不会错误承诺“整机一键恢复”。

## 🧬 System Replica

Harbor 可以把 Linux userspace 迁移到另一目标，但不会假设硬件状态可以直接复制。

- **物理机** — userspace + 明确的 boot/hardware 协调。
- **VM** — userspace + 虚拟硬件适配。
- **LXC** — 仅 userspace；排除物理 EFI 与内核状态。

对于已停止的 Proxmox LXC，Harbor 可以先创建原生 PVE rollback snapshot，挂载目标 rootfs，保留网络与 hostname，遵守容器真实 UID/GID namespace，重置 machine-id 和 SSH host keys，并在部署失败时自动 rollback。

## 🛡️ 安全模型

### 不允许静默本地回退

对于 NFS/SMB，Harbor 同时保存预期挂载点和 server/share。备份前 helper 会检查实时 mount table；生成的 engine 配置还会使用 `require_mount` 作为第二层保护。

### 不提供通用 root shell

桌面应用保持非特权运行。只读数据来自受限的系统 D-Bus 服务；修改操作只能通过经过验证的 Harbor 固定命令和 polkit 执行。

### 分阶段恢复

```text
plan → verify chain → receive into staging → verify staging → reconcile → activate
```

Harbor 拒绝直接覆盖正在运行的 root 文件系统。

### Recovery Kit 不包含凭据

Recovery Kit 保存系统意图和拓扑，但不会复制 secret credential 文件。

## 🏗️ 架构

```mermaid
flowchart LR
    UI["Tauri 2 + SvelteKit"] -->|只读 D-Bus| Agent["Rust harbor-agent"]
    UI -->|polkit + 固定 helper| Ctl["Rust btrfs-harborctl"]
    Agent --> Engine["btrfs-backup-ng engine"]
    Ctl --> Engine
    Ctl --> Systemd["systemd timers"]
    Engine --> Btrfs["Btrfs / Snapper"]
    Engine --> Raw["raw:// streams"]
    Raw --> Target["NFS / SMB / 本地文件系统"]
    Recovery["Rust harbor-recovery"] --> Raw
```

Harbor 基于 [berrym/btrfs-backup-ng](https://github.com/berrym/btrfs-backup-ng)。成熟的 Python engine 继续负责 send/receive、retention、raw metadata、locking 与 restore；Harbor 在此基础上增加 Rust control plane 和原生桌面 UI。

## ⚙️ 示例配置

完全通用的示例位于 [examples/harbor.toml](examples/harbor.toml)。

实际配置文件：

```text
/etc/btrfs-harbor/harbor.toml
```

生成的 engine profile：

```text
/var/lib/btrfs-harbor/generated/
```

## 🧪 开发

```bash
uv sync --frozen --extra test --extra dev
uv run --frozen pytest -q

cd harbor
cargo test --workspace --exclude btrfs-harbor-desktop --locked

cd desktop
pnpm install --frozen-lockfile
pnpm test
pnpm check
pnpm build
```

## 📦 项目状态

Btrfs Harbor 当前是早期公开 **v0.2**。核心备份、验证、staging 恢复以及 stopped-target LXC replication 流程已经在一次性测试环境中完成验证。

**不要让未稳定的备份工具成为重要数据的唯一副本。** 保留另一份备份，并先证明恢复流程真正可用。

## 🤝 Upstream 继承

Btrfs Harbor 基于 Michael Berry 和贡献者维护的 **btrfs-backup-ng**。继承的 engine 继续使用 MIT 许可证，并保留 upstream 历史。原始 upstream README 位于 [docs/upstream/UPSTREAM_ENGINE_README.md](docs/upstream/UPSTREAM_ENGINE_README.md)。

## 📄 许可证

MIT — 参见 [LICENSE](LICENSE)。
