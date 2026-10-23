/* MCP Apps bridge. No host DOM access, external resources, or model calls. */
(function (global) {
  "use strict";

  const POLL_MS = 2000;
  const RPC_TIMEOUT_MS = 10000;
  const RELAY_PRESETS = { aicodewith: "https://api.aicodewith.ai/chatgpt/v1" };
  const STATES = {
    connecting: ["·", "正在连接", "在当前对话中打开面板即可查看建议。"],
    unbound: ["↗", "尚未绑定对话", "请从需要建议的对话右侧打开面板。宿主未提供对话标识时，不会读取其他聊天。"],
    unconfigured: ["◇", "选择建议模型", "为下一步建议选择一个独立模型，也可以使用中转服务。"],
    waiting: ["·", "等这轮回复结束", "当前对话完成一轮回复后，这里会出现下一步建议。"],
    syncing: ["·", "正在同步对话", "正在确认当前对话的最新状态。"],
    generating: ["✳", "正在生成建议", "结合最近的对话生成建议，通常需要一点时间。"],
    ready: ["", "", ""],
    empty: ["✓", "暂时没有后续建议", "可以继续聊天，或重新生成一组建议。"],
    paused: ["Ⅱ", "自动建议已暂停", "打开自动建议后继续生成，也可以手动刷新这一轮。"],
    error: ["!", "暂时无法生成建议", "请检查模型和服务配置，再手动刷新。"],
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

  function publicError(error) { return error instanceof Error ? error.message : "请求失败，请重试。"; }
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
  function modelEntry(model) {
    if (typeof model === "string") return { id: model, label: model };
    if (!model || typeof model !== "object") return null;
    const id = model.id || model.model || model.slug;
    if (typeof id !== "string" || !id) return null;
    return { id, label: String(model.displayName || model.display_name || model.name || id) };
  }

  class Panel {
    constructor(win, doc, bridge = new Bridge(win)) {
      this.win = win; this.doc = doc; this.bridge = bridge;
      this.snapshot = null; this.config = {}; this.hostCapabilities = {}; this.hostContext = {};
      this.initialized = false; this.closed = false; this.busy = false; this.polling = false;
      this.inViewport = true; this.pollTimer = null; this.noticeTimer = null; this.epoch = 0;
      this.settingsDirty = false; this.settingsOpen = false; this.connectionError = "";
      this.catalogEpoch = 0; this.catalogKey = ""; this.catalogLoading = false;
      this.modelEntries = []; this.modelPickerOpen = false; this.filteredModels = [];
      this.suspended = false; this.view = "suggestions"; this.skin = "graphite"; this.skinBusy = false; this.appearanceEpoch = 0; this.viewPositions = {};
      this.pendingAuto = null;
      this.previewSuggestions = []; this.draftButtons = [];
      this.el = {};
      for (const id of ["thread-title", "auto-generate", "refresh", "model-summary", "status", "status-symbol", "status-title", "status-detail", "status-action", "suggestions", "draft-note", "usage", "settings", "settings-toggle", "settings-close", "settings-form", "provider", "model", "model-list", "load-models", "models-status", "reasoning-effort", "service-tier", "relay-fields", "base-url", "api-key", "api-key-status", "clear-api-key", "clear-api-key-row", "codex-bin", "settings-message", "save-settings", "notice", "close-panel", "app", "panel-scroll", "suggestion-header", "view-header", "view-title", "suggestion-view", "usage-view", "savebar", "settings-dirty", "suggestion-count", "check-model", "usage-open", "usage-label", "usage-arrow", "usage-calls", "usage-failures", "usage-records", "skin-graphite", "skin-annotation", "skin-focus", "discard-dialog", "discard-continue", "discard-confirm"]) this.el[id] = doc.getElementById(id);
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
      for (const id of ["relay-preset", "relay-preset-field", "relay-preset-hint", "model-picker", "model-toggle", "model-menu", "model-search", "model-count", "model-empty"]) this.el[id] = doc.getElementById(id);
      this.onModelOutside = event => { if (!this.el["model-picker"].contains(event.target)) this.closeModelPicker(); };
      doc.addEventListener("pointerdown", this.onModelOutside);
      this.bindControls(); this.render();
    }
    isVisible() { return this.doc.visibilityState !== "hidden" && this.inViewport && !this.closed; }
    async start() {
      try {
        const info = await this.bridge.request("ui/initialize", {
          appInfo: { name: "mogoo-codex-next", version: "0.1.13" },
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
      } catch (error) { this.connectionError = publicError(error); this.render(); }
    }
    bindControls() {
      this.el["settings-toggle"].addEventListener("click", () => this.showSettings(!this.settingsOpen));
      this.el["settings-close"].addEventListener("click", () => this.showSettings(false));
      this.el.refresh.addEventListener("click", () => this.action("next_refresh"));
      this.el["auto-generate"].addEventListener("change", () => {
        const enabled = this.el["auto-generate"].checked;
        this.pendingAuto = enabled;
        this.action("next_auto", { enabled }).finally(() => { this.pendingAuto = null; this.render(); });
      });
      this.el["status-action"].addEventListener("click", () => {
        if (this.currentState() === "unconfigured") this.showSettings(true);
        else if (!this.snapshot?.panel_id) this.reconnect();
        else this.action("next_refresh");
      });
      this.el["settings-form"].addEventListener("input", (event) => { if (event?.target === this.el["model-search"]) return; this.settingsDirty = true; this.el["settings-dirty"].hidden = false; if (event?.target === this.el["api-key"] && this.el["api-key"].value) this.el["clear-api-key"].checked = false; if (event?.target === this.el["base-url"]) { this.el["api-key"].value = ""; this.el["clear-api-key"].checked = false; } this.syncCatalogSource(); if (event?.target === this.el.model) this.renderModelPicker(); });
      this.el["clear-api-key"].addEventListener("change", () => { this.settingsDirty = true; this.el["settings-dirty"].hidden = false; if (this.el["clear-api-key"].checked) this.el["api-key"].value = ""; this.catalogKey = ""; this.syncCatalogSource(); });
      this.el.provider.addEventListener("change", () => { this.settingsDirty = true; this.showRelay(); });
      this.el["relay-preset"].addEventListener("change", () => this.chooseRelayPreset());
      this.el["settings-form"].addEventListener("submit", (event) => { event.preventDefault(); this.saveSettings(); });
      this.el["load-models"].addEventListener("click", () => this.loadModels());
      this.el["model-toggle"].addEventListener("click", () => this.modelPickerOpen ? this.closeModelPicker(true) : this.openModelPicker());
      this.el.model.addEventListener("keydown", event => {
        if (event.key === "ArrowDown") { event.preventDefault(); this.openModelPicker(); }
        else if (event.key === "Escape") this.closeModelPicker();
      });
      this.el["model-search"].addEventListener("input", () => this.renderModelPicker());
      this.el["model-search"].addEventListener("keydown", event => {
        if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); this.focusModelOption(event.key === "ArrowDown" ? 0 : this.filteredModels.length - 1); }
        else if (event.key === "Enter") { event.preventDefault(); if (this.filteredModels[0]) this.selectModel(this.filteredModels[0]); }
        else if (event.key === "Escape") { event.preventDefault(); this.closeModelPicker(true); }
      });
      this.el["model-picker"].addEventListener("focusout", event => { if (event.relatedTarget && !this.el["model-picker"].contains(event.relatedTarget)) this.closeModelPicker(); });
      this.el["close-panel"].addEventListener("click", () => this.close());
      this.el["model-summary"].addEventListener("click", () => this.showSettings(true));
      this.el["usage-open"].addEventListener("click", () => this.openView("usage"));
      this.el["check-model"].addEventListener("click", () => this.showSettings(true));
      for (const skin of ["graphite", "annotation", "focus"]) this.el[`skin-${skin}`].addEventListener("click", () => this.changeSkin(skin));
      this.el["discard-continue"].addEventListener("click", () => this.el["discard-dialog"].close?.());
      this.el["discard-confirm"].addEventListener("click", () => { this.settingsDirty = false; this.fillSettings(); this.el["discard-dialog"].close?.(); this.openView(this.pendingView || "suggestions"); });
    }
    async tool(name, args = {}, timeout = RPC_TIMEOUT_MS) {
      return structured(await this.bridge.request("tools/call", { name, arguments: args }, timeout));
    }
    openModelPicker() {
      if (!this.initialized || this.busy || this.closed) return;
      this.modelPickerOpen = true; this.el["model-search"].value = "";
      this.renderModelPicker(); this.el["model-search"].focus();
      this.el["model-menu"].scrollIntoView?.({ block: "nearest" });
    }
    closeModelPicker(focus = false) {
      this.modelPickerOpen = false; this.el["model-menu"].hidden = true;
      this.el["model-toggle"].setAttribute("aria-expanded", "false");
      if (focus) this.el.model.focus();
    }
    focusModelOption(index) {
      const option = this.el["model-list"].children[index];
      if (option) { option.focus(); option.scrollIntoView?.({ block: "nearest" }); }
    }
    selectModel(item) {
      if (this.busy || this.closed) return;
      this.el.model.value = item.id; this.settingsDirty = true; this.el["settings-dirty"].hidden = false;
      this.settingsMessage(""); this.closeModelPicker(true); this.renderModelPicker();
    }
    renderModelPicker() {
      const query = this.el["model-search"].value.trim().toLocaleLowerCase();
      this.filteredModels = this.modelEntries.filter(item => `${item.id} ${item.label}`.toLocaleLowerCase().includes(query));
      this.el["model-menu"].hidden = !this.modelPickerOpen;
      this.el["model-toggle"].setAttribute("aria-expanded", String(this.modelPickerOpen));
      this.el["model-count"].textContent = query ? `${this.filteredModels.length} / ${this.modelEntries.length} 个模型` : `${this.modelEntries.length} 个可选模型`;
      this.el["model-empty"].hidden = this.filteredModels.length > 0;
      this.el["model-empty"].textContent = this.catalogLoading ? "正在读取模型目录…" : this.modelEntries.length ? "没有匹配的模型，可以修改搜索词或直接输入模型 ID。" : "请先读取模型目录，也可以直接输入模型 ID。";
      this.el["model-list"].replaceChildren();
      this.filteredModels.forEach((item, index) => {
        const option = this.doc.createElement("button"); option.type = "button"; option.className = "model-option"; option.value = item.id;
        option.setAttribute("role", "option"); option.setAttribute("aria-selected", String(item.id === this.el.model.value.trim()));
        option.tabIndex = item.id === this.el.model.value.trim() ? 0 : -1;
        option.disabled = !this.initialized || this.busy || this.closed;
        const id = this.doc.createElement("span"); id.className = "model-option-id"; id.textContent = item.id; option.append(id);
        if (item.label !== item.id) { const label = this.doc.createElement("span"); label.className = "model-option-label"; label.textContent = item.label; option.append(label); }
        option.addEventListener("click", () => this.selectModel(item));
        option.addEventListener("keydown", event => {
          if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
            event.preventDefault();
            const target = event.key === "Home" ? 0 : event.key === "End" ? this.filteredModels.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + this.filteredModels.length) % this.filteredModels.length;
            this.focusModelOption(target);
          } else if (event.key === "Escape") { event.preventDefault(); this.closeModelPicker(true); }
          else if (event.key === "Enter") { event.preventDefault(); this.selectModel(item); }
        });
        this.el["model-list"].append(option);
      });
    }
    onHostMessage(message) {
      if (message.method === "ui/notifications/tool-result") {
        try { this.acceptResult(structured(message.params), true); }
        catch (error) { this.connectionError = publicError(error); this.render(); }
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
      if (!data || typeof data !== "object") return;
      if (data.appearance && !this.skinBusy && appearanceEpoch === this.appearanceEpoch) this.applyAppearance(data.appearance);
      if (!data.panel_id || !data.thread_id) {
        if (notification && data.status === "unbound") {
          const previous = this.snapshot;
          this.snapshot = null; this.previewSuggestions = []; this.pendingAuto = null; this.epoch++; this.win.clearTimeout(this.pollTimer);
          this.connectionError = typeof data.error === "string" ? data.error : "";
          if (previous?.panel_id) this.tool("next_close", { panel_id: previous.panel_id }, 700).catch(() => {});
        }
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
        this.tool("next_close", { panel_id: previous.panel_id }, 700).catch(() => {});
      }
      this.snapshot = data; this.connectionError = "";
      if (data.config) this.config = data.config;
      if (!this.settingsDirty) this.fillSettings();
      this.render(); this.schedulePoll();
      if (!this.isVisible()) this.suspend(true);
    }
    currentState() {
      if (this.closed) return "closed";
      if (this.connectionError) return this.snapshot?.panel_id ? "error" : "unbound";
      if (!this.snapshot?.panel_id) return this.initialized ? "unbound" : "connecting";
      if (this.snapshot.status === "ready" && (!Array.isArray(this.snapshot.suggestions) || !this.snapshot.suggestions.some((item) => item && typeof item.prompt === "string" && item.prompt.trim()))) return "empty";
      return STATES[this.snapshot.status] ? this.snapshot.status : "waiting";
    }
    schedulePoll(delay = POLL_MS) {
      this.win.clearTimeout(this.pollTimer); this.pollTimer = null;
      if (!this.initialized || !this.snapshot?.panel_id || !this.isVisible()) return;
      this.pollTimer = this.win.setTimeout(() => this.poll(), delay);
    }
    async poll() {
      if (this.polling || this.busy || !this.isVisible() || !this.snapshot?.panel_id) { this.schedulePoll(); return; }
      const epoch = this.epoch; const appearanceEpoch = this.appearanceEpoch; const panelId = this.snapshot.panel_id;
      this.polling = true;
      try { const data = await this.tool("next_poll", { panel_id: panelId }); if (epoch === this.epoch && this.isVisible()) this.acceptResult(data, false, appearanceEpoch); }
      catch (error) { if (epoch === this.epoch && this.isVisible()) { this.connectionError = publicError(error); this.render(); } }
      finally { this.polling = false; this.schedulePoll(); }
    }
    suspend(closeLease) {
      this.closeModelPicker();
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
    }
    async reconnect() {
      if (this.busy || this.closed || !this.isVisible()) return;
      if (!this.initialized) { this.connectionError = ""; await this.start(); return; }
      this.busy = true; this.render();
      try { const data = await this.tool("next_panel"); this.suspended = false; this.acceptResult(data, true); }
      catch (error) { this.connectionError = publicError(error); this.render(); }
      finally { this.busy = false; this.render(); }
    }
    async action(name, args = {}) {
      if (this.busy || !this.snapshot?.panel_id || !this.isVisible()) return;
      const epoch = this.epoch; const appearanceEpoch = this.appearanceEpoch;
      this.busy = true; this.render();
      try { const data = await this.tool(name, { panel_id: this.snapshot.panel_id, ...args }); if (epoch === this.epoch) this.acceptResult(data, false, appearanceEpoch); }
      catch (error) { this.notice(publicError(error), true); }
      finally { this.busy = false; this.render(); this.schedulePoll(); }
    }
    async draft(prompt) {
      if (!supportsDraft(this.hostCapabilities)) { this.notice("当前宿主不支持填入草稿，请复制建议。", true); return; }
      if (!this.snapshot?.panel_id || !this.isVisible() || this.busy || this.suspended || this.currentState() !== "ready") return;
      try { await this.bridge.request("ui/message", draftParams(prompt)); this.notice("已填入输入框，可编辑后发送。"); }
      catch (error) { this.notice(`未填入草稿：${publicError(error)} 可使用复制。`, true); }
    }
    async copy(prompt) {
      try {
        if (!this.win.navigator.clipboard?.writeText) throw new Error("当前环境不支持剪贴板。");
        await this.win.navigator.clipboard.writeText(prompt); this.notice("已复制建议。");
      } catch (_) { this.notice("复制未成功，请选中建议文字手动复制。", true); }
    }
    notice(message, error = false) {
      this.el.notice.textContent = message; this.el.notice.dataset.error = String(error); this.el.notice.hidden = false;
      this.win.clearTimeout(this.noticeTimer);
      this.noticeTimer = this.win.setTimeout(() => { this.el.notice.hidden = true; }, 5000);
    }
    openView(next) {
      if (next === this.view) return true;
      if (this.busy && this.view === "settings") return false;
      if (this.view === "settings" && this.settingsDirty) { this.pendingView = next; this.el["discard-dialog"].showModal?.(); return false; }
      this.closeModelPicker();
      this.viewPositions[this.view] = this.el["panel-scroll"].scrollTop || 0;
      this.view = next; this.settingsOpen = next === "settings"; this.renderView();
      this.el["panel-scroll"].scrollTop = this.viewPositions[next] || 0;
      return true;
    }
    renderView() {
      const next = this.view;
      this.el["suggestion-header"].hidden = next !== "suggestions"; this.el["view-header"].hidden = next === "suggestions";
      this.el["suggestion-view"].hidden = next !== "suggestions"; this.el.settings.hidden = next !== "settings";
      this.el["usage-view"].hidden = next !== "usage"; this.el.savebar.hidden = next !== "settings";
      this.el["view-title"].textContent = next === "settings" ? "建议设置" : "用量记录";
      this.el["settings-toggle"].setAttribute("aria-expanded", String(next === "settings"));
      this.el["settings-toggle"].setAttribute("aria-current", next === "settings" ? "page" : "false");
      this.el["settings-toggle"].disabled = next === "settings";
      this.el["usage-open"].setAttribute("aria-current", next === "usage" ? "page" : "false");
      this.el["usage-arrow"].hidden = next === "usage";
      this.el["usage-open"].disabled = next === "usage";
      this.el["settings-dirty"].hidden = !this.settingsDirty;
    }
    async showSettings(show) {
      if (!this.openView(show ? "settings" : "suggestions")) return;
      if (show && this.initialized && !this.settingsDirty) {
        try { const data = await this.tool("next_settings"); if (data?.config && !this.settingsDirty) { this.config = data.config; this.fillSettings(); this.render(); } }
        catch (error) { this.settingsMessage(publicError(error), true); }
      }
    }
    applyAppearance(appearance) {
      if (!["graphite", "annotation", "focus"].includes(appearance?.skin)) return;
      this.skin = appearance.skin; this.el.app.dataset.skin = this.skin;
      this.el.app.style.setProperty("--skin-index", String(["graphite", "annotation", "focus"].indexOf(this.skin)));
      for (const skin of ["graphite", "annotation", "focus"]) this.el[`skin-${skin}`].setAttribute("aria-pressed", String(skin === this.skin));
      if (appearance.error && appearance.error !== this.appearanceWarning) { this.appearanceWarning = appearance.error; this.notice("无法读取外观设置，请重新选择皮肤。", true); }
    }
    async changeSkin(skin) {
      if (this.skinBusy || !this.initialized || this.closed || skin === this.skin || !["graphite", "annotation", "focus"].includes(skin)) return;
      this.skinBusy = true; this.appearanceEpoch++; this.render();
      try { const data = await this.tool("next_appearance", { skin }); if (this.closed) return; this.applyAppearance(data?.appearance); }
      catch (_) { this.notice("外观没有保存，请重试。", true); }
      finally { this.skinBusy = false; this.render(); }
    }
    fillSettings() {
      const config = this.config;
      this.el.provider.value = config.provider === "relay" ? "relay" : "official";
      for (const [id, field] of [["model", "model"], ["reasoning-effort", "reasoning_effort"], ["service-tier", "service_tier"], ["base-url", "base_url"], ["codex-bin", "codex_bin"]]) this.el[id].value = typeof config[field] === "string" ? config[field] : (field === "reasoning_effort" ? "low" : "");
      this.el["api-key"].value = "";
      this.el["clear-api-key"].checked = false;
      const address = this.el["base-url"].value.trim().replace(/\/+$/, "");
      this.el["relay-preset"].value = Object.keys(RELAY_PRESETS).find(id => RELAY_PRESETS[id] === address) || (address ? "custom" : "aicodewith");
      this.el["clear-api-key-row"].hidden = !config.api_key_present;
      this.showRelay();
    }
    savedRelayKeyForAddress() {
      return !!this.config.api_key_present && this.el["base-url"].value.trim().replace(/\/+$/, "") === (this.config.base_url || "").replace(/\/+$/, "");
    }
    updateRelayKeyHint() {
      const saved = this.savedRelayKeyForAddress();
      this.el["api-key"].placeholder = saved ? "已保存，留空继续使用；填写可替换" : "粘贴所选中转站的 API Key";
      this.el["api-key-status"].textContent = saved ? (this.config.api_key_error || "API Key 已保存，留空继续使用。") : "请输入所选中转站的 API Key，加密保存在本机。";
    }
    syncRelayPreset() {
      const preset = RELAY_PRESETS[this.el["relay-preset"].value];
      this.el["base-url"].readOnly = !!preset;
      this.el["relay-preset-hint"].textContent = preset ? "API 地址已填好，填写对应的 API Key 即可。" : "使用其他中转站时，填写 API 地址和对应的 API Key。";
    }
    chooseRelayPreset() {
      if (this.busy || this.closed) return;
      const preset = RELAY_PRESETS[this.el["relay-preset"].value];
      if (preset && this.el["base-url"].value.trim().replace(/\/+$/, "") !== preset) {
        this.el["base-url"].value = preset;
        this.el["api-key"].value = "";
        this.el["clear-api-key"].checked = false;
      }
      this.settingsDirty = true; this.el["settings-dirty"].hidden = false;
      this.settingsMessage(""); this.syncRelayPreset(); this.syncCatalogSource();
    }
    hasRelayKey() {
      return !this.el["clear-api-key"].checked && (!!this.el["api-key"].value.trim() ||
        this.savedRelayKeyForAddress());
    }
    catalogSource() {
      return this.el.provider.value === "relay" ? { provider: "relay", base_url: this.el["base-url"].value.trim(), ...(this.el["api-key"].value.trim() ? { api_key: this.el["api-key"].value.trim() } : {}) } :
        { provider: "official", codex_bin: this.el["codex-bin"].value.trim() || null };
    }
    syncCatalogSource() {
      this.updateRelayKeyHint();
      const source = this.catalogSource(); const key = JSON.stringify(source);
      if (key === this.catalogKey) return;
      this.catalogKey = key; this.catalogEpoch++; this.catalogLoading = false;
      this.modelEntries = []; this.closeModelPicker(); this.renderModelPicker(); this.el["load-models"].disabled = !this.initialized || this.busy || this.closed;
      this.el["models-status"].textContent = source.provider === "relay" ?
        (!source.base_url || !this.hasRelayKey() ? "请先填写中转 API 地址和 API Key，再读取目录；也可手动输入模型 ID。" : "将读取当前中转服务的模型目录，也可手动输入模型 ID。") :
        "可读取 Codex 官方模型目录，或手动输入模型 ID。";
    }
    showRelay() {
      const relay = this.el.provider.value === "relay";
      this.el["relay-fields"].hidden = !relay; this.el["relay-preset-field"].hidden = !relay;
      if (relay && !this.el["base-url"].value.trim() && RELAY_PRESETS[this.el["relay-preset"].value]) this.el["base-url"].value = RELAY_PRESETS[this.el["relay-preset"].value];
      this.syncRelayPreset(); this.syncCatalogSource();
    }
    settingsMessage(message, error = false) { this.el["settings-message"].textContent = message; this.el["settings-message"].dataset.error = String(error); }
    async saveSettings() {
      if (!this.initialized || this.busy) return;
      const model = this.el.model.value.trim();
      if (!model) { this.settingsMessage("请明确选择或输入建议模型。", true); this.el.model.focus(); return; }
      const changes = {
        provider: this.el.provider.value, model,
        reasoning_effort: this.el["reasoning-effort"].value,
        service_tier: this.el["service-tier"].value,
        base_url: this.el["base-url"].value.trim(),
        ...(this.el["api-key"].value.trim() ? { api_key: this.el["api-key"].value.trim() } : {}),
        clear_api_key: this.el["clear-api-key"].checked,
        codex_bin: this.el["codex-bin"].value.trim() || null,
      };
      if (changes.provider === "relay" && !changes.clear_api_key && (!changes.base_url || !this.hasRelayKey())) {
        this.settingsMessage("请填写中转 API 地址和 API Key；更换地址后需重新填写 Key。", true); return;
      }
      this.busy = true; this.render(); this.settingsMessage("正在保存…");
      try {
        const data = await this.tool("next_configure", { changes });
        if (data?.config) this.config = data.config;
        this.settingsDirty = false; this.fillSettings(); this.busy = false; this.openView("suggestions"); this.notice("建议设置已保存。"); this.schedulePoll(0);
      } catch (error) { this.settingsMessage(publicError(error), true); }
      finally { this.busy = false; this.render(); }
    }
    async loadModels() {
      if (!this.initialized || this.closed || this.busy || this.el["load-models"].disabled) return;
      this.syncCatalogSource();
      const source = this.catalogSource();
      if (source.provider === "relay" && (!source.base_url || !this.hasRelayKey())) {
        this.modelEntries = []; this.renderModelPicker(); this.el["models-status"].textContent = "请先填写中转 API 地址和 API Key，再读取目录；也可手动输入模型 ID。"; return;
      }
      const epoch = ++this.catalogEpoch; const key = JSON.stringify(source);
      const current = () => !this.closed && epoch === this.catalogEpoch && key === JSON.stringify(this.catalogSource());
      this.catalogLoading = true;
      this.modelEntries = []; this.renderModelPicker();
      this.el["load-models"].disabled = true; this.el["models-status"].textContent = source.provider === "relay" ? "正在读取中转模型目录…" : "正在读取 Codex 官方模型目录…";
      try {
        const data = await this.tool("next_models", { source }, 15000);
        if (!current()) return;
        if (data?.provider !== source.provider) throw new Error("返回的模型目录来源不匹配，请重新读取或手动输入模型 ID。");
        const entries = (data?.models || []).map(modelEntry).filter(Boolean);
        this.modelEntries = [...new Map(entries.map(item => [item.id, item])).values()]; this.renderModelPicker();
        const label = source.provider === "relay" ? "中转" : "Codex 官方";
        this.el["models-status"].textContent = entries.length ? `${label}目录包含 ${entries.length} 个模型，可选择或手动输入。目录可读不代表模型可调用。` : `${label}目录为空，可以手动输入模型 ID。`;
      } catch (error) { if (current()) { const message = publicError(error); this.el["models-status"].textContent = message.includes("手动输入") ? message : `${message} 仍可手动输入模型 ID。`; } }
      finally { if (epoch === this.catalogEpoch) { this.catalogLoading = false; this.renderModelPicker(); this.el["load-models"].disabled = !this.initialized || this.busy || this.closed; } }
    }
    render() {
      this.renderView();
      const state = this.currentState(); const snapshot = this.snapshot; const bound = !!snapshot?.panel_id && !this.closed;
      this.el["thread-title"].textContent = bound ? (snapshot.title || "当前对话") : "尚未绑定当前对话";
      this.el["thread-title"].parentElement.classList.toggle("bound", bound);
      this.el["auto-generate"].checked = this.pendingAuto ?? snapshot?.auto_generate ?? this.config.auto_generate ?? true;
      this.el["auto-generate"].disabled = !bound || this.busy;
      this.el.refresh.disabled = !bound || this.busy || ["unconfigured", "generating", "syncing"].includes(state);
      this.el["save-settings"].disabled = !this.initialized || this.busy || this.closed;
      this.el["load-models"].disabled = !this.initialized || this.busy || this.closed || this.catalogLoading;
      this.el["relay-preset"].disabled = !this.initialized || this.busy || this.closed;
      this.el["model-toggle"].disabled = !this.initialized || this.busy || this.closed;
      this.el["model-search"].disabled = !this.initialized || this.busy || this.closed;
      for (const option of this.el["model-list"].children) option.disabled = !this.initialized || this.busy || this.closed;
      const provider = this.config.provider === "relay" ? "中转" : "官方";
      const displayModel = this.config.model === "gpt-6.1-sol" ? "GPT-6.1 Sol" : this.config.model;
      this.el["model-summary"].textContent = this.config.model ? `${provider} · ${displayModel}${this.config.reasoning_effort ? ` · ${this.config.reasoning_effort}` : ""}` : "选择独立的建议模型";
      const displayed = state === "ready" ? snapshot?.suggestions : state === "syncing" ? this.previewSuggestions : [];
      const suggestions = Array.isArray(displayed) ? displayed.filter((item) => item && typeof item.prompt === "string" && item.prompt.trim()).slice(0, 3) : [];
      this.el.status.hidden = suggestions.length > 0;
      this.el.status.dataset.state = state;
      const description = STATES[state];
      this.el["status-symbol"].textContent = description[0]; this.el["status-title"].textContent = description[1];
      this.el["status-detail"].textContent = this.connectionError || (state === "error" && snapshot?.error ? String(snapshot.error) : description[2]);
      const hasAction = ["unbound", "unconfigured", "error", "empty", "paused"].includes(state);
      this.el["status-action"].hidden = !hasAction; this.el["status-action"].disabled = this.busy;
      this.el["status-action"].textContent = state === "unconfigured" ? "选择模型" : state === "error" && bound ? "重试生成" : ["empty", "paused"].includes(state) ? "刷新建议" : "重新连接";
      this.el["check-model"].hidden = state !== "error";
      this.el["suggestion-count"].hidden = !suggestions.length; this.el["suggestion-count"].textContent = `${suggestions.length} 条`;
      for (const skin of ["graphite", "annotation", "focus"]) { this.el[`skin-${skin}`].disabled = !this.initialized || this.skinBusy || this.closed; this.el[`skin-${skin}`].setAttribute("aria-pressed", String(skin === this.skin)); }
      this.el.suggestions.hidden = !suggestions.length;
      const signature = JSON.stringify([suggestions, supportsDraft(this.hostCapabilities)]);
      if (signature !== this.cardsSignature) {
        this.cardsSignature = signature; this.draftButtons = []; this.el.suggestions.replaceChildren();
        suggestions.forEach((suggestion, index) => this.renderSuggestion(suggestion, index));
      }
      for (const button of this.draftButtons) button.disabled = this.busy || this.suspended || state !== "ready";
      this.el["draft-note"].hidden = !suggestions.length || (supportsDraft(this.hostCapabilities) && state !== "syncing");
      this.el["draft-note"].textContent = state === "syncing" ? "正在同步当前对话，确认后即可填入草稿。" : supportsDraft(this.hostCapabilities) ? "选择建议后填入输入框，可编辑后发送。" : "当前宿主未开放草稿能力，可复制建议后粘贴。";
      const usage = snapshot?.usage || {};
      const knownParts = [usage.input_tokens, usage.output_tokens].filter(Number.isFinite);
      const tokens = Number.isFinite(usage.total_tokens) ? usage.total_tokens :
        knownParts.length ? knownParts.reduce((total, value) => total + value, 0) : null;
      const hasCalls = Number.isFinite(usage.calls) && usage.calls > 0;
      const partial = (Number.isFinite(usage.unknown_calls) && usage.unknown_calls > 0) ||
        (!Number.isFinite(usage.total_tokens) && knownParts.length === 1);
      this.el.usage.hidden = !hasCalls;
      this.el["usage-label"].textContent = this.view === "usage" ? "当前：用量记录" : `用量记录${hasCalls ? ` · ${usage.calls.toLocaleString()} 次尝试` : ""}`;
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
        head.append(make("h3", "", record.origin === "manual" ? "手动刷新" : "自动生成"), make("span", "record-state", ({completed:"已完成",error:"未成功",stale:"已过期",running:"生成中",cancelled:"已取消"})[record.state] || "已记录"));
        const time = Number.isFinite(record.invoked_at) ? new Date(record.invoked_at * 1000).toLocaleString("zh-CN", {hour12:false}) : "时间未知";
        const duration = Number.isFinite(record.duration_seconds) ? `${record.duration_seconds.toFixed(1)} 秒` : "耗时尚未返回";
        item.append(head, make("p", "record-meta", `${time} · ${duration} · 第 ${record.ordinal} 次`));
        const tokens = make("div", "tokens", "");
        for (const [key, label] of [["input_tokens", "输入 tokens"], ["output_tokens", "输出 tokens"]]) { const cell = make("span", "", label); const value = record.usage?.[key]; cell.append(make("b", "", Number.isFinite(value) ? value.toLocaleString() : "未知")); tokens.append(cell); }
        item.append(tokens); target.append(item);
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
      if (supportsDraft(this.hostCapabilities)) { const draft = make("button", "primary", "填入草稿"); draft.type = "button"; draft.addEventListener("click", async () => { draft.disabled = true; try { await this.draft(suggestion.prompt); } finally { draft.disabled = this.busy || this.suspended || this.currentState() !== "ready"; } }); this.draftButtons.push(draft); actions.append(draft); }
      const copy = make("button", supportsDraft(this.hostCapabilities) ? "copy-button" : "primary", supportsDraft(this.hostCapabilities) ? "复制" : "复制建议"); copy.type = "button"; copy.addEventListener("click", () => this.copy(suggestion.prompt)); actions.append(copy); card.append(actions); this.el.suggestions.append(card);
    }
    async dispose() {
      if (this.closed) return;
      this.closed = true; this.epoch++; this.win.clearTimeout(this.pollTimer); this.win.clearTimeout(this.noticeTimer);
      this.observer?.disconnect();
      this.doc.removeEventListener("visibilitychange", this.onVisibility);
      this.doc.removeEventListener("pointerdown", this.onModelOutside); this.closeModelPicker();
      this.win.removeEventListener("pagehide", this.onPageHide); this.win.removeEventListener("pageshow", this.onPageShow);
      if (this.initialized && this.snapshot?.panel_id) { try { await this.tool("next_close", { panel_id: this.snapshot.panel_id }, 700); } catch (_) { /* lease expires if host disappears */ } }
      this.render();
    }
    async close() {
      await this.dispose();
      try { this.bridge.notify("ui/notifications/request-teardown"); } catch (_) { /* host gone */ }
    }
  }

  global.MogooNext = { Bridge, Panel, supportsDraft, draftParams, modelEntry, structured };
  if (global.document?.getElementById("app")) { const panel = new Panel(global, global.document); global.MogooNext.panel = panel; panel.start(); }
})(globalThis);
