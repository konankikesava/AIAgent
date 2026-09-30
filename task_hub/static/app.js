"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let state = null;
let view = "overview";
let selectedRun = null;
let currentRun = null;
let refreshing = false;
let token = sessionStorage.getItem("work-agents-token") || "";
const statusNames = {
  queued: "Queued",
  running: "Working",
  waiting_approval: "Needs review",
  succeeded: "Complete",
  failed: "Failed",
  cancelled: "Cancelled",
};
const viewCopy = {
  overview: [
    "Overview",
    "Your work, moving forward.",
    "Give your agents a task. Follow the progress. Pick up the result.",
  ],
  runs: [
    "Task runs",
    "A clear view of the work.",
    "Open a run to see its result, actions, and execution history.",
  ],
  items: [
    "Follow-ups",
    "The next steps that matter.",
    "A shared checklist for the actions you and your agents identify.",
  ],
  schedules: [
    "Schedules",
    "Make room for a routine.",
    "Recurring tasks run automatically while the service is online.",
  ],
};

async function api(path, body, raw = false) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let info;
    try {
      info = await response.json();
    } catch {
      info = { error: `Service returned HTTP ${response.status}` };
    }
    if (response.status === 401 && !$("access-dialog").open)
      $("access-dialog").showModal();
    throw new Error(info.error || "Request failed");
  }
  return raw ? response : response.json();
}

function notify(message) {
  $("notice").textContent = message;
  $("notice").classList.remove("hidden");
}

function dateLabel(value, timezone) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    ...(timezone ? { timeZone: timezone } : {}),
  }).format(new Date(value));
}

function agent(id) {
  return state.agents.find((a) => a.id === id);
}
function badge(status) {
  return `<span class="status-badge status-${escapeHtml(status)}">${escapeHtml(statusNames[status] || status)}</span>`;
}
function empty(symbol, title, description, action = "") {
  return `<div class="empty-state"><span class="empty-symbol" aria-hidden="true">${symbol}</span><h3>${title}</h3><p>${description}</p>${action}</div>`;
}

function render() {
  if (!state) return;
  const [crumb, title, description] = viewCopy[view];
  $("breadcrumb-current").textContent = crumb;
  $("page-title").textContent = title;
  $("page-description").textContent = description;
  document
    .querySelectorAll("[data-view]")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.view === view),
    );
  const cfg = state.config;
  $("mode-badge").textContent =
    cfg.provider === "demo" ? "OFFLINE DEMO" : cfg.provider.toUpperCase();
  $("demo-banner").classList.toggle("hidden", cfg.provider !== "demo");
  $("connection-label").innerHTML =
    `${cfg.provider === "demo" ? "Demo service online" : "Task service online"}<small>${escapeHtml(cfg.model)}</small>`;
  $("nav-run-count").textContent = state.runs.filter((r) =>
    ["queued", "running", "waiting_approval"].includes(r.status),
  ).length;
  const stats = [
    [
      "Tasks in motion",
      state.runs.filter((r) => ["queued", "running"].includes(r.status)).length,
      "Queued or working",
      "↗",
    ],
    [
      "Completed runs",
      state.runs.filter((r) => r.status === "succeeded").length,
      "From recent task history",
      "✓",
    ],
    [
      "Needs your review",
      state.approvals.length,
      "Actions waiting for approval",
      "◈",
    ],
    [
      "Active schedules",
      state.schedules.filter((s) => s.enabled).length,
      "Daily and weekly routines",
      "◷",
    ],
  ];
  $("stats").innerHTML = stats
    .map(
      (s) =>
        `<div class="stat"><span class="stat-icon" aria-hidden="true">${s[3]}</span><div class="stat-label">${s[0]}</div><div class="stat-number">${s[1]}</div><div class="stat-foot">${s[2]}</div></div>`,
    )
    .join("");
  $("stats").classList.toggle("hidden", view === "items");
  $("agent-section").classList.toggle("hidden", view !== "overview");
  $("agent-nav").innerHTML = state.agents
    .map(
      (a) =>
        `<button data-agent="${a.id}"><span class="mini-agent">${a.short}</span>${a.name}</button>`,
    )
    .join("");
  $("agent-cards").innerHTML = state.agents
    .map(
      (a) =>
        `<button class="agent-card" data-agent="${a.id}"><span class="agent-icon">${a.short}</span><h3>${a.name}</h3><p>${a.description}</p><span class="card-footer"><span><span class="ready-dot"></span>Ready to delegate</span><span class="card-arrow">↗</span></span></button>`,
    )
    .join("");
  $("approval-section").classList.toggle(
    "hidden",
    !state.approvals.length || view === "items",
  );
  $("approvals").innerHTML = state.approvals.map(approvalCard).join("");
  $("overview-grid").classList.toggle("hidden", view === "items");
  $("overview-grid").classList.toggle(
    "single-column",
    view === "runs" || view === "schedules",
  );
  $("runs-section").classList.toggle("hidden", view === "schedules");
  $("schedule-section").classList.toggle("hidden", view === "runs");
  $("items-section").classList.toggle("hidden", view !== "items");
  $("all-runs").classList.toggle("hidden", view === "runs");
  $("runs-heading").textContent =
    view === "runs"
      ? "Task history · most recent 100 runs"
      : "Recent task runs";
  const runs = view === "runs" ? state.runs : state.runs.slice(0, 6);
  $("run-list").innerHTML = runs.length
    ? runs
        .map(
          (r) =>
            `<button class="run-row" data-run="${r.id}"><span class="mini-agent">${agent(r.agent_id)?.short || "AI"}</span><span class="run-info"><span class="run-title">${escapeHtml(r.title)}</span><span class="run-subtitle">${escapeHtml(agent(r.agent_id)?.name || r.agent_id)} · ${escapeHtml(dateLabel(r.created_at))}</span></span>${badge(r.status)}<span class="card-arrow">›</span></button>`,
        )
        .join("")
    : empty(
        "↗",
        "Start with one useful task",
        "A meeting debrief, a weekly update, or a clearer plan for the day.",
        '<button class="text-button" data-compose="true">Give an agent a task <span>↗</span></button>',
      );
  $("schedule-list").innerHTML = state.schedules.length
    ? state.schedules
        .map((s) => {
          const spec = s.spec;
          const timezone = spec.timezone || "Asia/Kolkata";
          const cadence =
            spec.cadence === "weekly"
              ? `Every ${["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][spec.weekday || 0]}`
              : "Every day";
          return `<div class="schedule-row"><span class="schedule-clock">${escapeHtml(spec.time || "08:00")}<small>${spec.cadence === "weekly" ? "WEEKLY" : "DAILY"}</small></span><span class="run-info"><span class="run-title">${escapeHtml(s.label)}</span><span class="run-subtitle">${cadence} · ${escapeHtml(timezone)}<br>${s.enabled ? "Next: " + escapeHtml(dateLabel(s.next_at, timezone)) : "Schedule paused"}</span></span><button class="schedule-switch ${s.enabled ? "" : "paused"}" data-schedule="${s.id}" data-enable="${!s.enabled}" aria-label="${s.enabled ? "Pause" : "Resume"} ${escapeHtml(s.label)}">${s.enabled ? "Pause" : "Resume"}</button></div>`;
        })
        .join("")
    : empty(
        "◷",
        "Let the routine run itself",
        "Set a time for your next daily brief or weekly status update.",
      );
  $("item-list").innerHTML = state.work_items.length
    ? state.work_items
        .map(
          (item) =>
            `<div class="item-row ${item.status === "done" ? "done" : ""}"><input type="checkbox" id="item-${item.id}" data-item="${item.id}" ${item.status === "done" ? "checked" : ""}><label for="item-${item.id}">${escapeHtml(item.title)}${item.detail ? `<small>${escapeHtml(item.detail)}</small>` : ""}</label></div>`,
        )
        .join("")
    : empty(
        "✓",
        "Nothing waiting on you yet",
        "Add a next step here, or ask an agent to identify the follow-ups in your notes.",
      );
  $("last-updated").textContent =
    "Updated " +
    new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function approvalCard(approval) {
  const args = approval.arguments;
  return `<div class="approval-card"><h3>${escapeHtml(approval.run_title || "Proposed GitHub issue")}</h3><p>Create one GitHub issue in ${escapeHtml(args.repo)}. Approving sends the exact content below; the service adds an audit marker.</p><pre class="proposal">${escapeHtml(args.title)}\n\n${escapeHtml(args.body)}</pre><div class="approval-buttons"><button class="primary-button" data-approval="${approval.id}" data-approved="true">Approve & create issue</button><button class="secondary-button" data-approval="${approval.id}" data-approved="false">Reject action</button></div></div>`;
}

function setView(next) {
  view = next;
  render();
}

function compose(agentId = "chief-of-staff", scheduled = false) {
  if (!state) {
    notify("The service is still connecting.");
    return;
  }
  $("task-form").reset();
  $("task-agent").innerHTML = state.agents
    .map((a) => `<option value="${a.id}">${a.name}</option>`)
    .join("");
  $("task-agent").value = agentId;
  $("task-repo").innerHTML =
    '<option value="">No repository</option>' +
    state.config.github_repos
      .map((r) => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`)
      .join("");
  $("task-schedule").checked = scheduled;
  $("schedule-timezone").value = "Asia/Kolkata";
  $("composer-error").textContent = "";
  composerMode();
  $("composer").showModal();
  $("task-title").focus();
}

function composerMode() {
  const scheduled = $("task-schedule").checked;
  $("schedule-fields").classList.toggle("hidden", !scheduled);
  $("weekday-field").classList.toggle(
    "hidden",
    $("schedule-cadence").value !== "weekly",
  );
  $("task-submit").innerHTML = scheduled
    ? "Create schedule <span>◷</span>"
    : "Run task <span>↗</span>";
  $("composer-title").textContent = scheduled
    ? "Make a task a routine"
    : "Give your agent a task";
  $("composer-footnote").textContent = scheduled
    ? "First run is at the next scheduled time."
    : "Results are saved in your task history.";
}

async function openRun(id) {
  selectedRun = id;
  await refreshRun();
  if (!$("run-dialog").open) $("run-dialog").showModal();
}

async function refreshRun() {
  if (!selectedRun) return;
  const run = await api("/api/runs/" + selectedRun);
  currentRun = run;
  $("result-agent").textContent = agent(run.agent_id)?.name || run.agent_id;
  $("result-title").textContent = run.title;
  $("result-meta").innerHTML =
    badge(run.status) + `<span>${escapeHtml(dateLabel(run.created_at))}</span>`;
  $("result-error").textContent = run.error;
  $("result-content").textContent = run.result;
  $("result-content").classList.toggle("hidden", !run.result);
  $("result-empty").classList.toggle("hidden", Boolean(run.result));
  $("result-empty").textContent =
    run.status === "waiting_approval"
      ? "This task is paused until you review the proposed action."
      : run.status === "running"
        ? "Your agent is working. Progress updates appear below."
        : run.status === "queued"
          ? "This task is in the worker queue. It will start automatically."
          : "This run has no completed report. See the execution history for details.";
  $("result-approvals").innerHTML =
    run.status === "waiting_approval"
      ? run.approvals
          .filter((a) => a.status === "pending")
          .map(approvalCard)
          .join("")
      : "";
  $("result-events").innerHTML = run.events
    .map(
      (event) =>
        `<li><time>${escapeHtml(new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }))}</time><span>${escapeHtml(event.message)}</span></li>`,
    )
    .join("");
  $("cancel-run").classList.toggle(
    "hidden",
    !["queued", "running", "waiting_approval"].includes(run.status),
  );
  $("retry-run").classList.toggle(
    "hidden",
    !["failed", "cancelled"].includes(run.status) ||
      run.approvals.some((a) => ["executing", "uncertain"].includes(a.status)),
  );
  $("download-result").classList.toggle("hidden", run.status !== "succeeded");
  $("result-input-note").textContent = run.schedule_id
    ? "Started by a saved schedule."
    : "Started from your task request.";
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/api/state");
    render();
    if ($("run-dialog").open) await refreshRun();
  } catch (error) {
    $("connection-label").innerHTML =
      "Service unavailable<small>Check the service or access token</small>";
    notify(error.message);
  } finally {
    refreshing = false;
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.close) $(button.dataset.close).close();
    if (button.dataset.view) setView(button.dataset.view);
    if (button.dataset.agent) compose(button.dataset.agent);
    if (button.dataset.compose) compose();
    if (button.dataset.run) await openRun(button.dataset.run);
    if (button.dataset.approval) {
      button.disabled = true;
      await api("/api/approvals/" + button.dataset.approval, {
        approved: button.dataset.approved === "true",
      });
      notify(
        button.dataset.approved === "true"
          ? "Proposal approved. The worker will execute the exact action."
          : "Proposal rejected. The agent will continue without that action.",
      );
      await refresh();
    }
    if (button.dataset.schedule) {
      await api("/api/schedules/" + button.dataset.schedule, {
        enabled: button.dataset.enable === "true",
      });
      await refresh();
    }
  } catch (error) {
    notify(error.message);
  } finally {
    button.disabled = false;
  }
});

$("new-task").addEventListener("click", () => compose());
$("add-schedule").addEventListener("click", () =>
  compose("weekly-status", true),
);
$("all-runs").addEventListener("click", () => setView("runs"));
$("access-button").addEventListener("click", () =>
  $("access-dialog").showModal(),
);
$("task-schedule").addEventListener("change", composerMode);
$("schedule-cadence").addEventListener("change", composerMode);
$("use-example").addEventListener("click", () => {
  const a = agent($("task-agent").value);
  $("task-title").value = a.name + " task";
  $("task-instruction").value = a.example;
});
$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("task-submit").disabled = true;
  $("composer-error").textContent = "";
  const task = {
    agent_id: $("task-agent").value,
    title: $("task-title").value,
    instruction: $("task-instruction").value,
    context: { repo: $("task-repo").value, notes: $("task-notes").value },
  };
  try {
    if ($("task-schedule").checked) {
      await api("/api/schedules", {
        label: task.title,
        task,
        spec: {
          cadence: $("schedule-cadence").value,
          time: $("schedule-time").value,
          timezone: $("schedule-timezone").value,
          weekday: Number($("schedule-weekday").value),
        },
      });
      $("composer").close();
      setView("schedules");
      notify(
        "Schedule saved. The first task will run at the next scheduled time.",
      );
      await refresh();
    } else {
      const run = await api("/api/runs", task);
      $("composer").close();
      notify("Task queued. Your agent will pick it up automatically.");
      await refresh();
      await openRun(run.id);
    }
  } catch (error) {
    $("composer-error").textContent = error.message;
  } finally {
    $("task-submit").disabled = false;
  }
});
$("item-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/work-items", { title: $("item-title").value });
    $("item-title").value = "";
    await refresh();
  } catch (error) {
    notify(error.message);
  }
});
$("item-list").addEventListener("change", async (event) => {
  if (!event.target.dataset.item) return;
  try {
    await api("/api/work-items/" + event.target.dataset.item, {
      status: event.target.checked ? "done" : "open",
    });
    await refresh();
  } catch (error) {
    notify(error.message);
    await refresh();
  }
});
$("access-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  token = $("access-token").value.trim();
  sessionStorage.setItem("work-agents-token", token);
  $("access-token").value = "";
  $("access-dialog").close();
  await refresh();
});
$("cancel-run").addEventListener("click", async () => {
  try {
    await api("/api/runs/" + selectedRun + "/cancel", {});
    await refresh();
  } catch (error) {
    notify(error.message);
  }
});
$("retry-run").addEventListener("click", async () => {
  try {
    const run = await api("/api/runs/" + selectedRun + "/retry", {});
    await refresh();
    await openRun(run.id);
  } catch (error) {
    notify(error.message);
  }
});
$("download-result").addEventListener("click", async () => {
  try {
    const response = await api(
      "/api/runs/" + selectedRun + "/result",
      undefined,
      true,
    );
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "task-" + selectedRun + ".md";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    notify(error.message);
  }
});
$("run-dialog").addEventListener("close", () => {
  selectedRun = null;
  currentRun = null;
});
refresh();
setInterval(() => {
  if (document.visibilityState === "visible") refresh();
}, 2500);
