# Codex-Switch

> **Codex Desktop 对话投影修复与多账号无感切换工具箱**  
> 一键修复 Codex 历史记录卡死/空白，安全轮换多账号凭证，自动清除内嵌浏览器残留状态，支持本地单机与远程 SSH 协同。

---

## 🌟 核心解决的问题

在使用 OpenAI Codex Desktop 或 CLI 工具时，经常会遇到以下两类典型故障：

### 1. 对话历史卡死 / 不显示 / 报错丢失 (Projection Wedge)
* **故障现象**：Codex 窗口中某个会话无法继续加载、对话记录丢失或提示 `final paginated rollout record is missing an ordinal`。
* **技术原理**：Codex 权威的会话数据保存在 `~/.codex/sessions/**/rollout-*.jsonl`，而桌面端 UI 读取的是其衍生的 SQLite 投影缓存 `~/.codex/thread_history_1.sqlite`。当客户端遇到网络异常截断、历史回滚或元数据序号冲突时，SQLite 中的字节/序号游标会发生死锁。
* **解决方案**：本项目内置专门针对游标的数学化修复算法（覆盖 `dup_skip`、`ordinal_advance`、`catch_up`、`ordinal_backfill` 4 种典型损坏形态），在保留已有记录的前提下校准游标与回填序号，**无需暴力删库即可毫秒级无损修复**。

### 2. 多账号切换失效 / 提示「没有 Work 权限」
* **故障现象**：手动替换 `auth.json` 后，Codex Desktop 经常自动还原旧账号，或者打开网页侧边栏时报错 `You don't have access to Work yet`。
* **技术原理**：
  1. Codex Desktop 会定期轮换 `refresh_token`，若未在切换前将当前最新凭证存回快照，切换回来时原凭证将彻底作废；
  2. 桌面内嵌的 Chromium WebView 会在 `browser-sidebar-page-states.json` 及 LocalStorage 中缓存 `?surface=work` 状态，切换到个人账号后仍然尝试以团队模式访问。
* **解决方案**：
  * 切号前自动回写实时 `refresh_token` 保持凭证新鲜；
  * 自动清洗 WebView 侧边栏的残留页面与特定 Cookie；
  * 提供可视化的账号选择界面，并在切号前通过后端接口探活会话有效性（避免因 401 凭据失效引起困惑）。

### 3. Windows 无黑框静默运行
* 使用自编译的微型 C# WinExe 启动器（[`CodexRepair.exe`](CodexRepair.exe)）承载后台流程，彻底避免普通 `.bat` / `.ps1` 启动时一闪而过的控制台黑框，操作完成后直接唤起 Windows 原生系统提示框。

---

## 🚀 快速开始

### 运行环境
* **操作系统**：Windows 10 / 11
* **依赖**：已安装 Python 3.8+ 并加入系统环境变量 `PATH`

### 一键安装

1. 克隆或下载本仓库至本地（建议放置在不易误删的目录，如 `%LOCALAPPDATA%\CodexRepair`）：
   ```powershell
   git clone https://github.com/AnthonyWithLi/Codex-Switch.git
   cd Codex-Switch
   ```

2. 右键使用 PowerShell 运行或在控制台执行一键安装脚本：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
   > 脚本会自动检测 Python 环境，并在您的桌面上生成 **「修复 Codex 对话投影」** 快捷方式。

### 日常使用方法
1. **完全退出** Codex Desktop 客户端；
2. 双击桌面上的 **「修复 Codex 对话投影」** 快捷方式；
3. 在弹出的账号面板中选择需要切换的目标账号（或保持当前账号直接修复）；
4. 修复完成后系统会弹出结果摘要，此时重新打开 Codex Desktop 即可。

---

## ⚙️ 配置文件说明 (`config.json`)

默认情况下，工具完全以**本地单机模式**自动运行，无需任何手动配置。

如果您有特定的自定义需求（例如多机 SSH 同步、指定虚拟环境 Python 等），可参考 [`config.example.json`](config.example.json) 在项目根目录下创建 `config.json`：

```json
{
  "python": "C:\\Users\\YourUser\\miniconda3\\python.exe",
  "remote_host": "your-server-alias",
  "remote_proxy": "http://127.0.0.1:7890",
  "remote_csw": "~/.local/bin/csw",
  "remote_local_csw": "~/.local/bin/codex_local_csw.py",
  "app_user_model_id": "OpenAI.CodexRepair",
  "accounts": [
    {
      "name": "personal",
      "file": "auth.personal.json",
      "account_id": "00000000-0000-0000-0000-000000000000",
      "email": "user@example.com",
      "surface": "personal"
    },
    {
      "name": "team",
      "file": "auth.team.json",
      "account_id": "11111111-1111-1111-1111-111111111111",
      "email": "user@example.com",
      "surface": "work"
    }
  ],
  "aliases": {
    "my-team": "team"
  }
}
```

### 字段说明：
* `python` *(可选)*：自定义 Python 解释器路径。缺省时会自动在系统 PATH 及常见安装路径中探测。
* `remote_host` *(可选)*：远程 Linux 服务器 SSH 别名或 IP。留空时默认纯本地运行。
* `remote_proxy` *(可选)*：远程服务器访问 OpenAI 接口时使用的 HTTP 代理。
* `accounts` *(可选)*：多账号快照定义列表。留空时会自动遍历 `~/.codex/auth.*.json` 自动解析识别。

---

## 📁 目录结构

```text
Codex-Switch/
├── .gitignore                    # 忽略本地私有配置与日志凭据
├── README.md                     # 项目使用与技术说明文档
├── install.ps1                   # 一键初始化与桌面快捷方式部署
├── config.example.json           # 配置文件模板
├── CodexRepair.ico               # 快捷方式图标
├── CodexRepairLauncher.cs        # C# 静默后台启动器源码
├── start_codex_desktop.ps1       # 核心调度脚本（UI 弹窗、环境预检、切号与修复触发）
├── codex_unwedge_projection.py   # 核心 SQLite 投影修复制导引擎
├── codex_local_csw.py            # 本地账号快照管理与会话状态探针
├── switch_server_proxy.ps1       # 辅助：动态探测并切换 SSH 转发代理通道
├── view_codex_thread.py          # 辅助：跨切片完整拼接与离线浏览 Codex 会话
├── check_embedded_python.py      # 开发自测：编译检查嵌入式 Python 脚本语法
└── check_switch_policy.py        # 开发自测：账号强制切换策略自动化测试
```

---

## 🔒 隐私与安全性保障

* 本仓库代码**绝不包含任何硬编码的个人邮箱、账号令牌、UUID 或服务器账号**；
* 所有个人配置与登录凭据均通过本地 `config.json` 及 `~/.codex/` 管理，并通过 `.gitignore` 严格排除，杜绝代码分享时的信息泄露；
* 多设备协同期间，各主机的 Auth Token 均保留在对应机器本地，**绝不会跨 SSH 网络复制传输凭证**。

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。
