let configModel = null;
let selectedIndex = 0;
let activeView = "runtime";
let dirty = false;
let lastLogs = [];
let configPathText = "";
let workerCommand = [];

const qs = (selector) => document.querySelector(selector);

const MODE_LABELS = {
  observe: "观察",
  draft: "草稿",
  guarded: "门禁",
  autofix: "自动修复",
  send: "发送",
};
const REPLY_ENGINE_LABELS = {
  ai: "AI",
  codex: "Codex",
};
const CLASSIFIER_LABELS = {
  heuristic: "启发式",
  hybrid: "混合",
  ai: "AI",
};
const TRIGGER_LABELS = {
  mention: "仅 @ 时回复",
  group: "群消息都处理",
};
const CODE_WRITE_POLICY_LABELS = {
  none: "只读",
  approval: "群内审批",
  auto: "自动",
};
const CHANNEL_LABELS = {
  dingtalk: "钉钉",
};
const REPLY_TARGET_LABELS = {
  auto: "自动",
  sender: "发送人",
  conversation: "群会话",
};
const VIEWS = ["runtime", "groups", "safety", "logs"];

function viewFromHash() {
  const value = window.location.hash.replace("#", "");
  return VIEWS.includes(value) ? value : "runtime";
}

activeView = viewFromHash();

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    const detail = payload.error || payload.stderr || payload.stdout || `Request failed: ${response.status}`;
    throw new Error(String(detail).trim());
  }
  return payload;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function groups() {
  if (!configModel.groups) configModel.groups = [];
  return configModel.groups;
}

function defaults() {
  if (!configModel.defaults) configModel.defaults = {};
  return configModel.defaults;
}

function workerSettings() {
  if (!configModel.worker) configModel.worker = {};
  return configModel.worker;
}

function listToText(value) {
  if (Array.isArray(value)) return value.join(", ");
  return value || "";
}

function textToList(value) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function setDirty(value = true) {
  dirty = value;
  qs("#saveConfig").textContent = dirty ? "保存配置 *" : "保存配置";
}

function resolved(target, key, fallback = "") {
  const value = target[key];
  return value === undefined || value === null || value === "" ? fallback : value;
}

function renderRuntime(snapshot) {
  const runtime = snapshot.runtime || {};
  const stats = snapshot.stats || {};
  const running = runtime.running;
  qs("#metricRuntime").textContent = running ? `运行中 #${runtime.pid}` : "已停止";
  qs("#metricGroups").textContent = `${stats.enabled || 0}/${stats.groups || 0}`;
  qs("#metricSend").textContent = String(stats.send_enabled || 0);
  qs("#metricCodex").textContent = String(stats.codex_execute || 0);
  qs("#sideStatusText").textContent = running ? "监听运行中" : "监听已停止";
  qs("#sideStatusDot").classList.toggle("running", running);
  workerCommand = Array.isArray(snapshot.worker_command) ? snapshot.worker_command : workerCommand;
  renderCommandPreview();
}

function renderCommandPreview() {
  const preview = qs("#commandPreview");
  if (!preview) return;
  preview.textContent = workerCommand.length ? workerCommand.join(" ") : "保存或刷新后显示启动命令";
}

function renderGroupsTable() {
  const body = qs("#groupsBody");
  body.innerHTML = "";
  groups().forEach((group, index) => {
    const tr = document.createElement("tr");
    tr.className = index === selectedIndex ? "selected" : "";
    tr.addEventListener("click", () => {
      selectedIndex = index;
      render();
    });
    const groupId = group.group || "";
    const placeholder = groupId.includes("REPLACE");
    tr.innerHTML = `
      <td><input type="checkbox" data-field="enabled" ${group.enabled !== false ? "checked" : ""}></td>
      <td>
        <select data-field="channel">
          ${selectOptions(CHANNEL_LABELS, group.channel || defaults().channel || "dingtalk")}
        </select>
      </td>
      <td><input type="text" data-field="name" value="${escapeAttr(group.name || "")}"></td>
      <td><input type="text" data-field="project" value="${escapeAttr(group.project || "")}"></td>
      <td>
        <select data-field="mode">
          ${modeOptions(group.mode || defaults().mode || "draft")}
        </select>
      </td>
      <td>
        <select data-field="trigger">
          ${selectOptions(TRIGGER_LABELS, group.trigger || defaults().trigger || "group")}
        </select>
      </td>
      <td>
        <select data-field="reply_engine">
          ${selectOptions(REPLY_ENGINE_LABELS, group.reply_engine || defaults().reply_engine || "ai")}
        </select>
      </td>
      <td>
        <select data-field="code_write_policy">
          ${selectOptions(CODE_WRITE_POLICY_LABELS, group.code_write_policy || defaults().code_write_policy || "none")}
        </select>
      </td>
      <td><input type="checkbox" data-field="send" ${group.send ? "checked" : ""}></td>
      <td><input class="${placeholder ? "placeholder" : ""}" type="text" data-field="group" value="${escapeAttr(groupId)}"></td>
      <td class="row-actions"><button class="text-button" data-action="delete" type="button">删除</button></td>
    `;
    bindObjectControls(tr, group, { rerenderTable: false });
    tr.querySelector('[data-action="delete"]').addEventListener("click", (event) => {
      event.stopPropagation();
      groups().splice(index, 1);
      selectedIndex = Math.max(0, Math.min(selectedIndex, groups().length - 1));
      setDirty();
      render();
    });
    body.appendChild(tr);
  });
}

function modeOptions(current) {
  return ["observe", "draft", "guarded", "autofix", "send"]
    .map((mode) => `<option value="${mode}" ${mode === current ? "selected" : ""}>${MODE_LABELS[mode]}</option>`)
    .join("");
}

function renderDefaultsForm() {
  const form = qs("#defaultsForm");
  if (!form) return;
  const target = defaults();
  const history = historyContext(target);
  form.innerHTML = `
    ${formSection("基础")}
    ${selectField("默认渠道", "channel", CHANNEL_LABELS, target.channel || "dingtalk")}
    ${selectField("默认模式", "mode", MODE_LABELS, target.mode || "draft")}
    ${selectField("默认触发", "trigger", TRIGGER_LABELS, target.trigger || "mention")}
    ${selectField("默认回复目标", "reply_target", REPLY_TARGET_LABELS, target.reply_target || "auto")}
    ${field("默认 DWS Profile", "profile", target.profile || "")}
    ${field("默认项目", "project", target.project || "")}
    ${field("默认仓库路径", "repo", target.repo || "")}
    ${field("默认就绪消息", "ready_message", target.ready_message || "")}
    ${textareaField("默认监听关键词", "query", target.query || "")}
    ${textareaField("默认事件过滤 JSON", "filter_json", target.filter_json || "")}

    ${formSection("回复与分类")}
    ${selectField("默认回复引擎", "reply_engine", REPLY_ENGINE_LABELS, target.reply_engine || "codex")}
    ${selectField("默认分类器", "classifier", CLASSIFIER_LABELS, target.classifier || "heuristic")}
    ${checkboxField("默认允许发送", "send", target.send === true)}
    ${listField("默认代码关键词", "code_keywords", target.code_keywords || [])}
    ${listField("默认测试命令", "test_commands", target.test_commands || [])}

    ${formSection("Codex 与审批")}
    ${checkboxField("默认允许 Codex 执行", "codex_execute", target.codex_execute === true)}
    ${selectField("默认写代码权限", "code_write_policy", CODE_WRITE_POLICY_LABELS, target.code_write_policy || "none")}
    ${listField("默认审批确认词", "approval_phrases", target.approval_phrases || ["OK", "好的", "继续", "确认", "可以"])}
    ${numberField("默认审批有效期（分钟）", "approval_ttl_minutes", target.approval_ttl_minutes || 1440, 1)}

    ${formSection("历史背景")}
    ${historyFields(history)}
  `;
  bindObjectControls(form, target, { historyTarget: target, rerenderTable: true });
}

function renderWorkerForm() {
  const form = qs("#workerForm");
  if (!form) return;
  const target = workerSettings();
  form.innerHTML = `
    ${formSection("DWS")}
    ${field("DWS 命令路径", "dws_bin", resolved(target, "dws_bin", "dws"))}
    ${field("去重状态文件", "state_file", resolved(target, "state_file", "~/.dws/ai-autoreply/seen.jsonl"))}
    ${numberField("DWS 调用超时（秒）", "dws_timeout", resolved(target, "dws_timeout", 60), 1)}
    ${numberField("监听就绪等待（秒）", "ready_timeout", resolved(target, "ready_timeout", 60), 1)}
    ${field("单次监听时长", "duration", target.duration || "")}
    ${numberField("最多事件数", "max_events", resolved(target, "max_events", 0), 0)}
    ${checkboxField("允许任意发送目标", "allow_any_target", target.allow_any_target === true)}

    ${formSection("Codex")}
    ${field("Codex 命令路径", "codex_bin", resolved(target, "codex_bin", "codex"))}
    ${field("Codex 模型", "codex_model", target.codex_model || "")}
    ${field("Codex 任务目录", "codex_log_dir", resolved(target, "codex_log_dir", "~/.dws/ai-autoreply/codex-tasks"))}
    ${field("Codex Session 目录", "codex_session_dir", resolved(target, "codex_session_dir", "~/.dws/ai-autoreply/codex-sessions"))}
    ${numberField("Codex 超时（秒）", "codex_timeout", resolved(target, "codex_timeout", 3600), 1)}
    ${numberField("自动修复置信度", "autofix_min_confidence", resolved(target, "autofix_min_confidence", 0.85), 0, "0.01")}

    ${formSection("AI 回复")}
    ${field("AI Base URL", "base_url", target.base_url || "")}
    ${field("AI 模型", "model", target.model || "")}
    ${numberField("温度", "temperature", resolved(target, "temperature", 0.2), 0, "0.1")}
    ${numberField("最大 tokens", "max_tokens", resolved(target, "max_tokens", 256), 1)}
    ${numberField("最长回复字符", "max_reply_chars", resolved(target, "max_reply_chars", 500), 0)}
    ${numberField("AI 超时（秒）", "ai_timeout", resolved(target, "ai_timeout", 60), 1)}
    ${checkboxField("AI 失败时使用分类草稿", "reply_fallback", target.reply_fallback !== false)}
    ${textareaField("自定义系统提示词", "system_prompt", target.system_prompt || "")}
  `;
  bindObjectControls(form, target, { rerenderTable: false });
}

function renderInspector() {
  const form = qs("#inspectorForm");
  const selected = groups()[selectedIndex];
  if (!selected) {
    qs("#selectedGroupName").textContent = "未选择群";
    form.innerHTML = `<p class="notice">新建一个群后再编辑群详情。</p>`;
    return;
  }
  qs("#selectedGroupName").textContent = selected.name || selected.group || "未命名群";
  const inherited = defaults();
  const history = historyContext(selected);
  form.innerHTML = `
    ${formSection("基础路由")}
    ${checkboxField("启用", "enabled", selected.enabled !== false)}
    ${selectField("渠道", "channel", CHANNEL_LABELS, selected.channel || inherited.channel || "dingtalk")}
    ${field("群名称", "name", selected.name || "")}
    ${field("群 ID", "group", selected.group || "")}
    ${field("项目", "project", selected.project || inherited.project || "")}
    ${field("仓库路径", "repo", selected.repo || inherited.repo || "")}
    ${field("DWS Profile", "profile", selected.profile || inherited.profile || "")}
    ${selectField("模式", "mode", MODE_LABELS, selected.mode || inherited.mode || "draft")}
    ${selectField("触发方式", "trigger", TRIGGER_LABELS, selected.trigger || inherited.trigger || "group")}
    ${selectField("回复目标", "reply_target", REPLY_TARGET_LABELS, selected.reply_target || inherited.reply_target || "auto")}

    ${formSection("监听与回复")}
    ${selectField("回复引擎", "reply_engine", REPLY_ENGINE_LABELS, selected.reply_engine || inherited.reply_engine || "ai")}
    ${selectField("分类器", "classifier", CLASSIFIER_LABELS, selected.classifier || inherited.classifier || "heuristic")}
    ${checkboxField("允许发送", "send", selected.send === true)}
    ${field("就绪消息", "ready_message", selected.ready_message || inherited.ready_message || "")}
    ${textareaField("监听关键词", "query", selected.query || inherited.query || "")}
    ${textareaField("事件过滤 JSON", "filter_json", selected.filter_json || inherited.filter_json || "")}

    ${formSection("Codex 与代码")}
    ${checkboxField("允许 Codex 执行", "codex_execute", selected.codex_execute === true)}
    ${selectField("写代码权限", "code_write_policy", CODE_WRITE_POLICY_LABELS, selected.code_write_policy || inherited.code_write_policy || "none")}
    ${listField("代码关键词", "code_keywords", selected.code_keywords || inherited.code_keywords || [])}
    ${listField("测试命令", "test_commands", selected.test_commands || inherited.test_commands || [])}
    ${listField("审批确认词", "approval_phrases", selected.approval_phrases || inherited.approval_phrases || ["OK", "好的", "继续", "确认", "可以"])}
    ${numberField("审批有效期（分钟）", "approval_ttl_minutes", selected.approval_ttl_minutes || inherited.approval_ttl_minutes || 1440, 1)}

    ${formSection("历史背景")}
    ${historyFields(history)}
  `;
  bindObjectControls(form, selected, { historyTarget: selected, rerenderTable: true });
}

function bindObjectControls(root, target, options = {}) {
  root.querySelectorAll("input, select, textarea, button").forEach((control) => {
    control.addEventListener("click", (event) => event.stopPropagation());
  });
  root.querySelectorAll("[data-field]").forEach((control) => {
    control.addEventListener("change", () => {
      updateScalar(target, control);
      setDirty();
      if (options.rerenderTable) renderGroupsTable();
    });
    control.addEventListener("input", () => {
      if (control.tagName !== "SELECT" && control.type !== "checkbox") {
        updateScalar(target, control);
        setDirty();
      }
    });
  });
  root.querySelectorAll("[data-list-field]").forEach((control) => {
    control.addEventListener("input", () => {
      target[control.dataset.listField] = textToList(control.value);
      setDirty();
    });
  });
  root.querySelectorAll("[data-number-field]").forEach((control) => {
    control.addEventListener("input", () => {
      target[control.dataset.numberField] = numberFromControl(control);
      setDirty();
    });
  });
  root.querySelectorAll("[data-history-field]").forEach((control) => {
    control.addEventListener("change", () => {
      const historyTarget = options.historyTarget || target;
      historyTarget.history_context = historyContext(historyTarget);
      historyTarget.history_context[control.dataset.historyField] = control.checked;
      setDirty();
    });
  });
  root.querySelectorAll("[data-history-number-field]").forEach((control) => {
    control.addEventListener("input", () => {
      const historyTarget = options.historyTarget || target;
      historyTarget.history_context = historyContext(historyTarget);
      historyTarget.history_context[control.dataset.historyNumberField] = numberFromControl(control);
      setDirty();
    });
  });
}

function updateScalar(target, control) {
  target[control.dataset.field] = control.type === "checkbox" ? control.checked : control.value;
}

function numberFromControl(control) {
  const numberValue = Number.parseFloat(control.value);
  const min = Number.parseFloat(control.getAttribute("min") || "0");
  if (!Number.isFinite(numberValue)) return Number.isFinite(min) ? min : 0;
  const normalized = control.getAttribute("step") === "1" ? Math.trunc(numberValue) : numberValue;
  return Math.max(Number.isFinite(min) ? min : 0, normalized);
}

function field(label, key, value) {
  return `<label>${label}<input type="text" data-field="${key}" value="${escapeAttr(value)}"></label>`;
}

function textareaField(label, key, value) {
  return `<label class="full-row">${label}<textarea data-field="${key}">${escapeText(value)}</textarea></label>`;
}

function listField(label, key, value) {
  return `<label class="full-row">${label}<textarea data-list-field="${key}">${escapeText(listToText(value))}</textarea></label>`;
}

function numberField(label, key, value, min = 0, step = "1") {
  return `<label>${label}<input type="number" min="${escapeAttr(min)}" step="${escapeAttr(step)}" data-number-field="${key}" value="${escapeAttr(value)}"></label>`;
}

function checkboxField(label, key, checked) {
  return `
    <label class="field checkbox-row">
      <span class="field-label">${label}</span>
      <input type="checkbox" data-field="${key}" ${checked ? "checked" : ""}>
    </label>
  `;
}

function selectField(label, key, labels, current) {
  return `
    <label>${label}
      <select data-field="${key}">
        ${selectOptions(labels, current)}
      </select>
    </label>
  `;
}

function formSection(title) {
  return `<h3 class="form-section-title">${title}</h3>`;
}

function historyFields(history) {
  return `
    <label class="field checkbox-row">
      <span class="field-label">读取最近聊天背景</span>
      <input type="checkbox" data-history-field="enabled" ${history.enabled ? "checked" : ""}>
    </label>
    ${numberField("历史消息条数", "limit", history.limit || 0, 0)}
    ${numberField("历史时间窗口（分钟）", "minutes", history.minutes || 0, 0)}
  `
    .replaceAll("data-number-field=\"limit\"", "data-history-number-field=\"limit\"")
    .replaceAll("data-number-field=\"minutes\"", "data-history-number-field=\"minutes\"");
}

function selectOptions(labels, current) {
  return Object.entries(labels)
    .map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`)
    .join("");
}

function historyContext(target) {
  const value = target.history_context;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return {
      enabled: Boolean(value.enabled),
      limit: Number.parseInt(value.limit || 0, 10) || 0,
      minutes: Number.parseInt(value.minutes || 0, 10) || 0,
    };
  }
  return { enabled: Boolean(value), limit: 20, minutes: 0 };
}

function renderLogs(logs = lastLogs) {
  lastLogs = logs || [];
  const filter = qs("#logFilter").value;
  const visible = filter ? lastLogs.filter((item) => item.source === filter) : lastLogs;
  qs("#logStream").textContent = visible
    .slice(-180)
    .map((item) => `[${item.time}] ${item.source.padEnd(12)} ${item.line}`)
    .join("\n");
  const stream = qs("#logStream");
  stream.scrollTop = stream.scrollHeight;
}

function setActiveView(view, options = {}) {
  activeView = VIEWS.includes(view) ? view : "runtime";
  document.querySelectorAll("[data-view-section]").forEach((section) => {
    section.hidden = section.dataset.viewSection !== activeView;
  });
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === activeView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  if (options.pushHash !== false && window.location.hash !== `#${activeView}`) {
    window.history.replaceState(null, "", `#${activeView}`);
  }
}

function render(snapshot) {
  if (snapshot) {
    configModel = clone(snapshot.config);
    configPathText = snapshot.config_path;
    workerCommand = Array.isArray(snapshot.worker_command) ? snapshot.worker_command : [];
    qs("#configPath").textContent = configPathText;
    renderRuntime(snapshot);
    renderLogs(snapshot.logs);
  }
  if (!configModel) return;
  selectedIndex = Math.max(0, Math.min(selectedIndex, groups().length - 1));
  renderDefaultsForm();
  renderWorkerForm();
  renderGroupsTable();
  renderInspector();
  renderCommandPreview();
  setActiveView(activeView);
}

function escapeAttr(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function escapeText(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

async function loadState() {
  const snapshot = await requestJson("/api/state");
  render(snapshot);
  setDirty(false);
}

async function refreshRuntime() {
  try {
    const snapshot = await requestJson("/api/state");
    renderRuntime(snapshot);
    renderLogs(snapshot.logs);
    if (!dirty) {
      configModel = clone(snapshot.config);
      render();
    }
  } catch (error) {
    renderLogs([{ time: "--:--:--", source: "ui", line: error.message }]);
  }
}

async function saveConfig(markClean = true) {
  const payload = await requestJson("/api/config", {
    method: "POST",
    body: JSON.stringify({ config: configModel }),
  });
  if (markClean) setDirty(false);
  render(payload);
}

async function runCheck() {
  if (dirty) await saveConfig(false);
  try {
    await requestJson("/api/config-check", { method: "POST", body: "{}" });
  } finally {
    await refreshRuntime();
    setDirty(false);
  }
}

async function startWorker() {
  if (dirty) await saveConfig(false);
  try {
    await requestJson("/api/start", { method: "POST", body: "{}" });
  } finally {
    await refreshRuntime();
    setDirty(false);
  }
}

async function stopWorker() {
  await requestJson("/api/stop", { method: "POST", body: "{}" });
  await refreshRuntime();
}

function addGroup() {
  groups().push({
    enabled: true,
    channel: defaults().channel || "dingtalk",
    name: "新群组",
    group: "cid_REPLACE_GROUP",
    project: defaults().project || "",
    repo: defaults().repo || "",
    profile: defaults().profile || "",
    mode: defaults().mode || "draft",
    trigger: defaults().trigger || "mention",
    reply_target: defaults().reply_target || "auto",
    reply_engine: defaults().reply_engine || "codex",
    classifier: defaults().classifier || "heuristic",
    query: defaults().query || "",
    filter_json: defaults().filter_json || "",
    ready_message: defaults().ready_message || "",
    history_context: clone(defaults().history_context || { enabled: true, limit: 20 }),
    send: defaults().send === true,
    codex_execute: defaults().codex_execute === true,
    code_write_policy: defaults().code_write_policy || "none",
    code_keywords: clone(defaults().code_keywords || []),
    test_commands: clone(defaults().test_commands || []),
    approval_phrases: clone(defaults().approval_phrases || ["OK", "好的", "继续", "确认", "可以"]),
    approval_ttl_minutes: defaults().approval_ttl_minutes || 1440,
  });
  selectedIndex = groups().length - 1;
  setDirty();
  setActiveView("groups");
  render();
}

function bindActions() {
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => setActiveView(button.dataset.view));
  });
  qs("#saveConfig").addEventListener("click", () => saveConfig().catch(showError));
  qs("#runCheck").addEventListener("click", () => runCheck().catch(showError));
  qs("#startWorker").addEventListener("click", () => startWorker().catch(showError));
  qs("#stopWorker").addEventListener("click", () => stopWorker().catch(showError));
  qs("#addGroup").addEventListener("click", addGroup);
  qs("#refreshLogs").addEventListener("click", () => refreshRuntime().catch(showError));
  qs("#logFilter").addEventListener("change", () => renderLogs());
}

function showError(error) {
  const now = new Date().toTimeString().slice(0, 8);
  lastLogs.push({ time: now, source: "ui", line: error.message });
  renderLogs(lastLogs);
}

bindActions();
window.addEventListener("hashchange", () => setActiveView(viewFromHash(), { pushHash: false }));
loadState().catch(showError);
setInterval(refreshRuntime, 2000);
