const inboxPanel = $("#p-inbox");
const inboxRepo = $("#inbox-project");
const inboxRefresh = $("#inbox-refresh");
let inboxData;
let inboxPoll;
let inboxLoading = false;
let inboxAutoRefresh = false;

const draftKinds = { review: "PR review", triage: "Issue triage", scope: "Scope", design: "Design", implement: "Implementation" };
const runStates = { failed: "Failed", partial: "Incomplete", missed: "Missed" };
const projectUrl = (repo, suffix) => "/p/" + repo.split("/").map(encodeURIComponent).join("/") + "/" + suffix;

function inboxTime(value) {
  if (!value || Number.isNaN(Date.parse(value))) return "";
  return new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function inboxLink(url, label) {
  return `<a class="inbox-link small" href="${esc(url)}">${esc(label)}</a>`;
}

function githubItem(item) {
  return item.url?.startsWith("https://github.com/") ? ghLink(item.number, item.url) : " #" + item.number;
}

function inboxRow(item, status, detail, links, error = false) {
  const date = inboxTime(item.at);
  return `<article class="inbox-row">
    <div>
      <div class="small muted">${esc(item.repo)}${date ? " · " + esc(date) : ""}</div>
      <h3>${item.number ? githubItem(item) + " " : ""}${esc(item.title)}</h3>
      <div><span class="pill ${error ? "warn" : ""}">${esc(status)}</span></div>
      ${detail ? `<p class="small muted">${esc(detail)}</p>` : ""}
    </div>
    <div class="inbox-actions">${links}</div>
  </article>`;
}

function renderInbox() {
  if (!inboxData) return;
  const { projects, refreshing } = inboxData;
  const selected = inboxRepo.value || store.get("triage-inbox-repo") || "";
  const repos = ["", ...projects.map((p) => p.repo)];
  if (repos.join("\n") !== Array.from(inboxRepo.options, (option) => option.value).join("\n")) {
    inboxRepo.replaceChildren(...repos.map((r) => new Option(r || "All projects", r)));
  }
  inboxRepo.value = repos.includes(selected) ? selected : "";
  const visible = (item) => !inboxRepo.value || inboxRepo.value === item.repo;
  const drafts = inboxData.drafts.filter(visible), decisions = inboxData.decisions.filter(visible), runs = inboxData.runs.filter(visible);
  const total = inboxData.drafts.length + inboxData.decisions.length + inboxData.runs.length;
  setBadge("#b-inbox", total, total ? "warn" : "");
  setBadge("#inbox-draft-count", drafts.length, drafts.length ? "info" : "");
  setBadge("#inbox-decision-count", decisions.length, decisions.length ? "warn" : "");
  setBadge("#inbox-run-count", runs.length, runs.length ? "warn" : "");
  inboxRefresh.disabled = Boolean(refreshing.length);
  $("#inbox-status").textContent = refreshing.length
    ? "Refreshing " + refreshing.length + (refreshing.length === 1 ? " repository…" : " repositories…")
    : projects.length ? projects.length + (projects.length === 1 ? " repository" : " repositories") : "No repositories saved";
  $("#inbox-coverage").innerHTML = projects.filter(visible).filter((p) => p.stale || p.error).map((p) => `
    <div><b>${esc(p.repo)}</b> · ${esc(p.snapshotAt ? "Last issue snapshot " + inboxTime(p.snapshotAt) : "No issue snapshot yet")}
      ${p.error ? " · " + esc(p.error) : ""}</div>`).join("");

  $("#inbox-drafts").innerHTML = drafts.map((item) => inboxRow(
    item, [draftKinds[item.type] || "Draft", item.agent === "codex" ? "Codex" : item.agent === "claude" ? "Claude" : ""].filter(Boolean).join(" · "),
    item.detail, inboxLink(projectUrl(item.repo, "?draft=" + item.id + "#work:runs"), "Open draft")
  )).join("") || '<div class="empty">No drafts waiting for review.</div>';

  $("#inbox-decisions").innerHTML = decisions.map((item) => inboxRow(
    item, [item.priority, item.status || "Decision needed"].filter(Boolean).join(" · "),
    item.assignees.length ? "Assigned to " + item.assignees.join(", ") : "Unassigned",
    inboxLink(projectUrl(item.repo, "?issue=" + item.number + "#decisions"), "Open decision")
  )).join("") || '<div class="empty">No decisions in the available issue snapshots.</div>';

  $("#inbox-runs").innerHTML = runs.map((item) => inboxRow(
    { ...item, title: item.lane === "issues" ? "Issue triage" : "PR review" }, runStates[item.status], item.detail,
    inboxLink(projectUrl(item.repo, item.id ? "?run=" + item.id + "#projects" : "#projects"),
              item.id ? "Open run" : "Open schedule"), true
  )).join("") || '<div class="empty">No failed or missed runs in the last 7 days.</div>';
}

async function updateInbox() {
  clearTimeout(inboxPoll);
  if (inboxPanel.hidden || inboxLoading) return;
  inboxLoading = true;
  try {
    inboxData = await registry("/api/inbox");
    renderInbox();
    if (!inboxAutoRefresh && !inboxData.refreshing.length && inboxData.projects.some((p) => p.stale)) {
      inboxAutoRefresh = true;
      const { refreshing } = await registry("/api/inbox/refresh", {});
      inboxData.refreshing = refreshing;
      renderInbox();
    }
  } catch (error) {
    $("#inbox-status").textContent = "Could not load inbox: " + error.message;
  } finally {
    inboxLoading = false;
    inboxPoll = setTimeout(updateInbox, inboxData?.refreshing.length ? 2000 : 15000);
  }
}

window.openInbox = updateInbox;
inboxRepo.onchange = () => {
  store.set("triage-inbox-repo", inboxRepo.value);
  renderInbox();
};
inboxRefresh.onclick = async () => {
  inboxAutoRefresh = true;
  inboxRefresh.disabled = true;
  $("#inbox-status").textContent = "Starting refresh…";
  try {
    const result = await registry("/api/inbox/refresh", {});
    if (inboxData) inboxData.refreshing = result.refreshing;
    updateInbox();
  } catch (error) {
    inboxRefresh.disabled = false;
    $("#inbox-status").textContent = error.message;
  }
};

if (!inboxPanel.hidden) updateInbox();
