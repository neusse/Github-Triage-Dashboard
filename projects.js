// Project selection and setup use the registry; schedules and drafts use the selected worker.
const currentProject = PROJECT_ROUTE.slice(3);
const projectForm = $("#project-form");
const projectWorkflow = createWorkflowEditor(document);
const splitList = (value) => value.split(",").map((item) => item.trim()).filter(Boolean);
let editingProject = null;
let adding = !currentProject;
let panelLoaded = false;
let schedulePoll;
let scheduleBusy = false;
const linkedRun = pageParams.get("run");
let linkedRunShown = false;

async function registry(path, body) {
  const options = body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Triage": "1" }, body: JSON.stringify(body)
  };
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

async function loadProjectSwitcher() {
  const { projects } = await registry("/api/projects");
  const select = $("#project-switch");
  select.replaceChildren();
  for (const project of projects) {
    select.add(new Option(project.repo, project.repo, false, project.repo.toLowerCase() === currentProject.toLowerCase()));
  }
  if (!projects.length) select.add(new Option("No projects saved", ""));
  select.add(new Option("Add repository…", "+"));
  select.onchange = () => {
    if (select.value === "+") { showTab("projects"); beginNewProject(); select.value = currentProject; }
    else if (select.value) location.assign("/p/" + select.value + "/");
  };
}

function fillProject(profile) {
  const { repo, agent, base_branch, maintainers, schedule, ...advanced } = profile || {};
  const previousProject = editingProject;
  editingProject = repo || null;
  $("#project-name").value = repo || "";
  $("#project-name").readOnly = Boolean(repo);
  $("#project-agent").value = agent || "codex";
  $("#project-branch").value = base_branch || "";
  $("#project-maintainers").value = (maintainers || []).join(", ");
  $("#project-advanced").value = JSON.stringify(projectWorkflow.load(advanced), null, 2);
  if (previousProject !== editingProject) projectWorkflow.setLabelChoices([]);
  $("#schedule-enabled").checked = schedule?.enabled || false;
  $("#schedule-issues").value = (schedule?.issues_times || ["08:00", "16:00"]).join(", ");
  $("#schedule-prs").value = (schedule?.prs_times || ["08:30", "16:30"]).join(", ");
  $("#schedule-timezone").value = schedule?.timezone || "local";
  $("#schedule-max-issues").value = schedule?.max_issues ?? 5;
  $("#schedule-max-prs").value = schedule?.max_prs ?? 1;
  $("#project-label-check").disabled = !repo;
  $("#project-label-create").hidden = true;
  $("#project-label-info").textContent = repo ? "" : "Save the repository before checking labels.";
  $("#project-runs").hidden = !repo;
  $("#project-message").textContent = "";
  $("#project-add").textContent = adding && currentProject ? "Back to this project" : "Add repository";
}

function beginNewProject() {
  adding = true;
  panelLoaded = true;
  clearTimeout(schedulePoll);
  fillProject(null);
  $("#project-name").focus();
}

window.loadProjectsPanel = async function () {
  if (panelLoaded || adding) {
    if (!panelLoaded) { fillProject(null); panelLoaded = true; }
    if (!adding) loadSchedule();
    return;
  }
  try {
    const { profile } = await registry("/api/project-config?repo=" + encodeURIComponent(currentProject));
    if (adding) return;
    fillProject(profile);
    panelLoaded = true;
    loadSchedule();
  } catch (error) { $("#project-message").textContent = error.message; }
};

$("#project-add").onclick = () => {
  if (adding && currentProject) {
    adding = panelLoaded = false;
    window.loadProjectsPanel();
  } else beginNewProject();
};

projectForm.onsubmit = async (event) => {
  event.preventDefault();
  const save = $("#project-save"), message = $("#project-message");
  save.disabled = true;
  message.textContent = "Saving…";
  try {
    const advanced = JSON.parse($("#project-advanced").value || "{}");
    if (!advanced || typeof advanced !== "object" || Array.isArray(advanced)) throw new Error("Advanced settings must be a JSON object.");
    const { profile } = await registry("/api/project-config", {
      ...projectWorkflow.collect(advanced), repo: $("#project-name").value.trim(), agent: $("#project-agent").value,
      base_branch: $("#project-branch").value.trim() || null,
      maintainers: splitList($("#project-maintainers").value),
      schedule: {
        enabled: $("#schedule-enabled").checked,
        timezone: $("#schedule-timezone").value.trim() || "local",
        issues_times: splitList($("#schedule-issues").value),
        prs_times: splitList($("#schedule-prs").value),
        max_issues: Number($("#schedule-max-issues").value),
        max_prs: Number($("#schedule-max-prs").value)
      }
    });
    if (adding || profile.repo.toLowerCase() !== currentProject.toLowerCase()) {
      location.assign("/p/" + profile.repo + "/#projects");
      return;
    }
    fillProject(profile);
    message.textContent = "Saved.";
    loadSchedule();
    load().then(loadJobs);
    loadProjectSwitcher().catch(() => {});
  } catch (error) { message.textContent = error.message; }
  finally { save.disabled = false; }
};

async function checkMattLabels(create = false) {
  const repo = editingProject, check = $("#project-label-check"), add = $("#project-label-create");
  if (!repo) return;
  check.disabled = add.disabled = true;
  $("#project-label-info").textContent = create ? "Creating missing labels…" : "Checking GitHub…";
  try {
    const result = create ? await registry("/api/project-labels", { repo })
      : await registry("/api/project-labels?repo=" + encodeURIComponent(repo));
    if (editingProject !== repo) return;
    projectWorkflow.setLabelChoices(result.labels || []);
    add.hidden = !result.missing.length;
    const summary = result.missing.length ? "Missing: " + result.missing.join(", ")
      : result.created.length ? "Created: " + result.created.join(", ") : "All five labels are present.";
    $("#project-label-info").textContent = summary;
  } catch (error) { if (editingProject === repo) $("#project-label-info").textContent = error.message; }
  finally { if (editingProject === repo) check.disabled = add.disabled = false; }
}

$("#project-label-check").onclick = () => checkMattLabels();
$("#project-label-create").onclick = () => {
  if (editingProject && confirm("Create the missing standard triage labels in " + editingProject + "?")) checkMattLabels(true);
};

const displaySlot = (value) => value ? new Date(value).toLocaleString([], {
  month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
}) : "—";

async function loadSchedule() {
  clearTimeout(schedulePoll);
  if (!PROJECT_ROUTE || adding || $("#p-projects").hidden) return;
  try {
    const query = linkedRun ? "?include=" + encodeURIComponent(linkedRun) : "";
    const response = await projectFetch("/api/schedule" + query, { cache: "no-store" });
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || response.statusText);
    if (adding) return;
    const next = status.next;
    $("#schedule-next").textContent = status.enabled
      ? "Next issue pass: " + displaySlot(next.issues) + " · Next PR pass: " + displaySlot(next.prs)
      : "Automatic runs are paused. You can still draft on demand.";
    $("#schedule-run-issues").disabled = $("#schedule-run-prs").disabled = status.busy || scheduleBusy;
    $("#schedule-history").innerHTML = status.runs.length ? status.runs.map((run) => `
      <article class="schedule-run${run.id === linkedRun ? " inbox-target" : ""}">
        <b>${run.lane === "issues" ? "Issue triage" : "PR review"}</b>
        <span class="small muted"> · ${esc(displaySlot(run.at))} · ${esc(run.trigger)} · ${esc(run.state)}</span>
        ${run.error ? `<div class="small">${esc(run.error)}</div>` : ""}
        ${run.items.map((item) => `<div class="small">${ghLink(item.number, item.url)} ${esc(item.title)} · ${esc(item.detail || item.status)}
          ${item.status === "done" ? `<button class="small" data-scheduled-draft="${esc(item.id)}">Open draft</button>` : ""}</div>`).join("")}
        ${run.deferred ? `<div class="small muted">${run.deferred} eligible items deferred by the run limit.</div>` : ""}
        ${run.landing?.length ? `<details><summary class="small">Suggested manual landing order</summary>
          <ol class="small">${run.landing.map((pr) => `<li>${ghLink(pr.number, pr.url)} ${esc(pr.title)} <code>${esc(pr.head.slice(0, 9))}</code> · ${esc(pr.blocker || pr.verdict)}</li>`).join("")}</ol>
        </details>` : ""}
      </article>`).join("") : '<p class="muted small">No scheduled triage runs yet.</p>';
    const selected = $("#schedule-history .inbox-target");
    if (selected && !linkedRunShown) {
      linkedRunShown = true;
      selected.scrollIntoView({ block: "center" });
    }
    $("#schedule-history").querySelectorAll("[data-scheduled-draft]").forEach((button) => {
      button.onclick = async () => {
        const id = button.dataset.scheduledDraft;
        showTab("work");
        showWorkSub("runs");
        await loadJobs();
        OPEN_JOB = id;
        renderDraft(JOBS.find((job) => job.id === id));
      };
    });
  } catch (error) { $("#schedule-next").textContent = error.message; }
  schedulePoll = setTimeout(loadSchedule, 4000);
}

async function runTriagePass(lane) {
  if (scheduleBusy) return;
  scheduleBusy = true;
  $("#schedule-run-issues").disabled = $("#schedule-run-prs").disabled = true;
  try {
    const response = await projectFetch("/api/schedule/run", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Triage": "1", "X-Triage-Repo": currentProject },
      body: JSON.stringify({ lane })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || response.statusText);
    $("#project-message").textContent = "Draft pass started. See the run below.";
  } catch (error) { $("#project-message").textContent = error.message; }
  finally { scheduleBusy = false; loadSchedule(); }
}

$("#schedule-run-issues").onclick = () => runTriagePass("issues");
$("#schedule-run-prs").onclick = () => runTriagePass("prs");
loadProjectSwitcher().catch((error) => showErr("Could not load projects: " + error.message));
if (!$("#p-projects").hidden) window.loadProjectsPanel();
