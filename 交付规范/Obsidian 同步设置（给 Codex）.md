---
author: Claude
created: 2026-10-13
tags: [Claude, Obsidian, 同步]
---

> 用法：把下面「给 Codex 的任务」整段复制给 Mac 上的 Codex，它会帮你设置好。只需要设置一次。

## 这套同步怎么工作

1. Claude 在云端把文档推送到 GitHub 私有仓库 `luoxuran1982/Claude`。GitHub 自动把所有项目的文档汇总到一个叫 `obsidian` 的分支。
2. Mac 上的 Obsidian 用 Obsidian Git 插件，每 10 分钟拉取一次这个分支，放进 vault 的 `Claude/` 文件夹。
3. 手机通过 Obsidian 官方同步从 Mac 收到更新，手机上什么都不用装。

前提是 Mac 上的 Obsidian 开着，因为插件只在 Obsidian 运行时才拉取。

你要动手的只有三处：Codex 让你登录 GitHub 时按提示操作；在 Obsidian 里点一下启用插件；可能要在手机上关掉一个开关（见第 6 步）。

## 给 Codex 的任务

请在这台 Mac 上设置：让 Obsidian 自动从 GitHub 拉取 Claude 写的项目文档。

**背景**

- 仓库：`https://github.com/luoxuran1982/Claude`（私有）。只需要其中的 `obsidian` 分支，只读。
- vault：包含 `Codex/应用开发交付要求.md` 的那个 Obsidian vault，大概在 `/Users/你的用户名/Documents/Obsidian Vault`，请先确认。
- 这个 vault 通过 Obsidian Sync 同步到手机。

**步骤**

1. 确认 `git` 可用；没有就安装 Xcode 命令行工具。
2. 设置 GitHub 只读访问。优先用浏览器授权登录（例如 Git Credential Manager）。如果不方便，就引导我创建一个 fine-grained personal access token：只授权 `luoxuran1982/Claude` 这一个仓库，权限只给 Contents: Read-only，有效期选最长。凭据存进 macOS 钥匙串，不要写进任何文件、配置或 URL。
3. 把 `obsidian` 分支 clone 到 vault 根目录下的 `Claude/` 文件夹：
   `git clone --branch obsidian --single-branch https://github.com/luoxuran1982/Claude.git "<vault>/Claude"`
   如果 `Claude/` 已经存在，先停下来问我。
4. 在 Obsidian 里安装并启用社区插件「Obsidian Git」。需要我在界面上点的地方，告诉我怎么点。然后这样设置：
   - Advanced → Custom base path (Git repository path)：`Claude`
   - Pull on startup：打开
   - Auto pull interval：10 分钟
   - 所有自动提交、自动推送（如 Auto commit-and-sync interval）：关闭。这个仓库只拉不推。
5. 验证：在 Obsidian 里执行一次「Git: Pull」，确认 `Claude/00 目录.md` 能打开、里面的项目链接能点开；再确认 `.git` 文件夹没有出现在 Obsidian 的文件列表里。
6. 手机端：如果 Obsidian Sync 开了「同步社区插件」，手机上也会出现这个插件，但手机上没有 Git 仓库，会报错。请提醒我在手机上打开 Obsidian Git 的设置，勾选「Disable on this device」（只在这台设备停用，不会同步到别处）。

**注意**

- 不要改动 vault 里 `Claude/` 以外的任何内容，尤其是 `Codex/` 文件夹。
- 不要在 `Claude/` 里新建或修改笔记。这个文件夹是 GitHub 的镜像，内容由 GitHub 自动生成。
- 完成后告诉我：vault 路径、clone 的位置、插件的各项设置值、验证结果，以及以后怎么停止同步（在 Obsidian 里关闭插件，再删除 `Claude/` 文件夹）。

## 以后可能遇到的情况

- **新文档没出现**：先确认 Mac 上的 Obsidian 开着；也可以手动执行「Git: Pull」。GitHub 汇总通常在 Claude 推送后一两分钟内完成。
- **插件报登录失败**：token 可能过期了，让 Codex 换一个新的。
- **`Claude/` 里的文件被我改了，拉取失败**：让 Codex 在 `Claude/` 里执行 `git checkout -- .` 恢复原样，再拉取。
