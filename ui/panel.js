/* MCP Apps bridge. No host DOM access, external resources, or model calls. */
(function (global) {
  "use strict";

  const POLL_MS = 2000;
  const GENERATING_POLL_MS = 500;
  const RPC_TIMEOUT_MS = 10000;
  const UI = { appInfo: { name: "mogoo-codex-next", version: "0.1.59" } };
  const SKINS = ["graphite", "annotation", "microglow"];
  const UPDATE_PROMPT_PREFIX = "请执行以下两条命令更新插件：";
  const RELAY_PRESETS = { aicodewith: "https://api.aicodewith.ai/chatgpt/v1" };
  const STATES = {
    connecting: ["", "正在连接", "在当前对话中打开面板即可查看建议。"],
    unbound: ["↗", "尚未绑定对话", "请从需要建议的对话右侧打开面板。宿主未提供对话标识时，不会读取其他聊天。"],
    unconfigured: ["◇", "选择生成模型", "为下一步建议选择一个独立模型，也可以使用中转服务。"],
    waiting: ["", "等这轮回复结束", "当前对话完成一轮回复后，这里会出现下一步建议。"],
    syncing: ["", "正在同步对话", "正在确认当前对话的最新状态。"],
    generating: ["", "正在生成建议", "结合最近的对话生成建议，通常需要一点时间。"],
    streaming: ["", "首条建议已到", "正在生成其余建议。"],
    ready: ["", "", ""],
    empty: ["✓", "暂时没有后续建议", "可以继续聊天，或重新生成一组建议。"],
    paused: ["Ⅱ", "自动建议已暂停", "打开自动建议后继续生成，也可以手动刷新这一轮。"],
    error: ["", "暂时无法生成建议", "请检查模型和服务配置，再手动刷新。"],
    refresh_needed: ["", "建议需要刷新", "上次结果已失效，请重新生成一组建议。"],
    hidden: ["Ⅱ", "面板已暂停", "返回面板后会继续同步当前对话。"],
    closed: ["✓", "面板已关闭", "在对话右侧重新打开即可继续。"],
  };

  /** Minimal JSON-RPC transport to the immediate MCP Apps host/sandbox proxy. */
  class Bridge {
    constructor(win) {
      this.win = win;
      this.pending = new Map();
      this.handlers = new Set();
      this.sequence = 0;
      this.closed = false;
      this.onMessage = this.onMessage.bind(this);
      win.addEventListener("message", this.onMessage);
    }
    post(message) {
      if (this.closed || this.win.parent === this.win) throw new Error("请在 Codex 对话右侧打开此面板。");
      this.win.parent.postMessage({ jsonrpc: "2.0", ...message }, "*");
    }
    request(method, params, timeout = RPC_TIMEOUT_MS) {
      if (this.closed) return Promise.reject(new Error("面板连接已关闭。"));
      const id = `next-${++this.sequence}`;
      return new Promise((resolve, reject) => {
        const timer = this.win.setTimeout(() => {
          this.pending.delete(id);
          reject(new Error("连接超时，请重新打开面板。"));
        }, timeout);
        this.pending.set(id, { resolve, reject, timer });
        try { this.post({ id, method, params }); }
        catch (error) { this.win.clearTimeout(timer); this.pending.delete(id); reject(error); }
      });
    }
    notify(method, params = {}) { this.post({ method, params }); }
    reply(id, result = {}) { this.post({ id, result }); }
    subscribe(handler) { this.handlers.add(handler); return () => this.handlers.delete(handler); }
    onMessage(event) {
      if (event.source !== this.win.parent) return;
      const message = event.data;
      if (!message || typeof message !== "object" || message.jsonrpc !== "2.0") return;
      if (!message.method && message.id != null) {
        const call = this.pending.get(message.id);
        if (!call) return;
        this.pending.delete(message.id);
        this.win.clearTimeout(call.timer);
        if (message.error) call.reject(new Error(typeof message.error.message === "string" ? message.error.message : "宿主请求失败。"));
        else call.resolve(message.result);
        return;
      }
      if (typeof message.method === "string") for (const handler of this.handlers) handler(message);
    }
    dispose() {
      this.closed = true;
      this.win.removeEventListener("message", this.onMessage);
      for (const call of this.pending.values()) {
        this.win.clearTimeout(call.timer);
        call.reject(new Error("面板连接已关闭。"));
      }
      this.pending.clear(); this.handlers.clear();
    }
  }

  function isConnectionError(error) {
    const message = typeof error === "string" ? error : error?.message;
    return typeof message === "string" && /\btransport\s+(?:is\s+)?closed\b|^连接超时[，,]|^面板连接已关闭/i.test(message);
  }
  function publicError(error) {
    const message = typeof error === "string" ? error : error instanceof Error ? error.message : "请求失败，请重试。";
    return isConnectionError(error) ? "插件连接已中断，请重新连接；若持续失败，请重启 Codex。" : message;
  }
  function structured(result) {
    if (result?.isError) throw new Error(result.content?.find((item) => item.type === "text")?.text || "插件请求失败。");
    return result?.structuredContent ?? null;
  }
  function supportsDraft(capabilities) {
    const extensions = capabilities?.experimental;
    return !!extensions && Object.prototype.hasOwnProperty.call(extensions, "openai/message") &&
      extensions["openai/message"] !== null && typeof extensions["openai/message"] === "object";
  }
  function draftParams(prompt) {
    return { role: "user", content: [{ type: "text", text: prompt }], _meta: { "openai/message": { target: "active", send: false } } };
  }
  function supportsExecute(capabilities) {
    const text = capabilities?.message?.text;
    return supportsDraft(capabilities) || (text !== null && typeof text === "object");
  }
  function modelEntry(model) {
    if (typeof model === "string") return { id: model, label: model };
    if (!model || typeof model !== "object") return null;
    const id = model.id || model.model || model.slug;
    if (typeof id !== "string" || !id) return null;
    return { id, label: String(model.displayName || model.display_name || model.name || id) };
  }

  function versionIsOlder(value, reference) {
    const valid = version => typeof version === "string" && /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(version);
    if (!valid(value) || !valid(reference)) return false;
    const left = value.split("."); const right = reference.split(".");
    for (let index = 0; index < 3; index++) {
      if (left[index].length !== right[index].length) return left[index].length < right[index].length;
      if (left[index] !== right[index]) return left[index] < right[index];
    }
    return false;
  }

  class Panel {
    constructor(win, doc, bridge = new Bridge(win)) {
      this.win = win; this.doc = doc; this.bridge = bridge;
      this.snapshot = null; this.config = {}; this.hostCapabilities = {}; this.hostContext = {};
      this.initialized = false; this.closed = false; this.busy = false; this.polling = false;
      this.pendingDisplays = []; this.displayCandidate = null; this.displayObserved = new Set();
      this.renderedSuggestions = []; this.cardsActionCapabilities = null;
      this.messageBusy = false; this.executions = new Map(); this.executeButtons = [];
      this.inViewport = true; this.pollTimer = null; this.noticeTimer = null; this.epoch = 0;
      this.settingsReadSequence = 0;
      this.settingsSyncVersion = 0;
      this.settingsDirty = false; this.settingsOpen = false; this.connectionError = ""; this.clearApiKey = false;
      this.catalogEpoch = 0; this.catalogKey = ""; this.catalogLoading = false;
      this.modelEntries = []; this.manualModel = false; this.modelOptionsSignature = "";
      this.suspended = false; this.view = "suggestions"; this.skin = "graphite"; this.skinBusy = false; this.appearanceEpoch = 0; this.viewPositions = {};
      this.pendingAuto = null;
      this.reconnectAttempts = 0; this.refreshRequest = null; this.requestSequence = 0;
      this.refreshNoticeId = null;
      this.updateInfo = null; this.updateBusy = false; this.updateError = "";
      this.uiVersion = UI.appInfo.version; this.backendVersion = "";
      this.updateDrafting = false; this.updateDraftFeedback = ""; this.updateDraftFailed = false;
      this.restoringDefaults = false;
      this.previewSuggestions = []; this.draftButtons = [];
      this.el = {};
      for (const id of ["thread-title", "auto-generate", "refresh", "model-summary", "status", "status-symbol", "status-title", "status-detail", "status-action", "suggestions", "draft-note", "usage", "settings", "settings-toggle", "settings-close", "settings-form", "output-mode", "model", "model-list", "load-models", "models-status", "reasoning-effort", "service-tier", "relay-fields", "base-url", "api-key", "api-key-status", "clear-api-key", "settings-message", "save-settings", "notice", "close-panel", "app", "panel-scroll", "suggestion-header", "view-header", "settings-actions", "view-title", "suggestion-view", "usage-view", "settings-dirty", "check-model", "usage-open", "usage-label", "usage-arrow", "usage-calls", "usage-failures", "usage-records", "skin-graphite", "skin-annotation", "skin-microglow", "discard-dialog", "discard-continue", "discard-confirm"]) this.el[id] = doc.getElementById(id);
      this.unsubscribe = bridge.subscribe((message) => this.onHostMessage(message));
      this.onVisibility = () => this.syncVisibility();
      this.onPageHide = () => this.suspend(true);
      this.onPageShow = () => { if (!this.closed) this.syncVisibility(); };
      doc.addEventListener("visibilitychange", this.onVisibility);
      win.addEventListener("pagehide", this.onPageHide);
      win.addEventListener("pageshow", this.onPageShow);
      if (win.IntersectionObserver) {
        this.observer = new win.IntersectionObserver((entries) => {
          this.inViewport = entries.some((entry) => entry.isIntersecting);
          this.syncVisibility();
        });
        this.observer.observe(doc.documentElement);
      }
      for (const id of ["relay-preset", "relay-preset-field", "model-picker", "model-manual-field"]) this.el[id] = doc.getElementById(id);
      this.el["restore-defaults"] = doc.getElementById("restore-defaults");
      this.el["restore-defaults-status"] = doc.getElementById("restore-defaults-status");
      this.el["update-draft-status"] = doc.getElementById("update-draft-status");
      this.el["update-instruction"] = doc.getElementById("update-instruction");
      for (const id of ["update-check", "update-now", "update-versions", "update-status", "update-open", "update-dialog", "update-title", "update-message", "update-commands", "update-install", "update-draft", "update-copy", "update-changelog", "update-dismiss", "update-close", "update-confirm-dialog", "update-confirm-commands", "update-confirm", "update-cancel"]) this.el[id] = doc.getElementById(id);
      this.bindControls(); this.render();
    }
    isVisible() { return this.doc.visibilityState !== "hidden" && this.inViewport && !this.closed; }
    async start() {
      try {
        const info = await this.bridge.request("ui/initialize", {
          appInfo: UI.appInfo,
          appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
          protocolVersion: "2026-01-26",
        });
        this.hostCapabilities = info?.hostCapabilities || {};
        this.applyHostContext(info?.hostContext || {});
        this.initialized = true;
        this.bridge.notify("ui/notifications/initialized");
        this.render();
        // The entrypoint result is delivered after initialized, rather than
        // inferred from hostContext or a recent chat on disk.
      } catch (error) { this.reportConnectionError(error); }
    }
    bindControls() {
      this.el["update-open"].addEventListener("click", () => {
        if (!this.initialized || this.closed || this.updateBusy || this.messageBusy || this.busy) return;
        this.updateDraftFeedback = ""; this.renderUpdate(); this.el["update-dialog"].showModal?.();
      });
      this.el["update-close"].addEventListener("click", () => this.el["update-dialog"].close?.());
      this.el["update-now"].addEventListener("click", () => this.updateAction("check"));
      this.el["update-check"].addEventListener("change", () => this.toggleUpdateCheck());
      this.el["update-dismiss"].addEventListener("click", () => this.updateAction("dismiss"));
      this.el["update-copy"].addEventListener("click", () => this.copy(this.updatePrompt(), "已复制更新指令。"));
      this.el["update-draft"].addEventListener("click", () => this.updateDraft());
      this.el["update-changelog"].addEventListener("click", () => this.updateChangelog());
      this.el["update-install"].addEventListener("click", () => { if (!this.el["update-install"].disabled && this.updateInfo?.can_install) { this.el["update-dialog"].close?.(); this.el["update-confirm-dialog"].showModal?.(); } });
      this.el["update-cancel"].addEventListener("click", () => this.el["update-confirm-dialog"].close?.());
      this.el["update-confirm"].addEventListener("click", () => { if (!this.el["update-confirm"].disabled) { this.el["update-confirm-dialog"].close?.(); this.el["update-dialog"].showModal?.(); this.updateAction("install"); } });
      this.el["settings-toggle"].addEventListener("click", () => this.openFooterView("settings"));
      this.el["settings-close"].addEventListener("click", () => this.showSettings(false));
      this.el["restore-defaults"].addEventListener("click", () => this.restoreDefaults());
      this.el.refresh.addEventListener("click", () => this.action("next_refresh"));
      this.el["auto-generate"].addEventListener("change", () => {
        const enabled = this.el["auto-generate"].checked;
        this.pendingAuto = enabled;
        this.action("next_auto", { enabled }).finally(() => { this.pendingAuto = null; this.render(); });
      });
      this.el["status-action"].addEventListener("click", () => {
        if (this.currentState() === "unconfigured") this.showSettings(true);
        else if (!this.snapshot?.panel_id || this.suspended || this.connectionError || isConnectionError(this.snapshot?.error)) this.reconnect();
        else this.action("next_refresh");
      });
      this.el["settings-form"].addEventListener("input", (event) => { this.settingsDirty = true; this.el["settings-dirty"].hidden = false; if (event?.target === this.el["api-key"] && this.el["api-key"].value) this.clearApiKey = false; if (event?.target === this.el["base-url"]) { this.el["api-key"].value = ""; this.clearApiKey = false; } this.syncCatalogSource(); if (event?.target === this.el.model) this.renderModelPicker(); });
      this.el["clear-api-key"].addEventListener("click", () => {
        if (!this.initialized || this.busy || this.closed || !this.config.api_key_present) return;
        this.clearApiKey = !this.clearApiKey;
        this.settingsDirty = true; this.el["settings-dirty"].hidden = false;
        if (this.clearApiKey) this.el["api-key"].value = "";
        this.catalogKey = ""; this.syncCatalogSource();
      });
      this.el["relay-preset"].addEventListener("change", () => this.chooseRelayPreset());
      this.el["settings-form"].addEventListener("submit", (event) => { event.preventDefault(); this.saveSettings(); });
      this.el["load-models"].addEventListener("click", () => this.loadModels());
      this.el["model-list"].addEventListener("change", () => this.chooseModel());
      this.el["close-panel"].addEventListener("click", () => this.close());
      this.el["usage-open"].addEventListener("click", () => this.openFooterView("usage"));
      this.el["check-model"].addEventListener("click", () => this.showSettings(true));
      for (const skin of SKINS) this.el[`skin-${skin}`].addEventListener("click", () => this.changeSkin(skin));
      this.el["discard-continue"].addEventListener("click", () => this.el["discard-dialog"].close?.());
      this.el["discard-confirm"].addEventListener("click", () => { this.settingsDirty = false; this.fillSettings(); this.el["discard-dialog"].close?.(); this.openView(this.pendingView || "suggestions"); });
    }
    updateCommands() { return Array.isArray(this.updateInfo?.commands) ? this.updateInfo.commands.join("\n") : ""; }
    updatePrompt() { const commands = this.updateCommands(); return commands ? `${UPDATE_PROMPT_PREFIX}\n${commands}` : ""; }
    renderUpdate() {
      const info = this.updateInfo || {}; const op = info.operation || {};
      const active = this.initialized && !this.closed && !this.updateBusy && !this.busy && !this.messageBusy;
      const running = op.state === "running";
      this.el["restore-defaults"].disabled = !active || running;
      this.el["save-settings"].disabled = !active;
      this.el["update-check"].checked = this.config.update_check !== false;
      this.el["update-check"].disabled = !active;
      this.el["update-now"].disabled = !active || !!info.checking;
      this.el["update-now"].textContent = info.checking ? "检查中…" : "立即检查";
      const pending = info.state === "pending_restart";
      const available = ["auto_update", "manual_update"].includes(info.state) && !info.dismissed;
      const target = info.target || info.latest;
      const remoteStale = info.latest_stale === true || [info.running, info.installed, info.snapshot].some(value => versionIsOlder(info.latest, value));
      const uiNotice = this.backendVersion ? `插件已更新到 ${this.backendVersion}，请重新打开此面板以使用新界面。` : "";
      const pendingMessage = `已更新到 ${info.installed}，当前仍在运行 ${info.running}。新开聊天或重启 Codex 后生效。`;
      let message = info.status === "disabled" ? "已关闭检查更新。" : info.checking ? "正在检查更新…" : info.dismissed ? `已忽略版本 ${target}。` : info.state === "auto_update" ? `发现 ${target}。Codex 通常会在后台自动更新；也可以点击立即更新。` : info.state === "manual_update" ? `发现 ${target}，当前安装来源需要手动更新。` : info.state === "current" ? "当前已是最新版本。" : "暂时无法检查。";
      if (running) message = op.step === 1 ? "正在安装插件…" : "正在更新插件市场…";
      if (op.state === "success") message = "更新完成，请按 Codex 提示刷新插件。新开聊天或重启 Codex 后生效。" + (op.hooks_changed ? " Hook 已变化，请到 Codex 重新审阅并信任 Hook。" : "");
      if (op.state === "error") message = op.message || "更新失败，请使用手动命令。";
      if (this.updateError) message = this.updateError;
      if (pending) message = pendingMessage + (op.hooks_changed ? " Hook 已变化，请到 Codex 重新审阅并信任 Hook。" : "");
      if (uiNotice) message += " " + uiNotice;
      this.el["update-versions"].textContent = `运行中 ${info.running || "未知"} · 已安装 ${info.installed || "未检测到"} · 市场快照 ${info.snapshot || "未检测到"} · 远端 ${!remoteStale && info.latest || "未知"}`;
      this.el["update-status"].textContent = message;
      this.el["update-message"].textContent = message;
      this.el["update-title"].textContent = pending ? "更新等待生效" : target ? `插件更新 · ${target}` : "插件更新";
      this.el["update-open"].hidden = !uiNotice && !pending && !available && !running && !(op.state === "error" && info.state !== "current");
      this.el["update-open"].textContent = uiNotice || (this.updateDrafting ? "正在填入更新草稿…" : pending ? `已更新到 ${info.installed} · 等待生效` : running ? "正在更新插件…" : op.state === "success" ? "更新完成" : op.state === "error" ? "更新未完成 · 查看" : `有新版本 ${target}`);
      this.el["update-open"].disabled = !active;
      this.el["update-commands"].textContent = this.updateCommands();
      this.el["update-instruction"].textContent = UPDATE_PROMPT_PREFIX;
      this.el["update-instruction"].hidden = pending || !this.updateCommands();
      this.el["update-confirm-commands"].textContent = this.updateCommands();
      this.el["update-install"].hidden = !info.can_install;
      this.el["update-install"].disabled = !active || !available || running;
      this.el["update-confirm"].disabled = this.el["update-install"].disabled;
      this.el["update-dismiss"].hidden = pending || !available;
      this.el["update-dismiss"].disabled = !active || !available || running;
      this.el["update-commands"].hidden = pending || !this.updateCommands();
      this.el["update-copy"].hidden = pending;
      this.el["update-draft"].hidden = pending || !supportsDraft(this.hostCapabilities);
      this.el["update-draft"].disabled = !active || !this.snapshot?.panel_id || this.suspended || !this.isVisible() || !this.updateCommands();
      this.el["update-draft"].textContent = this.updateDrafting ? "正在填入…" : "填入草稿";
      this.el["update-draft-status"].textContent = this.updateDraftFeedback;
      this.el["update-draft-status"].hidden = !this.updateDraftFeedback;
      this.el["update-draft-status"].dataset.error = String(this.updateDraftFailed);
      this.el["update-copy"].disabled = !active || !this.updateCommands();
      this.el["update-changelog"].disabled = !active || !info.changelog_url;
      this.win.clearTimeout(this.updateTimer);
      if (this.isVisible() && !this.snapshot?.panel_id && (info.checking || running)) {
        this.updateTimer = this.win.setTimeout(async () => {
          if (!this.isVisible()) return;
          try { const data = await this.tool("next_update", { action: "status" }); if (!this.closed && !this.updateBusy) { this.updateInfo = data.update; this.renderUpdate(); } }
          catch (_) { if (!this.closed) { this.updateError = "未能读取更新进度，请重新打开面板查看。"; this.renderUpdate(); } }
        }, 1000);
      }
    }
    async updateAction(action) {
      if (!this.initialized || this.closed || !this.isVisible() || this.updateBusy || this.busy) return;
      if (action === "install" && (!this.updateInfo?.can_install || this.updateInfo.state !== "auto_update" || !this.snapshot?.panel_id)) return;
      this.updateBusy = true; this.updateError = ""; this.epoch++; this.renderUpdate();
      try {
        const data = await this.tool("next_update", { action, ...(action === "install" ? { panel_id: this.snapshot.panel_id, confirmed: true } : {}) });
        if (this.closed) return;
        if (data.update) this.updateInfo = data.update;
        if (data.config) { this.config = { ...this.config, update_check: data.config.update_check, dismissed_version: data.config.dismissed_version }; this.settingsSynced(); }
      } catch (_) { if (!this.closed) this.updateError = "未能确认更新操作结果，请查看状态后使用手动命令。"; }
      finally { this.updateBusy = false; if (!this.closed) { this.renderUpdate(); this.schedulePoll(0); } }
    }
    async toggleUpdateCheck() {
      if (!this.initialized || this.closed || this.updateBusy || this.busy) return;
      const enabled = this.el["update-check"].checked;
      this.updateBusy = true; this.updateError = ""; this.epoch++;
      this.renderUpdate();
      try {
        const data = await this.tool("next_configure", { changes: { update_check: enabled } });
        if (this.closed) return;
        if (data.config) { this.config = { ...this.config, update_check: data.config.update_check }; this.settingsSynced(); }
        if (data.update) this.updateInfo = data.update;
      } catch (_) { if (!this.closed) this.updateError = "检查更新开关未确认保存，请重试。"; }
      finally { this.updateBusy = false; if (!this.closed) this.renderUpdate(); }
    }
    async updateDraft() {
      if (!this.initialized || this.closed || !this.isVisible() || this.updateBusy || this.messageBusy || this.busy || this.updateInfo?.state === "pending_restart") return;
      if (!supportsDraft(this.hostCapabilities) || !this.snapshot?.panel_id || this.suspended || !this.updateCommands()) {
        this.updateDraftFeedback = "暂时无法填入当前聊天，请重新连接面板或复制命令。";
        this.updateDraftFailed = true; this.renderUpdate(); return;
      }
      const text = this.updatePrompt();
      const { panel_id: panelId, thread_id: threadId } = this.snapshot;
      const epoch = this.epoch; const view = this.view; const wasOpen = this.el["update-dialog"].open;
      const current = () => !this.closed && this.isVisible() && epoch === this.epoch && view === this.view &&
        panelId === this.snapshot?.panel_id && threadId === this.snapshot?.thread_id;
      this.updateBusy = true; this.messageBusy = true; this.updateDrafting = true;
      this.updateDraftFeedback = ""; this.updateDraftFailed = false;
      // Release the modal focus trap before the host focuses the owning chat's composer.
      this.el["update-dialog"].close?.(); this.render();
      try {
        const result = await this.bridge.request("ui/message", draftParams(text));
        if (result?.isError) throw new Error("draft_rejected");
        if (current()) this.notice("已追加更新指令到草稿，请编辑后自行发送。");
      } catch (error) {
        if (current()) {
          this.updateDraftFeedback = isConnectionError(error) ? "未确认填入结果，请先检查当前聊天草稿，避免重复追加；也可复制命令。" : "未能填入草稿，请重试或复制命令。";
          this.updateDraftFailed = true;
          if (wasOpen) this.el["update-dialog"].showModal?.();
          else this.notice(this.updateDraftFeedback, true);
        }
      } finally {
        this.updateBusy = false; this.messageBusy = false; this.updateDrafting = false;
        if (!this.closed) this.render();
      }
    }
    async updateChangelog() {
      if (!this.initialized || this.closed || !this.isVisible() || !this.updateInfo?.changelog_url) return;
      try { await this.bridge.request("ui/open-link", { url: this.updateInfo.changelog_url }); }
      catch (_) { if (!this.closed) this.notice("未能打开更新内容，请到公开仓库查看 CHANGELOG.zh.md。", true); }
    }
    async tool(name, args = {}, timeout = RPC_TIMEOUT_MS) {
      const data = structured(await this.bridge.request("tools/call", { name, arguments: args }, timeout));
      this.observeBackendVersion(data);
      return data;
    }
    observeBackendVersion(data) {
      const version = data?.plugin_version;
      if (this.closed || typeof version !== "string" || version.length > 64 || !/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(version) || version === this.uiVersion) return;
      this.backendVersion = version;
      this.renderUpdate();
    }
    chooseModel() {
      if (!this.initialized || this.busy || this.closed) return;
      const value = this.el["model-list"].value;
      this.manualModel = value === "";
      if (!this.manualModel) this.el.model.value = value;
      this.settingsDirty = true; this.el["settings-dirty"].hidden = false;
      this.settingsMessage(""); this.renderModelPicker();
      if (this.manualModel) this.el.model.focus();
    }
    renderModelPicker() {
      const current = this.el.model.value.trim();
      const entries = [...this.modelEntries];
      if (current && !entries.some(item => item.id === current)) entries.push({ id: current, label: current });
      const signature = JSON.stringify(entries);
      if (signature !== this.modelOptionsSignature) {
        this.el["model-list"].replaceChildren();
        for (const item of entries) {
          const option = this.doc.createElement("option"); option.value = item.id;
          option.textContent = item.label === item.id ? item.id : `${item.label} · ${item.id}`;
          this.el["model-list"].append(option);
        }
        const manual = this.doc.createElement("option"); manual.value = ""; manual.textContent = "手动输入模型 ID";
        this.el["model-list"].append(manual);
        this.modelOptionsSignature = signature;
      }
      this.el["model-list"].value = this.manualModel || !current ? "" : current;
      this.el["model-manual-field"].hidden = !this.manualModel && !!current;
    }
    onHostMessage(message) {
      if (message.method === "ui/notifications/tool-result") {
        try { this.acceptResult(structured(message.params), true); }
        catch (error) { this.reportConnectionError(error); }
      } else if (message.method === "ui/notifications/host-context-changed") {
        this.applyHostContext(message.params || {}); this.render(); this.syncVisibility();
      } else if (message.method === "ui/resource-teardown") {
        // Stop lease renewal immediately. The bounded close acknowledgement is
        // best effort; backend lease expiry also covers abrupt iframe removal.
        this.dispose().finally(() => { if (message.id != null) { try { this.bridge.reply(message.id, {}); } catch (_) { /* host gone */ } } this.bridge.dispose(); });
      } else if (message.method === "ping" && message.id != null) this.bridge.reply(message.id, {});
    }
    applyHostContext(update) {
      this.hostContext = { ...this.hostContext, ...update, styles: { ...(this.hostContext.styles || {}), ...(update.styles || {}) } };
      const root = this.doc.documentElement;
      const mode = this.hostContext.displayMode;
      if (["inline", "fullscreen", "pip"].includes(mode)) root.dataset.displayMode = mode;
      else delete root.dataset.displayMode;
      for (const edge of ["top", "right", "bottom", "left"]) {
        const value = this.hostContext.safeAreaInsets?.[edge];
        const key = "--host-safe-" + edge;
        if (typeof value === "number" && Number.isFinite(value) && value >= 0) root.style.setProperty(key, value + "px");
        else root.style.removeProperty(key);
      }
      if (update.theme === "light" || update.theme === "dark") this.doc.documentElement.dataset.theme = update.theme;
      const variables = update.styles?.variables;
      if (variables && typeof variables === "object") for (const [key, value] of Object.entries(variables)) {
        if (!/^--(?:color-|font-|border-radius-)/.test(key)) continue;
        if (typeof value === "string") this.doc.documentElement.style.setProperty(key, value);
        else this.doc.documentElement.style.removeProperty(key);
      }
      // No font CSS injection: the panel ships with system fonts and zero CDN.
    }
    acceptResult(data, notification = false, appearanceEpoch = this.appearanceEpoch) {
      if (!data || typeof data !== "object" || this.closed) return;
      this.observeBackendVersion(data);
      if (data.update && !this.updateBusy) this.updateInfo = data.update;
      if (data.appearance && !this.skinBusy && appearanceEpoch === this.appearanceEpoch) this.applyAppearance(data.appearance);
      if (!data.panel_id || !data.thread_id) {
        if (notification && data.status === "unbound") {
          const previous = this.snapshot;
          this.snapshot = null; this.previewSuggestions = []; this.pendingAuto = null; this.epoch++; this.win.clearTimeout(this.pollTimer);
          this.suspended = false;
          this.connectionError = typeof data.error === "string" ? data.error : "";
          if (previous?.panel_id) this.tool("next_close", { panel_id: previous.panel_id }, 700).catch(() => {});
        }
        if (data.config) this.settingsSynced();
        if (data.config && !this.settingsDirty) { this.config = data.config; this.fillSettings(); this.render(); }
        else this.render();
        return;
      }
      if (this.closed) { this.tool("next_close", { panel_id: data.panel_id }, 700).catch(() => {}); return; }
      const previous = this.snapshot;
      const switched = previous && (previous.panel_id !== data.panel_id || previous.thread_id !== data.thread_id);
      if (switched && !notification) return;
      if (previous?.thread_id === data.thread_id && Number.isFinite(data.revision) && Number.isFinite(previous.revision) && data.revision < previous.revision) {
        if (switched && notification) this.tool("next_close", { panel_id: data.panel_id }, 700).catch(() => {});
        return;
      }
      // Keep only the exact state already displayed for this conversation.
      // The backend still withholds its cache until the new lease has a fresh read.
      const preview = data.status === "syncing" && previous?.thread_id === data.thread_id &&
        Number.isFinite(data.revision) && data.revision === previous.revision;
      this.previewSuggestions = preview ? (previous.status === "ready" ? previous.suggestions : this.previewSuggestions) : [];
      if (switched) {
        this.epoch++; this.pendingAuto = null;
        this.pendingDisplays = []; this.displayCandidate = null;
        this.tool("next_close", { panel_id: previous.panel_id }, 700).catch(() => {});
      }
      this.snapshot = data; this.connectionError = "";
      if (this.isVisible() && data.refresh_notice?.id && data.refresh_notice.id !== this.refreshNoticeId &&
          typeof data.refresh_notice.message === "string") {
        this.refreshNoticeId = data.refresh_notice.id; this.notice(data.refresh_notice.message);
      }
      if (data.config) { this.config = data.config; this.settingsSynced(); }
      if (!this.settingsDirty) this.fillSettings();
      this.render(); this.schedulePoll();
      if (!this.isVisible()) this.suspend(true);
    }
    currentState() {
      if (this.closed) return "closed";
      if (this.connectionError) return this.snapshot?.panel_id || this.suspended ? "error" : "unbound";
      if (!this.snapshot?.panel_id) return this.initialized ? "unbound" : "connecting";
      if (this.snapshot.status === "error" && this.snapshot.reason_code === "result_unavailable") return "refresh_needed";
      if (this.snapshot.status === "ready" && (!Array.isArray(this.snapshot.suggestions) || !this.snapshot.suggestions.some((item) => item && typeof item.prompt === "string" && item.prompt.trim()))) return "empty";
      return STATES[this.snapshot.status] ? this.snapshot.status : "waiting";
    }
    schedulePoll(delay) {
      if (delay == null) delay = ["generating", "streaming"].includes(this.currentState()) ? GENERATING_POLL_MS : POLL_MS;
      this.win.clearTimeout(this.pollTimer); this.pollTimer = null;
      if (!this.initialized || (!this.snapshot?.panel_id && !this.suspended) || !this.isVisible()) return;
      this.pollTimer = this.win.setTimeout(() => this.poll(), delay);
    }
    async poll() {
      if (this.suspended) { await this.reconnect(); return; }
      if (this.polling || this.busy || !this.isVisible() || !this.snapshot?.panel_id) { this.schedulePoll(); return; }
      const epoch = this.epoch; const appearanceEpoch = this.appearanceEpoch; const panelId = this.snapshot.panel_id;
      const sample = this.pendingDisplays.find(item => item.panelId === panelId);
      this.polling = true;
      try {
        const data = await this.tool("next_poll", { panel_id: panelId, ...(sample ? { displayed: sample.data } : {}) });
        if (sample) this.pendingDisplays = this.pendingDisplays.filter(item => item !== sample);
        if (epoch === this.epoch && this.isVisible()) this.acceptResult(data, false, appearanceEpoch);
      }
      catch (error) { if (epoch === this.epoch && this.isVisible()) this.reportConnectionError(error); }
      finally { this.polling = false; this.schedulePoll(); }
    }
    suspend(closeLease) {
      this.win.clearTimeout(this.updateTimer);
      this.win.clearTimeout(this.pollTimer); this.pollTimer = null;
      this.epoch++;
      if (closeLease && !this.suspended && this.initialized && this.snapshot?.panel_id) {
        this.suspended = true;
        this.tool("next_close", { panel_id: this.snapshot.panel_id }, 700).catch(() => {});
      }
    }
    syncVisibility() {
      if (!this.isVisible()) this.suspend(true);
      else if (this.initialized && this.snapshot?.panel_id) {
        if (this.suspended) this.reconnect();
        else this.schedulePoll(0);
      }
      else if (this.isVisible()) this.renderUpdate();
    }
    async reconnect() {
      if (this.busy || this.closed || !this.isVisible()) { this.schedulePoll(); return false; }
      if (!this.initialized) { this.connectionError = ""; await this.start(); return; }
      const epoch = this.epoch;
      this.suspended = true;
      this.busy = true; this.render();
      try {
        const data = await this.tool("next_panel");
        if (epoch !== this.epoch || !this.isVisible()) {
          if (data?.panel_id) this.tool("next_close", { panel_id: data.panel_id }, 700).catch(() => {});
          return false;
        }
        if (!data?.panel_id || !data?.thread_id) throw new Error(data?.error || "未能重新连接当前聊天，请重试。");
        this.suspended = false; this.reconnectAttempts = 0; this.acceptResult(data, true);
        return true;
      }
      catch (error) { this.reportConnectionError(error); }
      finally {
        this.busy = false; this.render();
        this.schedulePoll(this.suspended ? Math.min(30000, 1000 * 2 ** Math.min(this.reconnectAttempts++, 5)) : undefined);
      }
      return false;
    }
    async action(name, args = {}) {
      if (this.busy || !this.isVisible()) return;
      if (this.suspended && !await this.reconnect()) return;
      if (!this.snapshot?.panel_id) return;
      const epoch = this.epoch; const appearanceEpoch = this.appearanceEpoch;
      const panelId = this.snapshot.panel_id;
      if (name === "next_refresh") {
        if (this.refreshRequest?.panelId !== panelId) this.refreshRequest = {
          panelId, id: this.win.crypto?.randomUUID?.() || `refresh-${Date.now()}-${++this.requestSequence}`,
        };
        args = { ...args, request_id: this.refreshRequest.id };
      }
      this.busy = true; this.render();
      try {
        const data = await this.tool(name, { panel_id: panelId, ...args });
        if (name === "next_refresh" && this.refreshRequest?.id === args.request_id) this.refreshRequest = null;
        if (epoch === this.epoch) this.acceptResult(data, false, appearanceEpoch);
      }
      catch (error) { if (isConnectionError(error)) this.reportConnectionError(error); else this.notice(publicError(error), true); }
      finally { this.busy = false; this.render(); this.schedulePoll(); }
    }
    async draft(prompt) {
      if (!supportsDraft(this.hostCapabilities)) { this.notice("当前宿主不支持填入草稿，请复制建议。", true); return; }
      if (!this.snapshot?.panel_id || !this.isVisible() || this.busy || this.messageBusy || this.suspended || this.currentState() !== "ready") return;
      this.messageBusy = true; this.render();
      try { await this.bridge.request("ui/message", draftParams(prompt)); this.notice("已填入输入框，可编辑后发送。"); }
      catch (error) { this.notice(`未填入草稿：${publicError(error)} 可使用复制。`, true); }
      finally { this.messageBusy = false; if (!this.closed) this.render(); }
    }
    executionKey() {
      return JSON.stringify([this.snapshot?.thread_id, this.snapshot?.recent_jobs?.[0]?.invoked_at ?? null, this.snapshot?.suggestions]);
    }
    async execute(prompt, expectedKey = this.executionKey()) {
      if (!supportsExecute(this.hostCapabilities)) { this.notice("当前宿主不支持直接执行，可填入草稿或复制建议。", true); return; }
      const key = this.executionKey();
      if (!this.initialized || !this.snapshot?.panel_id || !this.isVisible() || this.view !== "suggestions" || this.busy ||
          this.messageBusy || this.suspended || this.currentState() !== "ready" || expectedKey !== key || this.executions.has(key) ||
          !this.snapshot.suggestions.some(item => item?.prompt === prompt && typeof prompt === "string" && prompt.trim())) return;
      const submission = { prompt, phase: "pending" };
      this.executions.set(key, submission);
      if (this.executions.size > 50) this.executions.delete(this.executions.keys().next().value);
      const epoch = this.epoch;
      const current = () => !this.closed && epoch === this.epoch && key === this.executionKey();
      this.messageBusy = true; this.render();
      try {
        // Standard ui/message sends a follow-up in the owning conversation.
        // Send once, without a separate draft insertion or automatic replay.
        const result = await this.bridge.request("ui/message", { role: "user", content: [{ type: "text", text: prompt }] });
        if (result?.isError) {
          this.executions.delete(key);
          if (current()) this.notice("未能发送这条建议，可填入草稿后手动发送。", true);
        } else {
          submission.phase = "sent";
          if (current()) this.notice("已提交到当前聊天。");
        }
      } catch (_) {
        submission.phase = "unknown";
        if (current()) this.notice("未确认发送结果，请查看当前聊天后再决定是否重试。", true);
      } finally { this.messageBusy = false; if (!this.closed) this.render(); }
    }
    async copy(prompt, confirmation = "已复制建议。") {
      try {
        if (!this.win.navigator.clipboard?.writeText) throw new Error("当前环境不支持剪贴板。");
        await this.win.navigator.clipboard.writeText(prompt); this.notice(confirmation);
      } catch (_) { this.notice("复制未成功，请选中建议文字手动复制。", true); }
    }
    reportConnectionError(error) {
      const message = publicError(error);
      const changed = message !== this.connectionError;
      this.connectionError = message; this.suspended = true;
      this.render(); this.schedulePoll();
      if (changed && this.view !== "suggestions") this.notice(message, true);
    }
    notice(message, error = false) {
      const footerHeight = this.el["update-open"].parentElement?.offsetHeight;
      if (footerHeight) this.el.notice.style.setProperty("bottom", `calc(${footerHeight + 12}px + var(--panel-safe-bottom))`);
      this.el.notice.textContent = message; this.el.notice.dataset.error = String(error); this.el.notice.hidden = false;
      this.win.clearTimeout(this.noticeTimer);
      this.noticeTimer = this.win.setTimeout(() => { this.el.notice.hidden = true; }, 5000);
    }
    openFooterView(next) {
      if (!this.initialized || this.closed || this.busy || next === this.view) return false;
      return next === "settings" ? this.showSettings(true) : this.openView("usage");
    }
    openView(next) {
      if (next === this.view) return true;
      if (this.busy && this.view === "settings") return false;
      if (this.view === "settings" && this.settingsDirty) { this.pendingView = next; this.el["discard-dialog"].showModal?.(); return false; }
      this.viewPositions[this.view] = this.el["panel-scroll"].scrollTop || 0;
      this.view = next; this.settingsOpen = next === "settings"; this.renderView();
      this.el["panel-scroll"].scrollTop = this.viewPositions[next] || 0;
      return true;
    }
    renderView() {
      const next = this.view;
      this.el["suggestion-header"].hidden = next !== "suggestions"; this.el["view-header"].hidden = next === "suggestions";
      this.el["suggestion-view"].hidden = next !== "suggestions"; this.el.settings.hidden = next !== "settings";
      this.el["usage-view"].hidden = next !== "usage"; this.el["save-settings"].hidden = next !== "settings";
      this.el["settings-actions"].hidden = next !== "settings";
      this.el["view-title"].textContent = next === "settings" ? "插件设置" : "用量记录";
      const unavailable = !this.initialized || this.closed || this.busy;
      for (const [id, target] of [["settings-toggle", "settings"], ["usage-open", "usage"]]) {
        this.el[id].setAttribute("aria-expanded", String(next === target));
        this.el[id].setAttribute("aria-current", next === target ? "page" : "false");
        this.el[id].disabled = unavailable;
      }
      this.el["usage-arrow"].hidden = false;
      this.el["settings-dirty"].hidden = next !== "settings" || !this.settingsDirty;
      this.observeDisplay();
    }
    observeDisplay() {
      const jobId = this.snapshot?.display_sample?.job_id;
      if (!jobId || !this.win.requestAnimationFrame || this.closed || this.suspended || !this.isVisible() ||
          this.view !== "suggestions" || !["ready", "streaming"].includes(this.currentState())) return;
      const panelId = this.snapshot.panel_id;
      const stage = this.snapshot.display_sample.stage === "preview" ? "preview" : "final";
      const key = `${panelId}:${jobId}:${stage}`;
      if (this.displayObserved.has(key) || (this.displayCandidate?.key === key && this.displayCandidate.stage === stage)) return;
      const candidate = { key, epoch: this.epoch, stage }; this.displayCandidate = candidate;
      // Two animation frames place the observation after a visible render
      // opportunity. This is a browser observation, not a physical display probe.
      this.win.requestAnimationFrame(() => this.win.requestAnimationFrame(() => {
        if (this.displayCandidate === candidate) this.displayCandidate = null;
        if (candidate.epoch !== this.epoch || this.closed || this.suspended || !this.isVisible() ||
            this.view !== "suggestions" || !["ready", "streaming"].includes(this.currentState()) || this.el.suggestions.hidden ||
            this.snapshot?.panel_id !== panelId || this.snapshot?.display_sample?.job_id !== jobId ||
            (this.snapshot.display_sample.stage === "preview" ? "preview" : "final") !== stage || this.displayObserved.has(key)) return;
        this.pendingDisplays.push({ panelId, data: { job_id: jobId, displayed_at: Date.now() / 1000, ...(stage === "preview" ? { stage } : {}) } });
        this.displayObserved.add(key);
        if (this.displayObserved.size > 100) this.displayObserved.delete(this.displayObserved.values().next().value);
      }));
    }
    async showSettings(show) {
      if (!this.openView(show ? "settings" : "suggestions")) return;
      if (show && this.initialized && !this.settingsDirty) {
        const sequence = ++this.settingsReadSequence;
        const syncVersion = this.settingsSyncVersion;
        const current = () => sequence === this.settingsReadSequence && syncVersion === this.settingsSyncVersion && this.view === "settings" && !this.closed;
        try {
          const data = await this.tool("next_settings");
          if (current() && data?.config) {
            this.config = data.config; this.settingsSynced();
            if (data.update && !this.updateBusy) this.updateInfo = data.update;
            if (!this.settingsDirty) this.fillSettings();
            this.render();
          }
        }
        catch (error) { if (current()) this.reportConnectionError(error); }
      }
    }
    applyAppearance(appearance) {
      if (appearance?.skin === "focus") appearance = { ...appearance, skin: "graphite" };
      if (!SKINS.includes(appearance?.skin)) return;
      this.skin = appearance.skin; this.el.app.dataset.skin = this.skin;
      this.el.app.style.setProperty("--skin-index", String(SKINS.indexOf(this.skin)));
      for (const skin of SKINS) this.el[`skin-${skin}`].setAttribute("aria-pressed", String(skin === this.skin));
      if (appearance.error && appearance.error !== this.appearanceWarning) { this.appearanceWarning = appearance.error; this.notice("无法读取外观设置，请重新选择皮肤。", true); }
    }
    async changeSkin(skin) {
      if (this.skinBusy || !this.initialized || this.closed || skin === this.skin || !SKINS.includes(skin)) return;
      this.skinBusy = true; this.appearanceEpoch++; this.render();
      try { const data = await this.tool("next_appearance", { skin }); if (this.closed) return; this.applyAppearance(data?.appearance); }
      catch (_) { this.notice("外观没有保存，请重试。", true); }
      finally { this.appearanceEpoch++; this.skinBusy = false; this.render(); }
    }
    fillSettings() {
      const config = this.config;
      this.manualModel = !config.model;
      const defaults = { model: "gpt-6.1-terra", reasoning_effort: "low", service_tier: "priority", base_url: "" };
      for (const [id, field] of [["model", "model"], ["reasoning-effort", "reasoning_effort"], ["service-tier", "service_tier"], ["base-url", "base_url"]]) {
        const value = config[field];
        this.el[id].value = field === "service_tier" && value === null ? "" : typeof value === "string" ? value : defaults[field];
      }
      this.el["output-mode"].value = config.output_mode === "standard" ? "standard" : "concise";
      this.el["api-key"].value = "";
      this.clearApiKey = false;
      const address = this.el["base-url"].value.trim().replace(/\/+$/, "");
      this.el["relay-preset"].value = Object.keys(RELAY_PRESETS).find(id => RELAY_PRESETS[id] === address) || (address ? "custom" : "aicodewith");
      this.showRelay(); this.renderModelPicker();
    }
    savedRelayKeyForAddress() {
      return !!this.config.api_key_present && this.el["base-url"].value.trim().replace(/\/+$/, "") === (this.config.base_url || "").replace(/\/+$/, "");
    }
    updateRelayKeyHint() {
      const saved = this.savedRelayKeyForAddress();
      this.el["clear-api-key"].hidden = !this.config.api_key_present;
      this.el["clear-api-key"].textContent = this.clearApiKey ? "取消清除" : "清除";
      this.el["clear-api-key"].setAttribute("aria-label", this.clearApiKey ? "取消清除已保存的 API Key" : "清除已保存的 API Key");
      this.el["clear-api-key"].setAttribute("aria-pressed", String(this.clearApiKey));
      this.el["api-key"].placeholder = saved ? "已保存，留空继续使用；填写可替换" : "粘贴所选中转站的 API Key";
      this.el["api-key-status"].textContent = this.clearApiKey ? "保存后清除 API Key。" : saved ? (this.config.api_key_error || "API Key 已保存，留空继续使用。") : "请输入所选中转站的 API Key，加密保存在本机。";
    }
    syncRelayPreset() {
      const preset = RELAY_PRESETS[this.el["relay-preset"].value];
      this.el["base-url"].readOnly = !!preset;
    }
    chooseRelayPreset() {
      if (this.busy || this.closed) return;
      const preset = RELAY_PRESETS[this.el["relay-preset"].value];
      if (preset && this.el["base-url"].value.trim().replace(/\/+$/, "") !== preset) {
        this.el["base-url"].value = preset;
        this.el["api-key"].value = "";
        this.clearApiKey = false;
      }
      this.settingsDirty = true; this.el["settings-dirty"].hidden = false;
      this.settingsMessage(""); this.syncRelayPreset(); this.syncCatalogSource();
    }
    hasRelayKey() {
      return !this.clearApiKey && (!!this.el["api-key"].value.trim() ||
        this.savedRelayKeyForAddress());
    }
    catalogSource() {
      return { provider: "relay", base_url: this.el["base-url"].value.trim(), ...(this.el["api-key"].value.trim() ? { api_key: this.el["api-key"].value.trim() } : {}) };
    }
    syncCatalogSource() {
      this.updateRelayKeyHint();
      const source = this.catalogSource(); const key = JSON.stringify(source);
      if (key === this.catalogKey) return;
      this.catalogKey = key; this.catalogEpoch++; this.catalogLoading = false;
      this.modelEntries = []; this.renderModelPicker(); this.el["load-models"].disabled = !this.initialized || this.busy || this.closed;
      this.el["models-status"].textContent = (!source.base_url || !this.hasRelayKey() ? "请先填写中转 API 地址和 API Key，再读取目录；也可手动输入模型 ID。" : "将读取当前中转服务的模型目录，也可手动输入模型 ID。");
    }
    showRelay() {
      if (!this.el["base-url"].value.trim() && RELAY_PRESETS[this.el["relay-preset"].value]) this.el["base-url"].value = RELAY_PRESETS[this.el["relay-preset"].value];
      this.syncRelayPreset(); this.syncCatalogSource();
    }
    settingsSynced() {
      this.settingsSyncVersion++;
    }
    settingsMessage(message, error = false) {
      this.el["settings-message"].textContent = message; this.el["settings-message"].dataset.error = String(error);
    }
    async restoreDefaults() {
      if (!this.initialized || this.closed || !this.isVisible() || this.view !== "settings" ||
          this.busy || this.updateBusy || this.updateInfo?.operation?.state === "running") return;
      const connection = { address: this.el["base-url"].value, key: this.el["api-key"].value,
        preset: this.el["relay-preset"].value, clear: this.clearApiKey };
      const message = (text, error = false) => {
        this.el["restore-defaults-status"].textContent = text;
        this.el["restore-defaults-status"].dataset.error = String(error);
      };
      const epoch = ++this.epoch; this.settingsReadSequence++; this.catalogEpoch++; this.catalogLoading = false;
      this.busy = true; this.restoringDefaults = true; this.settingsDirty = true;
      this.render(); message("正在恢复默认配置…");
      try {
        const data = await this.tool("next_restore_defaults");
        if (this.closed || epoch !== this.epoch) return;
        if (!data?.config) throw new Error("未收到恢复后的配置，请重新读取设置确认。");
        this.config = data.config; this.settingsSynced();
        if (data.update) this.updateInfo = data.update;
        this.fillSettings();
        this.el["base-url"].value = connection.address; this.el["api-key"].value = connection.key;
        this.el["relay-preset"].value = connection.preset; this.clearApiKey = connection.clear;
        this.syncRelayPreset(); this.syncCatalogSource();
        this.settingsDirty = !!connection.key.trim() || connection.clear ||
          connection.address.trim().replace(/\/+$/, "") !== (this.config.base_url || "").replace(/\/+$/, "");
        this.settingsMessage("");
        message(this.settingsDirty ? "已恢复默认配置；API 连接的更改仍需保存。" : "已恢复默认配置。");
        this.schedulePoll(0);
      } catch (error) {
        if (!this.closed && epoch === this.epoch) {
          if (isConnectionError(error)) {
            this.reportConnectionError(error);
            message("未能确认默认配置是否恢复，填写内容已保留。请重连后重试。", true);
          } else message(publicError(error), true);
        }
      } finally {
        this.busy = false; this.restoringDefaults = false;
        if (!this.closed) this.render();
      }
    }
    async saveSettings() {
      if (!this.initialized || this.busy || this.updateBusy || this.closed) return;
      const model = this.el.model.value.trim();
      if (!model) { this.settingsMessage("请明确选择或输入生成模型。", true); this.el.model.focus(); return; }
      const outputMode = this.el["output-mode"].value;
      if (!["concise", "standard"].includes(outputMode)) { this.settingsMessage("建议格式只支持精简或标准。", true); return; }
      const changes = {
        provider: "relay", model, output_mode: outputMode,
        reasoning_effort: this.el["reasoning-effort"].value,
        service_tier: this.el["service-tier"].value,
        base_url: this.el["base-url"].value.trim(),
        ...(this.el["api-key"].value.trim() ? { api_key: this.el["api-key"].value.trim() } : {}),
        clear_api_key: this.clearApiKey,
      };
      if (!changes.clear_api_key && (!changes.base_url || !this.hasRelayKey())) {
        this.settingsMessage("请填写中转 API 地址和 API Key；更换地址后需重新填写 Key。", true); return;
      }
      const epoch = ++this.epoch; this.settingsReadSequence++;
      this.busy = true; this.render(); this.settingsMessage("正在保存…");
      try {
        const data = await this.tool("next_configure", { changes });
        if (this.closed || epoch !== this.epoch) return;
        if (data?.config) this.config = data.config;
        this.settingsDirty = false; this.fillSettings(); this.settingsMessage(""); this.busy = false; this.openView("suggestions"); this.notice("插件设置已保存。"); this.schedulePoll(0);
      } catch (error) {
        if (!this.closed && epoch === this.epoch) {
          if (isConnectionError(error)) {
            this.reportConnectionError(error);
            this.settingsMessage("尚未确认设置是否保存，已保留填写内容。连接恢复后请重试。", true);
          } else this.settingsMessage(publicError(error), true);
        }
      }
      finally { this.busy = false; if (!this.closed) this.render(); }
    }
    async loadModels() {
      if (!this.initialized || this.closed || this.busy || this.el["load-models"].disabled) return;
      this.syncCatalogSource();
      const source = this.catalogSource();
      if ((!source.base_url || !this.hasRelayKey())) {
        this.modelEntries = []; this.renderModelPicker(); this.el["models-status"].textContent = "请先填写中转 API 地址和 API Key，再读取目录；也可手动输入模型 ID。"; return;
      }
      const epoch = ++this.catalogEpoch; const key = JSON.stringify(source);
      const current = () => !this.closed && epoch === this.catalogEpoch && key === JSON.stringify(this.catalogSource());
      this.catalogLoading = true;
      this.modelEntries = []; this.renderModelPicker();
      this.el["load-models"].disabled = true; this.el["models-status"].textContent = "正在读取中转模型目录…";
      try {
        const data = await this.tool("next_models", { source }, 15000);
        if (!current()) return;
        if (data?.provider !== source.provider) throw new Error("返回的模型目录来源不匹配，请重新读取或手动输入模型 ID。");
        const entries = (data?.models || []).map(modelEntry).filter(Boolean);
        this.modelEntries = [...new Map(entries.map(item => [item.id, item])).values()]; this.renderModelPicker();
        const label = "中转";
        this.el["models-status"].textContent = entries.length ? `${label}目录包含 ${entries.length} 个模型，可选择或手动输入。目录可读不代表模型可调用。` : `${label}目录为空，可以手动输入模型 ID。`;
      } catch (error) {
        if (current()) {
          if (isConnectionError(error)) {
            this.reportConnectionError(error);
            this.el["models-status"].textContent = "模型目录未能读取，仍可手动输入模型 ID。";
          } else {
            const message = publicError(error);
            this.el["models-status"].textContent = message.includes("手动输入") ? message : `${message} 仍可手动输入模型 ID。`;
          }
        }
      }
      finally { if (epoch === this.catalogEpoch) { this.catalogLoading = false; this.renderModelPicker(); this.el["load-models"].disabled = !this.initialized || this.busy || this.closed; } }
    }
    render() {
      this.renderView(); this.renderUpdate();
      const state = this.currentState(); const snapshot = this.snapshot; const bound = !!snapshot?.panel_id && !this.closed;
      this.el["thread-title"].textContent = bound ? (snapshot.title || "当前对话") : "尚未绑定当前对话";
      this.el["thread-title"].parentElement.classList.toggle("bound", bound);
      this.el["auto-generate"].checked = this.pendingAuto ?? snapshot?.auto_generate ?? this.config.auto_generate ?? true;
      this.el["auto-generate"].disabled = !bound || this.busy;
      this.el.refresh.disabled = !bound || this.busy || ["unconfigured", "generating", "streaming", "syncing"].includes(state);
      this.el["output-mode"].disabled = !this.initialized || this.busy || this.closed;
      this.el["restore-defaults"].textContent = this.restoringDefaults ? "正在恢复…" : "一键恢复默认配置";
      for (const id of ["reasoning-effort", "service-tier", "base-url", "api-key"]) this.el[id].disabled = !this.initialized || this.busy || this.closed;
      this.el["save-settings"].disabled = !this.initialized || this.busy || this.updateBusy || this.closed;
      this.el["load-models"].disabled = !this.initialized || this.busy || this.closed || this.catalogLoading;
      this.el["relay-preset"].disabled = !this.initialized || this.busy || this.closed;
      this.el["clear-api-key"].disabled = !this.initialized || this.busy || this.closed;
      this.el["model-list"].disabled = !this.initialized || this.busy || this.closed;
      this.el.model.disabled = !this.initialized || this.busy || this.closed;
      const displayModel = this.config.model === "gpt-6.1-sol" ? "GPT-6.1 Sol" : this.config.model;
      this.el["model-summary"].textContent = this.config.model ? `中转 · ${displayModel}${this.config.reasoning_effort ? ` · ${this.config.reasoning_effort}` : ""}` : "选择独立的生成模型";
      const displayed = ["ready", "streaming"].includes(state) ? snapshot?.suggestions : state === "syncing" ? this.previewSuggestions : [];
      const suggestions = Array.isArray(displayed) ? displayed.filter((item) => item && typeof item.prompt === "string" && item.prompt.trim()).slice(0, 3) : [];
      this.el.status.hidden = suggestions.length > 0;
      this.el.status.dataset.state = state;
      const connectionFailure = state === "error" && (!!this.connectionError || isConnectionError(snapshot?.error));
      const description = connectionFailure ? ["", "插件连接已中断", ""] : STATES[state];
      this.el["status-symbol"].textContent = description[0]; this.el["status-symbol"].hidden = !description[0]; this.el["status-title"].textContent = description[1];
      this.el["status-detail"].textContent = publicError(this.connectionError || (state === "error" && snapshot?.error ? String(snapshot.error) : description[2]));
      const hasAction = ["unbound", "unconfigured", "error", "refresh_needed", "empty", "paused"].includes(state);
      this.el["status-action"].hidden = !hasAction; this.el["status-action"].disabled = this.busy;
      this.el["status-action"].textContent = state === "unconfigured" ? "选择模型" : state === "refresh_needed" ? "重新生成" : state === "error" && bound && !this.suspended && !connectionFailure ? "重试生成" : ["empty", "paused"].includes(state) ? "刷新建议" : "重新连接";
      this.el["check-model"].hidden = state !== "error" || connectionFailure;
      for (const skin of SKINS) { this.el[`skin-${skin}`].disabled = !this.initialized || this.skinBusy || this.closed; this.el[`skin-${skin}`].setAttribute("aria-pressed", String(skin === this.skin)); }
      this.el.suggestions.hidden = !suggestions.length;
      const capabilities = JSON.stringify([supportsDraft(this.hostCapabilities), supportsExecute(this.hostCapabilities)]);
      const signature = JSON.stringify([suggestions, capabilities]);
      if (signature !== this.cardsSignature) {
        const prefix = this.cardsActionCapabilities === capabilities && this.renderedSuggestions.length > 0 &&
          suggestions.length >= this.renderedSuggestions.length && this.renderedSuggestions.every((item, i) => JSON.stringify(item) === JSON.stringify(suggestions[i]));
        const start = prefix ? this.renderedSuggestions.length : 0;
        if (!prefix) { this.draftButtons = []; this.executeButtons = []; this.el.suggestions.replaceChildren(); }
        for (let i = start; i < suggestions.length; i++) this.renderSuggestion(suggestions[i], i);
        this.renderedSuggestions = suggestions; this.cardsActionCapabilities = capabilities; this.cardsSignature = signature;
      }
      for (const button of this.draftButtons) button.disabled = this.busy || this.messageBusy || this.suspended || state !== "ready";
      const executionKey = this.executionKey(); const submission = this.executions.get(executionKey);
      for (const { button, prompt } of this.executeButtons) {
        button.dataset.resultKey = executionKey;
        button.disabled = this.busy || this.messageBusy || this.suspended || state !== "ready" || !!submission;
        button.textContent = submission?.prompt === prompt ? ({ pending: "正在提交…", sent: "已提交", unknown: "待确认" })[submission.phase] : "直接执行";
      }
      this.el["draft-note"].hidden = !suggestions.length || ((supportsDraft(this.hostCapabilities) || supportsExecute(this.hostCapabilities)) && !["syncing", "streaming"].includes(state));
      this.el["draft-note"].textContent = state === "streaming" ? "首条建议已到，其余仍在生成；完成后可使用下方操作。" : state === "syncing" ? "正在同步当前对话，确认后即可操作。" : "当前宿主未开放消息能力，可复制建议后粘贴。";
      const usage = snapshot?.usage || {};
      const knownParts = [usage.input_tokens, usage.output_tokens].filter(Number.isFinite);
      const tokens = Number.isFinite(usage.total_tokens) ? usage.total_tokens :
        knownParts.length ? knownParts.reduce((total, value) => total + value, 0) : null;
      const hasCalls = Number.isFinite(usage.calls) && usage.calls > 0;
      const partial = (Number.isFinite(usage.unknown_calls) && usage.unknown_calls > 0) ||
        (!Number.isFinite(usage.total_tokens) && knownParts.length === 1);
      this.el.usage.hidden = !hasCalls;
      this.el["usage-label"].textContent = `用量记录${hasCalls ? ` · ${usage.calls.toLocaleString()} 次尝试` : ""}`;
      this.el["usage-calls"].textContent = String(usage.calls || 0); this.el["usage-failures"].textContent = String(usage.failed_calls || 0);
      this.renderRecords(snapshot?.recent_jobs || []);
      this.el.usage.textContent = hasCalls ? `本聊天累计 ${usage.calls.toLocaleString()} 次生成尝试 · ${Number.isFinite(tokens) ? `已知 ${tokens.toLocaleString()} tokens${partial ? " · 部分用量未知" : ""}` : "用量尚未返回"}` : "";
    }
    renderRecords(records) {
      const list = Array.isArray(records) ? records.slice(0, 20) : [];
      const signature = JSON.stringify(list); if (signature === this.recordsSignature) return; this.recordsSignature = signature;
      const target = this.el["usage-records"]; target.replaceChildren();
      const make = (tag, className, text) => { const node = this.doc.createElement(tag); node.className = className; node.textContent = text; return node; };
      if (!list.length) { target.append(make("p", "usage-note", "还没有生成记录。")); return; }
      for (const record of list) {
        const item = make("article", "record", ""); const head = make("div", "record-head", "");
        head.append(make("h3", "", record.origin === "manual" ? "手动刷新" : "自动生成"), make("span", "record-state", ({completed:"已完成",error:"未成功",stale:"已过期",running:"生成中",verifying:"复核中",cancelled:"已取消"})[record.state] || "已记录"));
        const time = Number.isFinite(record.invoked_at) ? new Date(record.invoked_at * 1000).toLocaleString("zh-CN", {hour12:false}) : "时间未知";
        const duration = Number.isFinite(record.duration_seconds) ? `${record.duration_seconds.toFixed(1)} 秒` : "耗时尚未返回";
        item.append(head, make("p", "record-meta", `${time} · 调用至复核完成 ${duration} · 第 ${record.ordinal} 次`));
        const tokens = make("div", "tokens", "");
        for (const [key, label] of [["input_tokens", "输入 tokens"], ["output_tokens", "输出 tokens"]]) { const cell = make("span", "", label); const value = record.usage?.[key]; cell.append(make("b", "", Number.isFinite(value) ? value.toLocaleString() : "未知")); tokens.append(cell); }
        item.append(tokens);
        const performance = record.performance || {};
        if (!Object.keys(performance).length) item.append(make("p", "usage-note performance-note", "分段耗时未采集"));
        else {
          const detail = make("details", "performance", ""); detail.append(make("summary", "", "分段耗时"));
          const seconds = value => Number.isFinite(value) && value >= 0 ? `${(value / 1000).toFixed(2)} 秒` : "未观测";
          if (Number.isFinite(performance.request_finished_ms)) {
            detail.append(make("p", "performance-note", "以下到达时间从 HTTP 请求开始累计。"));
            for (const [key, label] of [["headers_ms", "响应头"], ["first_text_ms", "首个文本增量"],
              ["first_suggestion_ms", "第一条完整建议"], ["completed_ms", "完成事件"], ["request_finished_ms", "请求结束"]])
              detail.append(make("p", "performance-row", `${label}：${seconds(performance[key])}`));
            if (performance.response_mode === "json") detail.append(make("p", "performance-note", "服务返回完整 JSON，未观测文本增量。"));
          }
          for (const [key, label] of [["preview_ready_ms", "首条通过复核（调用后）"], ["preview_verification_ms", "首条复核耗时"],
            ["generation_ms", "模型调用总计"], ["verification_queue_ms", "复核排队"],
            ["verification_read_ms", "复核读取"], ["verification_ms", "返回后复核总计"],
            ["displayed_ms", performance.display_stage === "preview" ? "首卡可见（提前展示）" : "调用至卡片可见"],
            ["all_displayed_ms", "全部建议可见（调用后）"],
            ["display_after_preview_ms", "首条复核至可见"], ["display_after_ready_ms", "完整复核至可见"]])
            detail.append(make("p", "performance-row", `${label}：${seconds(performance[key])}`));
          detail.append(make("p", "performance-note", "卡片可见时间由浏览器观测；未打开建议页、重开旧结果或未回传时不补算。"));
          detail.append(make("p", "performance-row", `实际服务档位：${performance.service_tier || "未返回"}`));
          item.append(detail);
        }
        target.append(item);
      }
    }
    renderSuggestion(suggestion, index) {
      if (!suggestion || typeof suggestion.prompt !== "string" || !suggestion.prompt.trim()) return;
      const make = (tag, className, text) => { const node = this.doc.createElement(tag); node.className = className; if (text != null) node.textContent = text; return node; };
      const card = make("article", "suggestion"); const heading = make("div", "title-row");
      heading.append(make("span", "index", String(index + 1).padStart(2, "0")), make("h2", "", suggestion.title || "继续这一步"));
      const reason = make("p", "reason-content", String(suggestion.reason || "")); reason.hidden = true; reason.setAttribute("id", `reason-${index}`);
      if (suggestion.reason) { const toggle = make("button", "reason-button", "理由"); toggle.type = "button"; toggle.setAttribute("aria-controls", `reason-${index}`); toggle.setAttribute("aria-expanded", "false"); toggle.addEventListener("click", () => { reason.hidden = !reason.hidden; toggle.setAttribute("aria-expanded", String(!reason.hidden)); }); heading.append(toggle); }
      card.append(heading, reason, make("p", "suggestion-prompt", suggestion.prompt));
      const actions = make("div", "card-actions");
      if (supportsDraft(this.hostCapabilities)) { const draft = make("button", "primary draft-button", "填入草稿"); draft.type = "button"; draft.id = `suggestion-draft-${index}`; draft.addEventListener("click", async () => { draft.disabled = true; try { await this.draft(suggestion.prompt); } finally { draft.disabled = this.busy || this.messageBusy || this.suspended || this.currentState() !== "ready"; } }); this.draftButtons.push(draft); actions.append(draft); }
      if (supportsExecute(this.hostCapabilities)) {
        const execute = make("button", "secondary execute-button", "直接执行"); execute.type = "button";
        execute.id = `suggestion-execute-${index}`;
        execute.title = "将此建议发送到当前聊天并立即执行";
        execute.addEventListener("click", () => this.execute(suggestion.prompt, execute.dataset.resultKey));
        this.executeButtons.push({ button: execute, prompt: suggestion.prompt }); actions.append(execute);
      }
      const copy = make("button", supportsDraft(this.hostCapabilities) || supportsExecute(this.hostCapabilities) ? "copy-button" : "primary", "复制建议"); copy.type = "button"; copy.id = `suggestion-copy-${index}`; copy.addEventListener("click", () => this.copy(suggestion.prompt)); actions.append(copy); card.append(actions); this.el.suggestions.append(card);
    }
    async dispose() {
      if (this.closed) return;
      this.closed = true; this.epoch++; this.win.clearTimeout(this.pollTimer); this.win.clearTimeout(this.noticeTimer); this.win.clearTimeout(this.updateTimer);
      this.observer?.disconnect();
      this.doc.removeEventListener("visibilitychange", this.onVisibility);
      this.win.removeEventListener("pagehide", this.onPageHide); this.win.removeEventListener("pageshow", this.onPageShow);
      if (this.initialized && this.snapshot?.panel_id) { try { await this.tool("next_close", { panel_id: this.snapshot.panel_id }, 700); } catch (_) { /* lease expires if host disappears */ } }
      this.render();
    }
    async close() {
      await this.dispose();
      try { this.bridge.notify("ui/notifications/request-teardown"); } catch (_) { /* host gone */ }
    }
  }

  global.MogooNext = { Bridge, Panel, supportsDraft, supportsExecute, draftParams, modelEntry, structured };
  if (global.document?.getElementById("app")) { const panel = new Panel(global, global.document); global.MogooNext.panel = panel; panel.start(); }
})(globalThis);
