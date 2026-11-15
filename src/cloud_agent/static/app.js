"use strict";
const $ = (id) => document.getElementById(id);
const statusLabels = {
  queued: "排队中",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已停止",
  reaping: "回收中",
};
let actor = null,
  registering = false,
  selected = null,
  source = null,
  tasks = [],
  refreshTimer = null;
const terminal = (s) => ["succeeded", "failed", "cancelled"].includes(s);
const number = (n) => new Intl.NumberFormat("zh-CN").format(n || 0);
const time = (n) =>
  new Date(n * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
async function api(path, options = {}) {
  const requestActor = actor;
  const headers = {
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...(requestActor ? { "X-CSRF-Token": requestActor.csrf_token } : {}),
    ...options.headers,
  };
  const response = await fetch("/api" + path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && actor === requestActor && actor) showAuth();
    throw new Error(
      typeof data.detail === "string" ? data.detail : "请求失败，请检查输入",
    );
  }
  return data;
}
function showAuth() {
  actor = null;
  selected = null;
  tasks = [];
  if (source) source.close();
  source = null;
  clearInterval(refreshTimer);
  $("auth-view").hidden = false;
  $("app-view").hidden = true;
  $("task-detail").hidden = true;
  $("empty-detail").hidden = false;
  for (const id of [
    "task-list",
    "event-log",
    "diff-panel",
    "detail-prompt",
    "detail-meta",
    "summary",
    "global-error",
  ])
    $(id).replaceChildren();
  $("download-patch").removeAttribute("href");
  $("prompt").value = "";
  $("repo").value = "";
}
async function showApp(value) {
  actor = value;
  $("auth-view").hidden = true;
  $("app-view").hidden = false;
  $("user-email").textContent = actor.email;
  $("user-initial").textContent = actor.email[0].toUpperCase();
  await refresh();
  if (actor !== value) return;
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => refresh().catch(showError), 6000);
}
function showError(error) {
  $("global-error").textContent = error.message;
}
$("auth-toggle").onclick = () => {
  registering = !registering;
  $("register-fields").hidden = !registering;
  $("auth-title").textContent = registering ? "创建你的工作空间" : "欢迎回来";
  $("auth-subtitle").textContent = registering
    ? "一个账号，一个独立的云端编码空间。"
    : "登录你的云端编码工作空间。";
  $("auth-submit").textContent = registering
    ? "创建工作空间 →"
    : "登录工作空间 →";
  $("auth-switch-label").textContent = registering
    ? "已有账号？"
    : "还没有账号？";
  $("auth-toggle").textContent = registering ? "登录" : "创建工作空间";
  $("password").autocomplete = registering
    ? "new-password"
    : "current-password";
  $("auth-error").textContent = "";
};
$("auth-form").onsubmit = async (event) => {
  event.preventDefault();
  $("auth-submit").disabled = true;
  $("auth-error").textContent = "";
  try {
    const value = await api(registering ? "/auth/register" : "/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: $("email").value,
        password: $("password").value,
        workspace: $("workspace").value,
        signup_code: $("signup-code").value,
      }),
    });
    $("password").value = "";
    $("signup-code").value = "";
    await showApp(value);
  } catch (error) {
    $("auth-error").textContent = error.message;
  } finally {
    $("auth-submit").disabled = false;
  }
};
$("logout").onclick = async () => {
  try {
    await api("/auth/logout", { method: "POST", body: "{}" });
    showAuth();
  } catch (error) {
    showError(error);
  }
};
$("refresh").onclick = () => refresh().catch(showError);
$("tasks-nav").onclick = () => $("prompt").focus();
async function refresh() {
  const currentActor = actor;
  if (!currentActor) return;
  const [list, usage] = await Promise.all([api("/tasks"), api("/usage")]);
  if (actor !== currentActor) return;
  tasks = list.tasks;
  $("workspace-name").textContent = usage.workspace;
  $("task-count").textContent = tasks.length;
  $("activity-count").textContent = tasks.length;
  const pct = Math.round(
    ((usage.monthly_tokens + usage.reserved_tokens) /
      usage.monthly_token_limit) *
      100,
  );
  $("quota-percent").textContent = pct + "%";
  $("quota-progress").value = pct;
  $("quota-description").textContent =
    number(usage.monthly_tokens) +
    " 已用 · " +
    number(usage.reserved_tokens) +
    " 预留";
  $("daily-description").textContent =
    "今日任务 " +
    usage.daily_tasks +
    " / " +
    usage.daily_task_limit +
    " · 月额度 " +
    number(usage.monthly_token_limit);
  $("active-description").textContent =
    "正在执行 " + usage.active_tasks + " / " + usage.concurrent_task_limit;
  $("task-budget").textContent =
    "每任务 " + number(usage.task_token_budget) + " Token 上限";
  renderTasks();
  if (selected) {
    const detail = await api("/tasks/" + selected);
    renderDetail(detail);
  }
}
function badge(status) {
  const element = document.createElement("span");
  element.className = "status " + status;
  element.textContent = statusLabels[status] || status;
  return element;
}
function renderTasks() {
  const list = $("task-list");
  list.replaceChildren();
  $("empty-list").hidden = tasks.length > 0;
  for (const task of tasks) {
    const row = document.createElement("button");
    row.className = "task-row" + (task.id === selected ? " selected" : "");
    const top = document.createElement("div");
    top.className = "task-row-header";
    top.append(badge(task.status));
    const stamp = document.createElement("span");
    stamp.className = "task-time";
    stamp.textContent = time(task.created_at);
    top.append(stamp);
    const title = document.createElement("div");
    title.className = "task-row-title";
    title.textContent = task.prompt;
    const repo = document.createElement("div");
    repo.className = "task-row-repo";
    repo.textContent =
      "⑂ " +
      (task.repo_url.replace("https://github.com/", "") || "全新工作空间");
    row.append(top, title, repo);
    row.onclick = () => selectTask(task.id).catch(showError);
    list.append(row);
  }
}
$("task-form").onsubmit = async (event) => {
  event.preventDefault();
  $("global-error").textContent = "";
  $("create-task").disabled = true;
  try {
    const task = await api("/tasks", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        prompt: $("prompt").value,
        repo_url: $("repo").value.trim(),
      }),
    });
    $("prompt").value = "";
    await refresh();
    await selectTask(task.id);
  } catch (error) {
    showError(error);
  } finally {
    $("create-task").disabled = false;
  }
};
function renderDetail(task) {
  if (task.id !== selected) return;
  $("empty-detail").hidden = true;
  $("task-detail").hidden = false;
  $("detail-status").className = "status " + task.status;
  $("detail-status").textContent = statusLabels[task.status] || task.status;
  $("detail-id").textContent = "#" + task.id.slice(0, 10);
  $("detail-prompt").textContent = task.prompt;
  $("detail-meta").textContent =
    time(task.created_at) +
    " · " +
    number(task.used_tokens) +
    " Token · " +
    (task.repo_url.replace("https://github.com/", "") || "全新工作空间");
  $("cancel-task").hidden = terminal(task.status) || task.status === "reaping";
  $("cancel-task").disabled = task.cancel_requested;
  $("cancel-task").textContent = task.cancel_requested
    ? "正在停止…"
    : "停止任务";
  $("summary").hidden = !task.summary && !task.error;
  $("summary").textContent = task.error || task.summary;
  $("download-patch").hidden = !terminal(task.status) || !task.patch;
  $("download-patch").href = "/api/tasks/" + task.id + "/patch";
  $("download-patch").download = task.id + ".patch";
  renderDiff(task.patch);
}
function renderDiff(patch) {
  const panel = $("diff-panel");
  panel.replaceChildren();
  $("diff-count").textContent = patch
    ? "· " + patch.split("\n").filter((x) => x.startsWith("diff --git")).length
    : "";
  if (!patch) {
    panel.textContent = "尚无代码变更。任务完成后，补丁将在这里显示。";
    return;
  }
  for (const line of patch.split("\n")) {
    const span = document.createElement("span");
    span.className = line.startsWith("+")
      ? "diff-added"
      : line.startsWith("-")
        ? "diff-removed"
        : line.startsWith("@@") || line.startsWith("diff ")
          ? "diff-header"
          : "";
    span.textContent = line + "\n";
    panel.append(span);
  }
}
function addEvent(event) {
  const log = $("event-log"),
    nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 70;
  const row = document.createElement("div");
  row.className = "event-entry " + event.kind;
  const label = document.createElement("span");
  label.className = "event-kind";
  label.textContent = time(event.created_at) + " / " + event.kind.toUpperCase();
  row.append(label);
  let body;
  if (event.kind === "text_delta") body = event.data.text;
  else if (event.kind === "tool_call")
    body = event.data.name + " " + JSON.stringify(event.data.arguments);
  else if (event.kind === "tool_result")
    body = "exit " + event.data.exit_code + "\n" + event.data.output;
  else if (event.kind === "usage") body = number(event.data.tokens) + " Token";
  else body = event.data.message || JSON.stringify(event.data);
  row.append(document.createTextNode(body || ""));
  log.append(row);
  if (log.children.length > 700) log.firstChild.remove();
  if (nearBottom) log.scrollTop = log.scrollHeight;
}
async function selectTask(id) {
  if (source) source.close();
  selected = id;
  $("event-log").replaceChildren();
  renderTasks();
  const task = await api("/tasks/" + id);
  if (selected !== id) return;
  renderDetail(task);
  const connection = new EventSource("/api/tasks/" + id + "/events");
  source = connection;
  connection.onmessage = (event) => {
    if (selected !== id) return;
    try {
      addEvent(JSON.parse(event.data));
    } catch {}
  };
  connection.addEventListener("complete", () => {
    connection.close();
    if (selected === id) refresh().catch(showError);
  });
  connection.onerror = () => {
    if (selected === id)
      $("active-description").textContent = "连接中断，正在重连…";
  };
}
$("cancel-task").onclick = async () => {
  if (!selected) return;
  $("cancel-task").disabled = true;
  try {
    await api("/tasks/" + selected + "/cancel", { method: "POST", body: "{}" });
    await refresh();
  } catch (error) {
    showError(error);
    $("cancel-task").disabled = false;
  }
};
function tab(diff) {
  $("log-panel").hidden = diff;
  $("diff-panel").hidden = !diff;
  $("log-tab").classList.toggle("active", !diff);
  $("diff-tab").classList.toggle("active", diff);
  $("log-tab").setAttribute("aria-selected", String(!diff));
  $("diff-tab").setAttribute("aria-selected", String(diff));
}
$("log-tab").onclick = () => tab(false);
$("diff-tab").onclick = () => tab(true);
api("/auth/me").then(showApp).catch(showAuth);
