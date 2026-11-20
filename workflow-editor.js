// Form fields map to the existing profile keys. Missing keys keep their server defaults.
const workflowFields = [
  { path: "labels.statuses.triage", label: "Needs triage", group: "primary-status-mappings", fallback: "needs-triage" },
  { path: "labels.statuses.needs-information", label: "Needs information", group: "primary-status-mappings", fallback: "needs-info" },
  { path: "labels.statuses.ready-for-work", label: "Ready for agent", group: "primary-status-mappings", fallback: "ready-for-agent" },
  { path: "labels.statuses.ready-for-human", label: "Ready for human", group: "primary-status-mappings", fallback: "ready-for-human" },
  { path: "labels.wontfix", label: "Won’t fix", group: "primary-status-mappings", fallback: "wontfix" },
  { path: "labels.statuses.needs-scope-review", label: "Needs scope review", group: "other-status-mappings" },
  { path: "labels.statuses.design-needed", label: "Design needed", group: "other-status-mappings" },
  { path: "labels.statuses.design-review", label: "Design in review", group: "other-status-mappings" },
  { path: "labels.statuses.in-progress", label: "In progress", group: "other-status-mappings" },
  { path: "labels.statuses.waiting-on-author", label: "Waiting on author", group: "other-status-mappings" },
  { path: "labels.statuses.waiting-on-dependency", label: "Waiting on dependency", group: "other-status-mappings" },
  { path: "labels.statuses.waiting-on-review", label: "Waiting for PR review", group: "other-status-mappings" },
  { path: "labels.statuses.ready-to-merge", label: "Ready to merge", group: "other-status-mappings" },
  { path: "labels.statuses.inactive", label: "Inactive", group: "other-status-mappings" },
  { path: "labels.claimed", label: "Claimed", group: "special-label-mappings" },
  { path: "labels.tracker", label: "Tracker issue", group: "special-label-mappings" },
  { path: "labels.maintainer_decision", label: "Maintainer decision", group: "special-label-mappings" },
  { path: "labels.design_approved", label: "Design approved", group: "special-label-mappings" },
  { path: "labels.priorities", id: "rule-priorities", type: "lines" },
  { path: "labels.kinds", id: "rule-kinds", type: "lines" },
  { path: "labels.area_prefix", id: "rule-area-prefix" },
  { path: "workflow.require_maintainer_review", id: "rule-maintainer-review", type: "checkbox" },
  { path: "workflow.require_up_to_date", id: "rule-up-to-date", type: "checkbox" },
  { path: "workflow.require_resolved_threads", id: "rule-resolved-threads", type: "checkbox" },
  { path: "workflow.require_status_label", id: "rule-status-label", type: "checkbox" },
  { path: "workflow.required_checks", id: "rule-required-checks", type: "lines" },
  { path: "workflow.merge_method", id: "rule-merge-method" },
  { path: "workflow.standing_authorization", id: "rule-standing-auth", type: "commas" },
  { path: "workflow.post_authorization_comment", id: "rule-auth-comment", type: "checkbox" },
].map((field) => ({ id: "rule-" + field.path.replaceAll(".", "-").replaceAll("_", "-"), ...field,
                   keys: field.path.split(".") }));

function nestedValue(data, keys) {
  return keys.reduce((parent, key) => parent && Object.hasOwn(parent, key) ? parent[key] : undefined, data);
}

function workflowDefaults(field, original) {
  const stored = nestedValue(original, field.keys);
  if (stored !== undefined) return stored;
  if (field.type === "checkbox") return false;
  if (field.type === "lines" || field.type === "commas") return [];
  return field.fallback ?? null;
}

function removeWorkflowField(data, keys) {
  const [key, ...rest] = keys;
  if (!Object.hasOwn(data, key)) return;
  if (!rest.length) delete data[key];
  else if (data[key] && typeof data[key] === "object" && !Array.isArray(data[key])) {
    removeWorkflowField(data[key], rest);
    if (!Object.keys(data[key]).length) delete data[key];
  }
}

function advancedWorkflowSettings(profile) {
  const remaining = structuredClone(profile);
  for (const field of workflowFields) removeWorkflowField(remaining, field.keys);
  return remaining;
}

function collectWorkflowSettings(advanced, original, read) {
  for (const keys of [["labels"], ["labels", "statuses"], ["workflow"]]) {
    const value = nestedValue(advanced, keys);
    if (value !== undefined && (!value || typeof value !== "object" || Array.isArray(value))) {
      throw new Error(keys.join(".") + " in Advanced settings must be a JSON object.");
    }
  }
  const result = structuredClone(advanced);
  for (const field of workflowFields) {
    if (nestedValue(advanced, field.keys) !== undefined) {
      throw new Error("Use the form to edit " + field.path + " and remove that key from Advanced settings.");
    }
    const stored = nestedValue(original, field.keys);
    const next = read(field);
    const current = workflowDefaults(field, original);
    const expected = typeof current === "string" ? current.trim()
      : Array.isArray(current) ? current.map((value) => value.trim()) : current;
    const unchanged = JSON.stringify(next) === JSON.stringify(expected);
    const value = unchanged ? stored : next;
    if (value === undefined) continue;
    let parent = result;
    for (const key of field.keys.slice(0, -1)) parent = parent[key] ??= {};
    parent[field.keys.at(-1)] = value;
  }
  return result;
}

function createWorkflowEditor(document) {
  for (const field of workflowFields.filter((item) => item.group)) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.id = field.id;
    input.setAttribute("list", "project-github-labels");
    input.spellcheck = false;
    input.placeholder = "GitHub label";
    label.append(field.label, input);
    document.getElementById(field.group).append(label);
  }
  const control = (field) => document.getElementById(field.id);
  let original = {};
  return {
    load(profile) {
      original = profile;
      for (const field of workflowFields) {
        const value = workflowDefaults(field, original);
        if (field.type === "checkbox") control(field).checked = value;
        else if (field.type === "lines") control(field).value = value.join("\n");
        else if (field.type === "commas") control(field).value = value.join(", ");
        else control(field).value = value ?? "";
      }
      return advancedWorkflowSettings(original);
    },
    collect(advanced) {
      return collectWorkflowSettings(advanced, original, (field) => {
        const input = control(field);
        if (field.type === "checkbox") return input.checked;
        if (field.type === "lines" || field.type === "commas") {
          return input.value.split(field.type === "lines" ? /\r?\n/ : ",").map((value) => value.trim()).filter(Boolean);
        }
        return input.value.trim() || null;
      });
    },
    setLabelChoices(labels) {
      const list = document.getElementById("project-github-labels");
      list.replaceChildren(...labels.map((name) => new Option(name, name)));
    },
  };
}

if (typeof module !== "undefined") {
  module.exports = { advancedWorkflowSettings, collectWorkflowSettings, createWorkflowEditor, workflowDefaults, workflowFields };
}
