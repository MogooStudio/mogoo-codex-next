# Mogoo Codex Next

[English](README.md) | **简体中文**

---

![Mogoo Codex Next 下一步建议面板](docs/images/panel-preview.jpg)

*使用合成对话的离线开发预览。面板目前使用中文界面。*

Mogoo Codex Next 在 Codex 当前聊天的右侧面板中生成下一步建议。它根据对话上下文提出相关后续，你可以将建议追加到草稿，编辑后自行发送。

插件使用独立的模型设置。打开面板自动生成，关闭面板暂停新请求，发送内容始终由你决定。

## 功能

- **当前聊天的下一步建议**：结合原始目标和最近已完成的回复，生成最多 3 条建议，理由可按需展开。
- **追加草稿**：采纳只追加到输入框，保留已有内容，由你决定何时发送；也可复制建议。
- **独立模型配置**：服务来源、模型、推理强度和服务档位均与当前聊天分开设置。
- **官方与中转连接**：复用 Codex CLI 登录，或在面板填写 Responses API 地址和 API Key；提供 AI Code With 预设及自定义地址。
- **可搜索的模型目录**：展开完整列表，按名称或 ID 搜索，支持键盘选择及手动输入，保存后才生效。
- **自动与手动生成**：面板打开时自动生成，可手动刷新；对话状态未变时复用有效结果。
- **三套外观**：石墨、批注和聚焦，建议、设置及用量页面使用同一外观。
- **用量记录**：查看本聊天最近 20 次生成尝试、已知 token 用量和固定错误分类。

## 安装

需要 Codex 继承的 `PATH` 能找到 **Python 3.10+**、可正常启动且支持插件命令的 **Codex CLI**，以及支持 **MCP Apps 和聊天侧面板的 Codex 桌面端**。官方来源还需要 CLI 已登录。开发和原生验证主要在 Windows 上进行，面板直接保存 API Key 当前依赖 Windows 账户加密（DPAPI）。

### 复制下面这段话发给 Codex

```text
帮我从 GitHub 仓库 MogooStudio/mogoo-codex-next 安装完整插件，使用仓库内的 mogoo-next 插件市场和 Codex 官方插件 CLI。先检查 Python 3.10+ 和 Codex CLI 是否可用，说明打开当前聊天“下一步建议”面板前需要的刷新或重启步骤。可选 Hook 如需我在界面中审阅并信任，请告诉我怎么操作。让我在面板设置中自行选择模型，不要在聊天中索取 API Key，也不要发送任何草稿。
```

### 命令行安装

```sh
codex plugin marketplace add MogooStudio/mogoo-codex-next --ref main
codex plugin add mogoo-codex-next@mogoo-next
```

安装内容包括聊天面板 MCP 服务、Codex 技能及可选 Stop Hook。随后按 Codex 提示刷新插件或重新打开聊天；如需重启，先完成正在进行的工作。

安装和更新不会自动信任 Hook，需在 Codex 官方入口审阅并信任。未信任时面板仍可通过只读轮询发现已完成的回复。

### 后续更新

```sh
codex plugin marketplace upgrade mogoo-next
codex plugin add mogoo-codex-next@mogoo-next
```

随后按 Codex 提示刷新，并在提示时重新审阅变化的 Hook。插件设置与外观保存在安装目录之外，版本变化见[更新记录](CHANGELOG.zh.md)。

如果之前安装过本地开发来源，切换前先通过官方 CLI 卸载旧来源，再执行上方安装命令：

```sh
codex plugin remove mogoo-codex-next@mogoo-codex-next-local
```

这样可避免同时加载两份插件。插件数据目录与安装文件分开保存。

## 使用

1. 在当前聊天右侧打开 **下一步建议**，也可让 Codex：`打开当前聊天的下一步建议面板。`
2. 点击 **设置**。默认未选择模型，配置前不会发起模型请求。
3. 选择 **官方** 复用 CLI 登录，或选择 **中转 · API** 填写 Responses API 基础地址和 API Key；中转可选 AI Code With 或自定义地址。
4. 读取模型目录或手动填写模型 ID，选择模型和服务实际支持的推理强度、档位，再保存设置。
5. 当前回复完成后，打开的面板可自动生成建议。展开理由、复制内容，或点击 **填入草稿** 追加到输入框。
6. 自行编辑并发送。点击 **刷新建议** 可重新生成，关闭自动建议可暂停自动生成，关闭面板则停止新请求。

没有合理后续时，空结果是正常情况；失败不会循环重试。切回应用时会先确认当前聊天状态，再允许填入草稿。

![可搜索的模型目录](docs/images/model-picker-preview.jpg)

*0.1.13 模型选择器的离线预览。目录条目为合成数据，不代表服务商实际可用模型。*

中转 Key 保存后，输入框留空表示沿用相同地址的已保存 Key，填写新值可替换，勾选明确清除选项并保存可删除。更换 API 地址时需填写新地址对应的 Key。真实 Key 只在面板填写，不要放入聊天或命令参数。

## 开发

运行时仅使用 Python 标准库和静态 HTML/CSS/JavaScript，无需 npm 安装或前端构建。Node.js 只用于前端测试，需支持 `--test-isolation=none`。

```sh
git clone https://github.com/MogooStudio/mogoo-codex-next.git
cd mogoo-codex-next
python scripts/run.py doctor
python scripts/run.py doctor --probe-cli
python scripts/test.py
node --test --test-isolation=none tests/test_panel.mjs
python scripts/dev_preview.py --port 8765
```

`doctor --probe-cli` 只启动 CLI 读取模型目录，不发起推理。打开[本地开发预览](http://127.0.0.1:8765)可验证面板交互：使用临时目录、合成对话和固定输出，不读取真实聊天、不调用真实模型。

生成本机 ZIP 和可安装目录：

```sh
python scripts/package.py --output dist/mogoo-codex-next-local
```

每次打包请使用尚不存在的输出路径。portable MCP 声明使用 `PATH` 中的 `python`，包内旧格式 `.mcp.json` 使用打包机器的 Python 绝对路径，移动到其他电脑时应重新打包。

| 路径 | 用途 |
| --- | --- |
| `.agents/plugins/marketplace.json` | 仓库内插件市场和插件条目 |
| `.codex-plugin/plugin.json` | Codex 插件元数据与扩展声明 |
| `src/mogoo_codex_next/` | MCP 服务、上下文、生成、设置、凭据和存储 |
| `ui/` | 面板页面、样式及宿主桥接 |
| `skills/`、`hooks/` | Codex 技能及可选回复完成通知 |
| `scripts/`、`tests/` | 运行入口、预览、打包及离线检查 |
| `docs/` | 验证记录和截图 |

## 说明

- **调用与费用**：每次生成是一次独立模型请求，按所选账号或服务商规则计量，不沿用 Codex 官方 Composer predictions 的免额度规则。token 记录不等同账单或剩余额度，未知用量不表示零消耗。
- **对话隐私**：所选服务会收到有限的原始目标、最近用户消息和最终回复摘录，不包含工具原始输出、推理过程或认证文件。请为该聊天选择你认可的服务商。
- **本地数据**：配置、账户加密凭据、外观、租约、建议与用量位于 `<CODEX_HOME 或 ~/.codex>/mogoo-codex-next/`；可用 `MOGOO_CODEX_NEXT_HOME` 或 `python scripts/run.py --data-dir <路径> ...` 覆盖。公开设置接口不返回实际 Key。
- **生成范围**：缺少宿主提供的聊天标识时停止生成，仅处理非活动、非归档聊天的最新已完成轮次。建议模型不执行建议、不调用工具、不恢复原聊天任务。
- **关闭与缓存**：关闭或隐藏后不启动新请求，已发出的请求仍可能计量。租约过期、新轮次或配置变化会使旧结果失效；同一状态复用有效结果。
- **兼容性**：填入草稿使用宿主的 `send:false` 扩展，不支持时提供复制。MCP Apps、聊天标识及草稿能力取决于宿主版本；桥接使用 MCP Apps `2026-01-26` 及包含 `2025-11-25` 在内的兼容 MCP stdio 版本。
- **验收范围**：离线测试和合成预览不能替代原生宿主、真实服务调用验收。各版本证据与待验项见[验证记录](docs/验证记录.md)和 [0.1.13 验收说明](docs/0.1.13-模型选择器修复验收.md)。Windows 沙箱若限制临时目录访问，应在正常用户终端运行离线测试。

## License

[MIT](LICENSE) © 2026 MogooStudio。第三方参考与保留声明见 [THIRD_PARTY.md](THIRD_PARTY.md)。
