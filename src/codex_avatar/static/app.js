let configModel = null;
let selectedIndex = 0;
let activeView = "runtime";
let dirty = false;
let lastLogs = [];

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

function updateGroup(index, field, value) {
  groups()[index][field] = value;
  setDirty();
  render();
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
      <td><input type="text" data-field="name" value="${escapeAttr(group.name || "")}"></td>
      <td><input type="text" data-field="project" value="${escapeAttr(group.project || "")}"></td>
      <td>
        <select data-field="mode">
          ${modeOptions(group.mode || defaults().mode || "draft")}
        </select>
      </td>
      <td><input type="checkbox" data-field="send" ${group.send ? "checked" : ""}></td>
      <td><input class="${placeholder ? "placeholder" : ""}" type="text" data-field="group" value="${escapeAttr(groupId)}"></td>
      <td class="row-actions"><button class="text-button" data-action="delete" type="button">删除</button></td>
    `;
    tr.querySelectorAll("input, select").forEach((control) => {
      control.addEventListener("click", (event) => event.stopPropagation());
      control.addEventListener("change", () => {
        const field = control.dataset.field;
        const value = control.type === "checkbox" ? control.checked : control.value;
        updateGroup(index, field, value);
      });
      control.addEventListener("input", () => {
        if (control.type === "text") {
          groups()[index][control.dataset.field] = control.value;
          setDirty();
        }
      });
    });
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

function renderInspector() {
  const form = qs("#inspectorForm");
  const selected = groups()[selectedIndex];
  if (!selected) {
    qs("#selectedGroupName").textContent = "未选择群";
    form.innerHTML = `<p class="notice">新建一个群后再编辑运行门禁。</p>`;
    return;
  }
  qs("#selectedGroupName").textContent = selected.name || selected.group || "未命名群";
  form.innerHTML = `
    ${field("就绪消息", "ready_message", selected.ready_message || "")}
    ${field("仓库路径", "repo", selected.repo || "")}
    ${field("监听关键词", "query", selected.query || defaults().query || "")}
    <label>回复引擎
      <select data-field="reply_engine">
        ${selectOptions(REPLY_ENGINE_LABELS, selected.reply_engine || defaults().reply_engine || "ai")}
      </select>
    </label>
    <label>分类器
      <select data-field="classifier">
        ${selectOptions(CLASSIFIER_LABELS, selected.classifier || defaults().classifier || "heuristic")}
      </select>
    </label>
    <label>代码关键词
      <textarea data-list-field="code_keywords">${escapeText(listToText(selected.code_keywords || []))}</textarea>
    </label>
    <label>测试命令
      <textarea data-list-field="test_commands">${escapeText(listToText(selected.test_commands || []))}</textarea>
    </label>
    <label class="field"><span class="field-label">允许 Codex 执行</span>
      <input type="checkbox" data-field="codex_execute" ${selected.codex_execute ? "checked" : ""}>
    </label>
  `;
  form.querySelectorAll("[data-field]").forEach((control) => {
    control.addEventListener("change", () => {
      const fieldName = control.dataset.field;
      selected[fieldName] = control.type === "checkbox" ? control.checked : control.value;
      setDirty();
      renderGroupsTable();
    });
    control.addEventListener("input", () => {
      if (control.tagName !== "SELECT" && control.type !== "checkbox") {
        selected[control.dataset.field] = control.value;
        setDirty();
      }
    });
  });
  form.querySelectorAll("[data-list-field]").forEach((control) => {
    control.addEventListener("input", () => {
      selected[control.dataset.listField] = textToList(control.value);
      setDirty();
    });
  });
}

function field(label, key, value) {
  return `<label>${label}<input type="text" data-field="${key}" value="${escapeAttr(value)}"></label>`;
}

function selectOptions(labels, current) {
  return Object.entries(labels)
    .map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`)
    .join("");
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

function setActiveView(view) {
  const valid = ["runtime", "groups", "safety", "logs"];
  activeView = valid.includes(view) ? view : "runtime";
  document.querySelectorAll("[data-view-section]").forEach((section) => {
    section.hidden = section.dataset.viewSection !== activeView;
  });
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === activeView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
}

function render(snapshot) {
  if (snapshot) {
    configModel = clone(snapshot.config);
    qs("#configPath").textContent = snapshot.config_path;
    renderRuntime(snapshot);
    renderLogs(snapshot.logs);
  }
  if (!configModel) return;
  selectedIndex = Math.max(0, Math.min(selectedIndex, groups().length - 1));
  renderGroupsTable();
  renderInspector();
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
  renderRuntime(payload);
  renderLogs(payload.logs);
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
    name: "新群组",
    group: "cid_REPLACE_GROUP",
    project: "",
    repo: "",
    mode: "draft",
    reply_engine: defaults().reply_engine || "codex",
    query: defaults().query || "",
    send: false,
    codex_execute: false,
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
loadState().catch(showError);
setInterval(refreshRuntime, 2000);
