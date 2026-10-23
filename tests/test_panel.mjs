import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../ui/panel.js", import.meta.url), "utf8");
const html = await readFile(new URL("../ui/panel.html", import.meta.url), "utf8");
const context = vm.createContext({ console });
vm.runInContext(source, context);
const { Bridge, Panel, supportsDraft, draftParams, modelEntry } = context.MogooNext;
const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

test("visibility reconnect retains the same conversation's cards while its state is rechecked", async () => {
  const trace = [snapshot(), snapshot({ panel_id: "panel-b", status: "syncing", suggestions: [] }),
    snapshot({ panel_id: "panel-b", revision: 2 })];
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.hostCapabilities = { experimental: { "openai/message": {} } };
  panel.acceptResult(trace[0]);
  const card = env.elements.suggestions.children[0];
  card.children[0].children[2].events.get("click")();
  env.elements["panel-scroll"].scrollTop = 180;
  env.respond(message => message.params.name === "next_panel" ? undefined : { structuredContent: {} });
  env.hide(); await settle(); env.show(); await settle();
  assert.equal(panel.currentState(), "ready", "reconnect itself must not clear the previous display");
  const request = env.sent.find(message => message.params?.name === "next_panel");
  env.deliver({ id: request.id, result: { structuredContent: trace[1] } });
  await settle();
  assert.equal(env.elements.suggestions.hidden, false, "existing suggestions must not flash away on app focus");
  assert.notEqual(panel.currentState(), "waiting", "rechecking is not an active chat turn");
  assert.equal(env.elements.suggestions.children[0], card);
  assert.equal(card.children[1].hidden, false, "expanded reason survives reconnect");
  assert.equal(card.children[3].children[0].disabled, true);
  assert.equal(env.elements.refresh.disabled, true);
  assert.match(env.elements["draft-note"].textContent, /正在同步/);
  await panel.draft(trace[0].suggestions[0].prompt);
  assert.equal(env.sent.filter(m => m.method === "ui/message").length, 0);
  panel.acceptResult(trace[2]);
  assert.equal(env.elements.suggestions.hidden, false);
  assert.equal(panel.currentState(), "ready");
  assert.equal(env.elements.suggestions.children[0], card);
  assert.equal(env.elements["panel-scroll"].scrollTop, 180);
  assert.equal(card.children[3].children[0].disabled, false);
  assert.equal(env.sent.filter(m => m.params?.name === "next_refresh").length, 0);
  assert.equal(env.sent.filter(m => m.method === "ui/message").length, 0);
  await panel.dispose(); env.bridge.dispose();
});

test("a replayed older entrypoint cannot replace a newer snapshot of the same conversation", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.acceptResult(snapshot({ panel_id: "panel-current", revision: 8 }));
  env.deliver({ method: "ui/notifications/tool-result", params: { structuredContent:
    snapshot({ panel_id: "panel-old", revision: 2, status: "waiting", suggestions: [] }) } });
  assert.equal(panel.snapshot.panel_id, "panel-current");
  assert.equal(env.elements.suggestions.hidden, false);
  await settle(); await panel.dispose(); env.bridge.dispose();
});

test("a confirmed new turn clears preview and late responses cannot restore its old suggestions", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot({ revision: 4 }));
  panel.acceptResult(snapshot({ panel_id: "panel-b", revision: 4, status: "syncing", suggestions: [] }), true);
  assert.equal(env.elements.suggestions.hidden, false);
  panel.acceptResult(snapshot({ panel_id: "panel-b", revision: 5, status: "waiting", suggestions: [] }));
  assert.equal(env.elements.suggestions.hidden, true);
  assert.equal(panel.previewSuggestions.length, 0);
  panel.acceptResult(snapshot({ panel_id: "panel-b", revision: 4 }));
  assert.equal(panel.currentState(), "waiting");
  assert.equal(env.elements.suggestions.hidden, true);
  await panel.dispose(); env.bridge.dispose();
});

test("syncing never borrows cards from another conversation or a changed configuration", async () => {
  for (const change of [{ thread_id: "thread-b", revision: 1 }, { revision: 2 }]) {
    const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
    panel.initialized = true; panel.acceptResult(snapshot());
    panel.acceptResult(snapshot({ panel_id: "new-panel", status: "syncing", suggestions: [], ...change }), true);
    assert.equal(env.elements.suggestions.hidden, true);
    assert.equal(panel.currentState(), "syncing");
    assert.equal(env.elements["status-title"].textContent, "正在同步对话");
    await panel.dispose(); env.bridge.dispose();
  }
});

test("repeated focus changes reuse cards and never request manual generation or send a message", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  const card = env.elements.suggestions.children[0];
  let revision = 1; let id = "panel-a";
  env.respond(message => ({ structuredContent: message.params.name === "next_panel" ?
    snapshot({ panel_id: id = `resume-${revision}`, revision, status: "syncing", suggestions: [] }) : {} }));
  for (let i = 0; i < 20; i++) {
    env.hide(); await settle(); env.show(); env.show(); await settle();
    assert.equal(env.elements.suggestions.hidden, false);
    assert.equal(env.elements.suggestions.children[0], card);
    panel.acceptResult(snapshot({ panel_id: id, revision: ++revision }));
    assert.equal(panel.currentState(), "ready");
  }
  assert.equal(env.sent.filter(m => m.params?.name === "next_panel").length, 20);
  assert.equal(env.sent.filter(m => m.params?.name === "next_refresh").length, 0);
  assert.equal(env.sent.filter(m => m.method === "ui/message").length, 0);
  await panel.dispose(); env.bridge.dispose();
});

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.textContent = "";
    this.hidden = false; this.value = ""; this.checked = false; this.disabled = false;
    this.dataset = {}; this.attrs = {}; this.events = new Map();
    this.classList = { toggle() {} };
    this.style = { setProperty() {}, removeProperty() {} };
  }
  append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(event, callback) { this.events.set(event, callback); }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  contains(target) { for (let node = target; node; node = node.parentElement) if (node === this) return true; return false; }
}

function setup() {
  const events = new Map(); const docEvents = new Map(); const timers = new Map(); const sent = [];
  let timerId = 0;
  const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()]));
  if (elements["model-picker"]) { elements["model-picker"].append(elements.model, elements["model-toggle"], elements["model-menu"]); elements["model-menu"].append(elements["model-search"], elements["model-count"], elements["model-list"], elements["model-empty"]); }
  elements["thread-title"].parentElement = new Element();
  const doc = {
    visibilityState: "visible", documentElement: new Element("html"),
    getElementById: (id) => elements[id], createElement: (tag) => new Element(tag),
    addEventListener: (event, callback) => docEvents.set(event, callback),
    removeEventListener: (event) => docEvents.delete(event),
  };
  const win = {
    parent: {}, navigator: { clipboard: { writeText: async () => {} } },
    addEventListener: (event, callback) => events.set(event, callback),
    removeEventListener: (event) => events.delete(event),
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  };
  const bridge = new Bridge(win);
  let handler = () => ({});
  const deliver = (message, source = win.parent) => events.get("message")?.({ source, data: { jsonrpc: "2.0", ...message } });
  win.parent.postMessage = (message) => {
    sent.push(message);
    if (message.id && message.method) queueMicrotask(() => {
      try { const result = handler(message); if (result !== undefined) deliver({ id: message.id, result }); }
      catch (error) { deliver({ id: message.id, error: { code: -32000, message: error.message } }); }
    });
  };
  return {
    win, doc, bridge, sent, timers, elements, deliver,
    pointer: target => docEvents.get("pointerdown")?.({ target }),
    respond: (callback) => { handler = callback; },
    hide: () => { doc.visibilityState = "hidden"; docEvents.get("visibilitychange")?.(); },
    show: () => { doc.visibilityState = "visible"; docEvents.get("visibilitychange")?.(); },
    run: (delay) => { for (const [id, timer] of [...timers]) if (timer.delay === delay) { timers.delete(id); timer.callback(); } },
  };
}

function snapshot(overrides = {}) {
  return { panel_id: "panel-a", thread_id: "thread-a", title: "当前工作", revision: 1, status: "ready", auto_generate: true,
    config: { provider: "official", model: "chosen-model", reasoning_effort: "low" },
    suggestions: [{ title: "检查修改", prompt: "请检查这次修改的边界。", reason: "便于确认结果。" }], usage: {}, ...overrides };
}

const presetUrl = "https://api.aicodewith.ai/chatgpt/v1";

test("opening the model picker shows the whole catalog without erasing the configured model", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot({ config: { provider: "official", model: "gpt-6.1-sol" } }));
  const models = [{ id: "gpt-6.1-sol" }, ...Array.from({ length: 38 }, (_, i) => ({ id: `catalog-model-${i}` }))];
  env.respond(() => ({ structuredContent: { provider: "official", models } }));
  await panel.loadModels();
  assert.ok(env.elements["model-toggle"], "the native datalist cannot provide an unfiltered, themeable picker");
  env.elements["model-toggle"].events.get("click")();
  assert.equal(env.elements["model-menu"].hidden, false);
  assert.equal(env.elements.model.value, "gpt-6.1-sol");
  assert.equal(env.elements["model-search"].value, "");
  assert.equal(env.elements["model-list"].children.length, 39);
  assert.doesNotMatch(html, /<datalist|list="model-list"/);
  assert.equal(env.sent.filter(m => m.params?.name === "next_configure" || m.params?.name === "next_refresh" || m.method === "ui/message").length, 0);
  await panel.dispose(); env.bridge.dispose();
});

test("model search filters names and IDs independently without editing or saving the current model", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.respond(() => ({ structuredContent: { provider: "official", models: [{ id: "one", displayName: "SOL Alpha" }, { id: "sol-two" }, { id: "third" }] } }));
  await panel.loadModels(); panel.openModelPicker();
  env.elements["model-search"].value = "SoL";
  env.elements["model-search"].events.get("input")();
  env.elements["settings-form"].events.get("input")({ target: env.elements["model-search"] });
  assert.deepEqual([...env.elements["model-list"].children].map(item => item.value), ["one", "sol-two"]);
  assert.equal(env.elements.model.value, "chosen-model"); assert.equal(panel.settingsDirty, false);
  env.elements["model-search"].value = "no-match"; env.elements["model-search"].events.get("input")();
  assert.equal(env.elements["model-empty"].hidden, false);
  assert.equal(env.elements.model.value, "chosen-model");
  assert.equal(env.sent.length, 1);
  await panel.dispose(); env.bridge.dispose();
});

test("model selection supports keyboard navigation and Enter never submits the settings form", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.respond(() => ({ structuredContent: { provider: "official", models: [{ id: "first" }, { id: "second" }] } }));
  await panel.loadModels(); panel.openModelPicker();
  let prevented = 0; const key = value => ({ key: value, preventDefault() { prevented++; } });
  env.elements["model-search"].events.get("keydown")(key("ArrowDown"));
  const first = env.elements["model-list"].children[0]; const second = env.elements["model-list"].children[1];
  assert.equal(first.focused, true);
  first.events.get("keydown")(key("ArrowDown")); assert.equal(second.focused, true);
  second.events.get("keydown")(key("Enter"));
  assert.equal(prevented, 3); assert.equal(env.elements.model.value, "second");
  assert.equal(panel.config.model, "chosen-model"); assert.equal(panel.settingsDirty, true);
  assert.equal(env.elements["model-menu"].hidden, true); assert.equal(env.elements.model.focused, true);
  assert.equal(env.sent.filter(m => m.params?.name === "next_configure" || m.method === "ui/message").length, 0);
  await panel.dispose(); env.bridge.dispose();
});

test("Escape and clicking outside close the picker and reopening restores all models", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.respond(() => ({ structuredContent: { provider: "official", models: [{ id: "alpha" }, { id: "beta" }] } }));
  await panel.loadModels(); panel.openModelPicker();
  env.pointer(env.elements["model-search"]); assert.equal(panel.modelPickerOpen, true);
  env.elements["model-search"].value = "alpha"; env.elements["model-search"].events.get("input")();
  env.elements["model-search"].events.get("keydown")({ key: "Escape", preventDefault() {} });
  assert.equal(env.elements["model-menu"].hidden, true); assert.equal(panel.settingsDirty, false);
  panel.openModelPicker(); assert.equal(env.elements["model-list"].children.length, 2);
  env.pointer(env.elements.provider); assert.equal(panel.modelPickerOpen, false);
  assert.equal(env.elements.model.value, "chosen-model");
  await panel.dispose(); env.bridge.dispose();
});

test("polling cannot reset an open model search or rebuild its visible options", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.respond(() => ({ structuredContent: { provider: "official", models: [{ id: "alpha" }, { id: "beta" }] } }));
  await panel.loadModels(); panel.openModelPicker();
  env.elements["model-search"].value = "alpha"; env.elements["model-search"].events.get("input")();
  const option = env.elements["model-list"].children[0]; env.elements["model-list"].scrollTop = 128;
  panel.acceptResult(snapshot({ revision: 2 }));
  assert.equal(panel.modelPickerOpen, true); assert.equal(env.elements["model-search"].value, "alpha");
  assert.equal(env.elements["model-list"].children[0], option); assert.equal(env.elements["model-list"].scrollTop, 128);
  env.elements.provider.value = "relay"; env.elements.provider.events.get("change")();
  assert.equal(panel.modelPickerOpen, false); assert.equal(env.elements["model-list"].children.length, 0);
  assert.equal(env.elements.model.value, "chosen-model");
  await panel.dispose(); env.bridge.dispose();
});

test("model catalog failures retain manual IDs and unsafe labels remain plain text", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  const label = '<img src=x onerror="alert(1)">'; const id = 'long-model-' + "segment-".repeat(50);
  env.respond(() => ({ structuredContent: { provider: "official", models: [{ id, displayName: label }] } }));
  await panel.loadModels(); panel.openModelPicker();
  const option = env.elements["model-list"].children[0];
  assert.equal(option.children[0].textContent, id); assert.equal(option.children[1].textContent, label);
  env.elements.model.value = "manual-model";
  env.respond(() => { throw new Error("目录不可用"); });
  await panel.loadModels();
  assert.equal(env.elements["model-list"].children.length, 0); assert.equal(env.elements.model.value, "manual-model");
  assert.equal(env.elements["model-empty"].hidden, false);
  await panel.dispose(); env.bridge.dispose();
});

test("relay presets default to AI Code With in the unsaved form without changing the active model", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  assert.equal(env.elements["relay-preset-field"].hidden, true);
  env.elements.provider.value = "relay"; env.elements.provider.events.get("change")();
  assert.equal(env.elements["relay-preset-field"].hidden, false);
  assert.equal(env.elements["relay-preset"].value, "aicodewith");
  assert.equal(env.elements["base-url"].value, presetUrl);
  assert.equal(env.elements["base-url"].readOnly, true);
  assert.equal(panel.config.provider, "official");
  assert.equal(env.elements.model.value, "chosen-model");
  assert.equal(env.elements["reasoning-effort"].value, "low");
  assert.equal(env.sent.length, 0);
  await panel.dispose(); env.bridge.dispose();
});

test("saved relay settings identify the preset or custom address without replacing them", async () => {
  for (const [address, preset, readOnly] of [[presetUrl + "/", "aicodewith", true], ["https://custom.example/endpoint", "custom", false]]) {
    const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
    panel.initialized = true;
    panel.acceptResult(snapshot({ config: { provider: "relay", model: "kept-model", reasoning_effort: "low", base_url: address, api_key_present: true } }));
    assert.equal(env.elements["relay-preset"].value, preset);
    assert.equal(env.elements["base-url"].value, address);
    assert.equal(env.elements["base-url"].readOnly, readOnly);
    assert.equal(env.elements["api-key"].value, "");
    assert.equal(panel.hasRelayKey(), true);
    assert.match(env.elements["api-key"].placeholder, /已保存/);
    assert.equal(env.sent.length, 0);
    await panel.dispose(); env.bridge.dispose();
  }
});

test("changing a preset clears a different service's typed key and cannot reuse its saved key", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  const config = { provider: "relay", model: "kept-model", base_url: "https://custom.example/v1", api_key_present: true };
  panel.acceptResult(snapshot({ config }));
  env.elements["api-key"].value = "synthetic-other-service-key";
  env.elements["relay-preset"].value = "aicodewith";
  env.elements["relay-preset"].events.get("change")();
  assert.equal(env.elements["base-url"].value, presetUrl);
  assert.equal(env.elements["api-key"].value, "");
  assert.equal(panel.hasRelayKey(), false);
  assert.doesNotMatch(env.elements["api-key"].placeholder, /已保存/);
  await panel.loadModels(); await panel.saveSettings();
  assert.equal(env.sent.length, 0);
  assert.equal(panel.config.base_url, config.base_url);
  assert.equal(panel.settingsDirty, true);
  await panel.dispose(); env.bridge.dispose();
});

test("custom address remains editable and changing it clears a key typed for the previous endpoint", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.acceptResult(snapshot({ config: { provider: "relay", model: "kept-model", base_url: presetUrl } }));
  env.elements["api-key"].value = "synthetic-acw-key";
  env.elements["relay-preset"].value = "custom"; env.elements["relay-preset"].events.get("change")();
  assert.equal(env.elements["base-url"].readOnly, false);
  assert.equal(env.elements["base-url"].value, presetUrl);
  assert.equal(env.elements["api-key"].value, "synthetic-acw-key");
  env.elements["base-url"].value = "https://different.example/v1";
  env.elements["settings-form"].events.get("input")({ target: env.elements["base-url"] });
  assert.equal(env.elements["api-key"].value, "");
  assert.equal(panel.hasRelayKey(), false);
  assert.equal(env.sent.length, 0);
  await panel.dispose(); env.bridge.dispose();
});

test("a preset submits its URL and entered key only on explicit save and reads back after reload", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.elements.provider.value = "relay"; env.elements.provider.events.get("change")();
  env.elements["api-key"].value = "synthetic-acw-key";
  env.elements["service-tier"].value = "";
  env.respond(m => ({ structuredContent: m.params.name === "next_models" ? { provider: "relay", models: [{ id: "chosen-model" }] } :
    { config: { ...m.params.arguments.changes, api_key: undefined, api_key_present: true } } }));
  await panel.loadModels();
  assert.equal(env.sent.filter(m => m.params?.name === "next_configure").length, 0);
  assert.equal(env.sent.find(m => m.params?.name === "next_models").params.arguments.source.base_url, presetUrl);
  await panel.saveSettings();
  const changes = env.sent.find(m => m.params?.name === "next_configure").params.arguments.changes;
  assert.equal(changes.base_url, presetUrl);
  assert.equal(changes.api_key, "synthetic-acw-key");
  assert.equal(changes.model, "chosen-model");
  assert.equal(changes.reasoning_effort, "low");
  assert.equal(Object.hasOwn(changes, "relay_preset"), false);
  panel.fillSettings();
  assert.equal(env.elements["relay-preset"].value, "aicodewith");
  assert.equal(env.elements["api-key"].value, "");
  assert.equal(panel.hasRelayKey(), true);
  assert.equal(env.sent.filter(m => m.params?.name === "next_refresh" || m.method === "ui/message").length, 0);
  await panel.dispose(); env.bridge.dispose();
});

test("pending catalog results and failed saves do not discard preset edits", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.acceptResult(snapshot({ config: { provider: "relay", model: "kept-model", base_url: "https://custom.example/v1", api_key_present: true } }));
  env.respond(m => { if (m.params.name === "next_models") return undefined; if (m.params.name === "next_configure") throw new Error("保存失败"); return {}; });
  const pending = panel.loadModels();
  const request = env.sent.find(m => m.params?.name === "next_models");
  env.elements["relay-preset"].value = "aicodewith"; env.elements["relay-preset"].events.get("change")();
  env.deliver({ id: request.id, result: { structuredContent: { provider: "relay", models: [{ id: "old-service-model" }] } } });
  await pending;
  assert.equal(env.elements["model-list"].children.length, 0);
  env.elements["api-key"].value = "synthetic-new-key";
  await panel.saveSettings();
  assert.equal(panel.settingsDirty, true);
  assert.equal(env.elements["relay-preset"].value, "aicodewith");
  assert.equal(env.elements["base-url"].value, presetUrl);
  assert.equal(env.elements["api-key"].value, "synthetic-new-key");
  assert.equal(panel.config.base_url, "https://custom.example/v1");
  env.elements["discard-confirm"].events.get("click")();
  assert.equal(env.elements["relay-preset"].value, "custom");
  assert.equal(env.elements["base-url"].value, "https://custom.example/v1");
  await panel.dispose(); env.bridge.dispose();
});

test("bridge initializes before receiving entrypoint data and rejects foreign-window responses", async () => {
  const env = setup(); env.respond(() => undefined);
  const pending = env.bridge.request("ui/initialize", {});
  const request = env.sent.at(-1);
  env.deliver({ id: request.id, result: { forged: true } }, {});
  assert.equal(env.bridge.pending.size, 1);
  env.deliver({ id: request.id, result: { hostCapabilities: {} } });
  assert.deepEqual(plain(await pending), { hostCapabilities: {} });
  env.bridge.dispose();
});

test("draft requires advertised OpenAI capability and always sets send:false", async () => {
  assert.equal(supportsDraft({}), false);
  assert.equal(supportsDraft({ experimental: { "openai/message": null } }), false);
  assert.equal(supportsDraft({ experimental: { "openai/message": {} } }), true);
  assert.deepEqual(plain(draftParams("编辑我")), { role: "user", content: [{ type: "text", text: "编辑我" }], _meta: { "openai/message": { target: "active", send: false } } });
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  await panel.draft("编辑我");
  assert.equal(env.sent.filter((message) => message.method === "ui/message").length, 0);
  panel.hostCapabilities = { experimental: { "openai/message": {} } };
  await panel.draft("编辑我");
  assert.equal(env.sent.find((message) => message.method === "ui/message").params._meta["openai/message"].send, false);
  await panel.dispose(); env.bridge.dispose();
});

test("hidden panel closes lease, stops polling, then reopens against host-bound thread", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  env.respond((message) => ({ structuredContent: message.params.name === "next_panel" ? snapshot({ panel_id: "panel-b" }) : {} }));
  panel.initialized = true; panel.acceptResult(snapshot());
  assert.ok([...env.timers.values()].some((timer) => timer.delay === 2000));
  env.hide(); await settle();
  assert.equal(env.sent.filter((message) => message.params?.name === "next_close").length, 1);
  assert.equal([...env.timers.values()].some((timer) => timer.delay === 2000), false);
  env.run(2000); await settle();
  assert.equal(env.sent.filter((message) => message.params?.name === "next_poll").length, 0);
  env.show(); await settle();
  const open = env.sent.find((message) => message.params?.name === "next_panel");
  assert.deepEqual(plain(open.params.arguments), {});
  assert.equal(panel.snapshot.panel_id, "panel-b");
  await panel.dispose(); env.bridge.dispose();
});

test("poll only sends panel_id; stale cross-thread response cannot replace current state", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  env.respond((message) => ({ structuredContent: snapshot({ thread_id: "other-thread", panel_id: "other-panel" }) }));
  panel.initialized = true; panel.acceptResult(snapshot());
  await panel.poll();
  const poll = env.sent.find((message) => message.params?.name === "next_poll");
  assert.deepEqual(plain(poll.params.arguments), { panel_id: "panel-a" });
  assert.equal(panel.snapshot.thread_id, "thread-a");
  panel.acceptResult(snapshot({ panel_id: "panel-b", thread_id: "thread-b", title: "另一对话" }), true);
  assert.equal(panel.snapshot.thread_id, "thread-b");
  assert.equal(env.elements["thread-title"].textContent, "另一对话");
  await panel.dispose(); env.bridge.dispose();
});

test("teardown stops renewals and responds to host request with empty result", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.deliver({ id: 82, method: "ui/resource-teardown", params: { reason: "tab closed" } });
  await settle();
  assert.equal(panel.closed, true);
  assert.equal(env.bridge.closed, true);
  assert.deepEqual(plain(env.sent.find((message) => message.id === 82).result), {});
  assert.equal([...env.timers.values()].some((timer) => timer.delay === 2000), false);
});

test("tool notification drives binding; hostContext thread guesses are ignored", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  env.respond(() => ({ protocolVersion: "2026-01-26", hostCapabilities: {}, hostContext: { theme: "dark", threadId: "forged-thread" } }));
  await panel.start();
  assert.equal(env.sent[0].method, "ui/initialize");
  assert.equal(env.sent[1].method, "ui/notifications/initialized");
  assert.equal(panel.snapshot, null);
  assert.equal(env.doc.documentElement.dataset.theme, "dark");
  env.deliver({ method: "ui/notifications/tool-result", params: { structuredContent: snapshot() } });
  assert.equal(panel.snapshot.thread_id, "thread-a");
  await panel.dispose(); env.bridge.dispose();
});

test("model content is rendered as text and no suggestion auto-submits", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  const attack = '<img src=x onerror="alert(1)">';
  panel.acceptResult(snapshot({ suggestions: [{ title: attack, prompt: attack, reason: attack }] }));
  const card = env.elements.suggestions.children[0];
  assert.equal(card.children[0].children[1].textContent, attack);
  assert.equal(card.children[2].textContent, attack);
  assert.equal(env.sent.filter((message) => message.method === "ui/message").length, 0);
  assert.equal(source.includes("innerHTML"), false);
  await panel.dispose(); env.bridge.dispose();
});

test("an unbound entrypoint clears the previous thread and pending suggestions", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  panel.acceptResult({ status: "unbound", panel_id: null, config: {} }, true);
  assert.equal(panel.snapshot, null);
  assert.equal(env.elements.suggestions.hidden, true);
  assert.equal(env.elements.refresh.disabled, true);
  assert.equal(panel.currentState(), "unbound");
  await panel.dispose(); env.bridge.dispose();
});

test("business states never invent suggestions and unbound controls cannot generate", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.render();
  await panel.action("next_refresh");
  assert.equal(env.sent.length, 0);
  for (const status of ["unconfigured", "waiting", "generating", "empty", "paused", "error"]) {
    panel.acceptResult(snapshot({ status, revision: (panel.snapshot?.revision || 0) + 1 }));
    assert.equal(env.elements.suggestions.hidden, true, status);
    assert.ok(env.elements["status-title"].textContent.length > 0, status);
  }
  await panel.dispose(); env.bridge.dispose();
});

test("auto checkbox keeps the user's choice while the service request is pending", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.respond((message) => message.params?.name === "next_auto" ? undefined : { structuredContent: {} });
  env.elements["auto-generate"].checked = false;
  env.elements["auto-generate"].events.get("change")();
  assert.equal(env.elements["auto-generate"].checked, false);
  assert.equal(env.elements["auto-generate"].disabled, true);
  const call = env.sent.find((message) => message.params?.name === "next_auto");
  env.deliver({ id: call.id, result: { structuredContent: snapshot({ auto_generate: false, status: "paused", revision: 2 }) } });
  await settle();
  assert.equal(env.elements["auto-generate"].checked, false);
  assert.equal(env.elements["auto-generate"].disabled, false);
  await panel.dispose(); env.bridge.dispose();
});

test("settings accept a write-only API key and catalog remains optional", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.elements.provider.value = "relay"; env.elements.model.value = "relay-model";
  env.elements["base-url"].value = "https://relay.example/v1";
  env.elements["api-key"].value = "synthetic-ui-key";
  env.elements["reasoning-effort"].value = "low";
  env.respond((message) => ({ structuredContent: message.params.name === "next_models" ? { provider: "relay", models: [{ id: "relay-model", displayName: "可选模型" }] } : { config: { ...message.params.arguments.changes, api_key: undefined, api_key_present: true } } }));
  await panel.saveSettings();
  const saved = env.sent.find((message) => message.params?.name === "next_configure");
  assert.equal(saved.params.arguments.changes.api_key, "synthetic-ui-key");
  assert.equal(saved.params.arguments.changes.clear_api_key, false);
  assert.equal(env.elements["api-key"].value, "");
  await panel.loadModels();
  assert.equal(env.elements["model-list"].children[0].value, "relay-model");
  assert.deepEqual(plain(modelEntry("plain-id")), { id: "plain-id", label: "plain-id" });
  await panel.dispose(); env.bridge.dispose();
});

test("saved key stays blank in the form and is reused for the same endpoint", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.acceptResult(snapshot({ config: { provider: "relay", base_url: "https://relay.example/v1", model: "r", reasoning_effort: "low", api_key_present: true } }));
  assert.equal(env.elements["api-key"].value, "");
  assert.match(env.elements["api-key"].placeholder, /已保存/);
  assert.match(html, /id="api-key" type="password"/);
  assert.doesNotMatch(html, /api-key-env|relay-source|local-relay/);
  env.respond((m) => ({ structuredContent: m.params.name === "next_models" ? { provider: "relay", models: [{ id: "r" }] } : { config: panel.config } }));
  await panel.loadModels(); await panel.saveSettings();
  const directory = env.sent.find(m => m.params?.name === "next_models").params.arguments;
  assert.equal(Object.hasOwn(directory.source, "api_key"), false);
  const changes = env.sent.find(m => m.params?.name === "next_configure").params.arguments.changes;
  assert.equal(Object.hasOwn(changes, "api_key"), false);
  assert.equal(changes.clear_api_key, false);
  await panel.dispose(); env.bridge.dispose();
});

test("saved key cannot be sent to a different endpoint without entering a key", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.acceptResult(snapshot({ config: { provider: "relay", base_url: "https://relay.example/v1", model: "r", api_key_present: true } }));
  env.elements["base-url"].value = "https://different.example/v1";
  await panel.loadModels(); await panel.saveSettings();
  assert.equal(env.sent.filter(m => ["next_models", "next_configure"].includes(m.params?.name)).length, 0);
  assert.match(env.elements["settings-message"].textContent, /重新填写/);
  env.respond(() => ({})); await panel.dispose(); env.bridge.dispose();
});

test("explicit key clearing is saved without sending a key", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.acceptResult(snapshot({ config: { provider: "relay", base_url: "https://relay.example/v1", model: "r", api_key_present: true } }));
  env.elements["clear-api-key"].checked = true;
  env.respond(() => ({ structuredContent: { config: { ...panel.config, api_key_present: false } } }));
  await panel.saveSettings();
  const changes = env.sent.find(m => m.params?.name === "next_configure").params.arguments.changes;
  assert.equal(changes.clear_api_key, true);
  assert.equal(Object.hasOwn(changes, "api_key"), false);
  assert.equal(env.elements["api-key"].value, "");
  await panel.dispose(); env.bridge.dispose();
});

test("clearing the key invalidates a pending directory response", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.acceptResult(snapshot({ config: { provider: "relay", base_url: "https://relay.example/v1", model: "r", api_key_present: true } }));
  env.respond(() => undefined);
  const pending = panel.loadModels();
  const request = env.sent.find(m => m.params?.name === "next_models");
  env.elements["clear-api-key"].checked = true;
  env.elements["clear-api-key"].events.get("change")();
  env.deliver({ id: request.id, result: { structuredContent: { provider: "relay", models: [{ id: "stale" }] } } });
  await pending;
  assert.equal(env.elements["model-list"].children.length, 0);
  assert.equal(panel.hasRelayKey(), false);
  env.respond(() => ({})); await panel.dispose(); env.bridge.dispose();
});

test("unsaved relay without endpoint or key never shows the official catalog", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.respond(() => ({ structuredContent: { provider: "official", models: Array.from({ length: 7 }, (_, i) => ({ id: `official-${i}` })) } }));
  env.elements.provider.value = "relay"; env.elements.provider.events.get("change")();
  await panel.loadModels();
  assert.equal(env.sent.filter(m => m.params?.name === "next_models").length, 0);
  assert.equal(env.elements["model-list"].children.length, 0);
  assert.match(env.elements["models-status"].textContent, /API 地址.*API Key/);
  assert.equal(env.elements.model.value, "chosen-model");
  assert.equal(env.sent.filter(m => m.params?.name === "next_configure").length, 0);
  await panel.dispose(); env.bridge.dispose();
});

test("catalog uses the unsaved relay source without saving model settings", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.elements.provider.value = "relay";
  env.elements["base-url"].value = "https://relay.example/v1";
  env.elements["api-key"].value = "synthetic-ui-key";
  env.elements.provider.events.get("change")();
  env.respond(() => ({ structuredContent: { provider: "relay", models: [{ id: "relay-only" }] } }));
  await panel.loadModels();
  const request = env.sent.find(m => m.params?.name === "next_models");
  assert.deepEqual(plain(request.params.arguments), { source: { provider: "relay", base_url: "https://relay.example/v1", api_key: "synthetic-ui-key" } });
  assert.equal(env.elements["model-list"].children[0].value, "relay-only");
  assert.match(env.elements["models-status"].textContent, /中转.*1/);
  assert.equal(panel.config.provider, "official");
  assert.equal(env.sent.filter(m => m.params?.name === "next_configure").length, 0);
  await panel.dispose(); env.bridge.dispose();
});

test("switching source invalidates a pending catalog response", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.respond(() => undefined);
  const pending = panel.loadModels();
  const request = env.sent.find(m => m.params?.name === "next_models");
  env.elements.provider.value = "relay"; env.elements.provider.events.get("change")();
  env.deliver({ id: request.id, result: { structuredContent: { provider: "official", models: [{ id: "late-official" }] } } });
  await pending;
  assert.equal(env.elements["model-list"].children.length, 0);
  assert.match(env.elements["models-status"].textContent, /中转/);
  assert.equal(env.elements["load-models"].disabled, false);
  env.respond(() => ({}));
  await panel.dispose(); env.bridge.dispose();
});

test("catalog failure clears previous choices and retains manual model input", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.elements.provider.value = "relay";
  env.elements["base-url"].value = "https://relay.example/v1";
  env.elements["api-key"].value = "synthetic-ui-key";
  env.elements.provider.events.get("change")();
  env.elements.model.value = "manual-relay-model";
  env.elements["model-list"].append(new Element("option"));
  env.respond(() => { throw new Error("中转服务未提供模型目录，请手动输入模型 ID。"); });
  await panel.loadModels();
  assert.equal(env.elements["model-list"].children.length, 0);
  assert.equal(env.elements.model.value, "manual-relay-model");
  assert.match(env.elements["models-status"].textContent, /手动输入/);
  await panel.dispose(); env.bridge.dispose();
});

test("editing endpoint invalidates old responses and polling cannot enable duplicate reads", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.elements.provider.value = "relay";
  env.elements["base-url"].value = "https://first.example/v1";
  env.elements["api-key"].value = "synthetic-ui-key";
  env.elements.provider.events.get("change")();
  env.respond(() => undefined);
  const first = panel.loadModels();
  const firstRequest = env.sent.find(m => m.params?.name === "next_models");
  panel.render(); await panel.loadModels();
  assert.equal(env.sent.filter(m => m.params?.name === "next_models").length, 1);
  env.elements["base-url"].value = "https://second.example/v1";
  env.elements["settings-form"].events.get("input")();
  const second = panel.loadModels();
  const secondRequest = env.sent.filter(m => m.params?.name === "next_models").at(-1);
  env.deliver({ id: firstRequest.id, result: { structuredContent: { provider: "relay", models: [{ id: "old-relay" }] } } });
  await first;
  assert.equal(env.elements["model-list"].children.length, 0);
  assert.equal(env.elements["load-models"].disabled, true);
  env.deliver({ id: secondRequest.id, result: { structuredContent: { provider: "relay", models: [{ id: "new-relay" }] } } });
  await second;
  assert.equal(env.elements["model-list"].children[0].value, "new-relay");
  assert.equal(env.elements["load-models"].disabled, false);
  env.respond(() => ({}));
  await panel.dispose(); env.bridge.dispose();
});

test("a mismatched provider response is never displayed as a relay catalog", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true; panel.acceptResult(snapshot());
  env.elements.provider.value = "relay";
  env.elements["base-url"].value = "https://relay.example/v1";
  env.elements["api-key"].value = "synthetic-ui-key";
  env.elements.provider.events.get("change")();
  env.respond(() => ({ structuredContent: { provider: "official", models: [{ id: "wrong-catalog" }] } }));
  await panel.loadModels();
  assert.equal(env.elements["model-list"].children.length, 0);
  assert.match(env.elements["models-status"].textContent, /来源不匹配/);
  await panel.dispose(); env.bridge.dispose();
});

test("usage describes cumulative calls, remains visible for errors and empty results, and preserves unknowns", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.acceptResult(snapshot({ usage: { calls: 3, input_tokens: 2100, output_tokens: 240 }, revision: 1 }));
  assert.equal(env.elements.usage.hidden, false);
  assert.equal(env.elements.usage.textContent, "本聊天累计 3 次生成尝试 · 已知 2,340 tokens");
  panel.acceptResult(snapshot({ status: "error", usage: { calls: 4, failed_calls: 1 }, revision: 2 }));
  assert.equal(env.elements.usage.hidden, false);
  assert.equal(env.elements.usage.textContent, "本聊天累计 4 次生成尝试 · 用量尚未返回");
  assert.equal(env.elements.usage.textContent.includes("0 tokens"), false);
  panel.acceptResult(snapshot({ status: "empty", usage: { calls: 5, input_tokens: 2800, unknown_calls: 1 }, revision: 3 }));
  assert.equal(env.elements.usage.hidden, false);
  assert.equal(env.elements.usage.textContent, "本聊天累计 5 次生成尝试 · 已知 2,800 tokens · 部分用量未知");
  panel.acceptResult(snapshot({ usage: { calls: 0 }, revision: 4 }));
  assert.equal(env.elements.usage.hidden, true);
  await panel.dispose(); env.bridge.dispose();
});

test("skin persistence uses only the display tool and preserves cards, reasons, dirty settings and model", async () => {
  const env = setup(); const panel = new Panel(env.win, env.doc, env.bridge);
  panel.initialized = true;
  panel.hostCapabilities = { experimental: { "openai/message": {} } };
  panel.acceptResult(snapshot({ appearance: { skin: "graphite" } }));
  const card = env.elements.suggestions.children[0];
  const reason = card.children[1];
  card.children[0].children[2].events.get("click")();
  assert.equal(reason.hidden, false);
  env.respond(message => ({ structuredContent: message.params.name === "next_appearance" ?
    { appearance: { skin: message.params.arguments.skin } } : { config: panel.config } }));
  await panel.showSettings(true);
  env.elements.model.value = "unsaved-model";env.elements["settings-form"].events.get("input")();
  await panel.changeSkin("annotation");
  assert.equal(panel.skin, "annotation");assert.equal(env.elements.app.dataset.skin, "annotation");
  assert.equal(env.elements.suggestions.children[0], card);assert.equal(reason.hidden, false);
  assert.equal(panel.view, "settings");assert.equal(env.elements.model.value, "unsaved-model");
  assert.equal(panel.config.model, "chosen-model");
  assert.equal(env.sent.filter(m => ["next_refresh", "next_configure", "next_auto"].includes(m.params?.name)).length, 0);
  assert.equal(env.sent.filter(m => m.method === "ui/message").length, 0);
  const second = new Panel(env.win, env.doc, env.bridge);
  second.acceptResult(snapshot({ appearance: { skin: "annotation" } }));
  assert.equal(second.skin, "annotation");
  await second.dispose();await panel.dispose();env.bridge.dispose();
});

test("failed appearance write keeps the last skin and redacts the service failure", async () => {
  const env=setup();const panel=new Panel(env.win,env.doc,env.bridge);
  panel.initialized=true;panel.acceptResult(snapshot({appearance:{skin:"focus"}}));
  env.respond(()=>{throw new Error("private-raw-path");});
  await panel.changeSkin("annotation");
  assert.equal(panel.skin,"focus");assert.equal(env.elements.app.dataset.skin,"focus");
  assert.equal(env.elements.notice.textContent,"外观没有保存，请重试。");
  assert.equal(panel.skinBusy,false);
  await panel.dispose();env.bridge.dispose();
});

test("poll begun before a skin write cannot repaint the previous preference", async () => {
  const env=setup();const panel=new Panel(env.win,env.doc,env.bridge);
  panel.initialized=true;panel.acceptResult(snapshot({appearance:{skin:"graphite"}}));
  env.respond(m=>m.params.name==="next_poll"?undefined:{structuredContent:{appearance:{skin:"annotation"}}});
  const polling=panel.poll();await settle();
  const request=env.sent.find(m=>m.params?.name==="next_poll");
  await panel.changeSkin("annotation");
  env.deliver({id:request.id,result:{structuredContent:snapshot({revision:2,appearance:{skin:"graphite"}})}});
  await polling;
  assert.equal(panel.skin,"annotation");
  await panel.dispose();env.bridge.dispose();
});

test("failed model save and dirty navigation keep edits until explicit discard", async () => {
  const env=setup();const panel=new Panel(env.win,env.doc,env.bridge);
  panel.initialized=true;panel.acceptResult(snapshot());
  env.respond(m=>{if(m.params.name==="next_configure")throw new Error("设置没有保存");return {structuredContent:{config:panel.config}};});
  await panel.showSettings(true);
  env.elements.model.value="edited-model";env.elements["settings-form"].events.get("input")();
  await panel.saveSettings();assert.equal(panel.settingsDirty,true);
  assert.equal(env.elements.model.value,"edited-model");assert.equal(panel.view,"settings");
  panel.openView("usage");assert.equal(panel.view,"settings");assert.equal(env.elements["discard-dialog"].open,true);
  env.elements["discard-continue"].events.get("click")();assert.equal(env.elements.model.value,"edited-model");
  panel.openView("usage");env.elements["discard-confirm"].events.get("click")();
  assert.equal(panel.view,"usage");assert.equal(env.elements.model.value,"chosen-model");
  assert.equal(panel.settingsDirty,false);
  await panel.dispose();env.bridge.dispose();
});

test("real record metadata is shown without treating missing tokens as zero", async () => {
  const env=setup();const panel=new Panel(env.win,env.doc,env.bridge);panel.initialized=true;
  panel.acceptResult(snapshot({recent_jobs:[{ordinal:3,origin:"manual",state:"error",invoked_at:1000,duration_seconds:2,usage:{}}],usage:{calls:3,failed_calls:1,unknown_calls:1}}));
  const card=env.elements["usage-records"].children[0];
  assert.equal(card.children[0].children[0].textContent,"手动刷新");
  assert.equal(card.children[0].children[1].textContent,"未成功");
  assert.equal(card.children[2].children[0].children[0].textContent,"未知");
  panel.openView("usage");assert.equal(env.elements["usage-arrow"].hidden,true);
  assert.equal(env.elements["usage-open"].disabled,true);
  await panel.dispose();env.bridge.dispose();
});
