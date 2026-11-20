const test = require("node:test");
const assert = require("node:assert/strict");
const {
  advancedWorkflowSettings, collectWorkflowSettings, workflowDefaults, workflowFields
} = require("../workflow-editor.js");

const edit = (profile, advanced, changes = {}) => collectWorkflowSettings(
  advanced, profile, (field) => Object.hasOwn(changes, field.path)
    ? changes[field.path]
    : workflowDefaults(field, profile)
);

test("existing mappings, explicit nulls, and advanced settings survive a save", () => {
  const profile = {
    labels: {
      statuses: { triage: "status:triage", "needs-information": null },
      wontfix: null,
      priorities: ["Urgent", "Normal"]
    },
    workflow: { require_maintainer_review: true, gate_commands: [["python", "-m", "unittest"]] },
    review: { timeout_min: 20 }
  };
  const advanced = advancedWorkflowSettings(profile);
  assert.deepEqual(advanced, {
    workflow: { gate_commands: [["python", "-m", "unittest"]] },
    review: { timeout_min: 20 }
  });
  assert.deepEqual(edit(profile, advanced), profile);
  assert.deepEqual(edit({}, {}), {});
});

test("edits update only the relevant keys and can clear a default mapping", () => {
  const profile = { workflow: { policy_files: ["GOVERNANCE.md"] } };
  const saved = edit(profile, advancedWorkflowSettings(profile), {
    "labels.statuses.triage": null,
    "labels.statuses.waiting-on-review": "Ready for eyes",
    "labels.kinds": ["defect", "feature"],
    "workflow.require_resolved_threads": true,
    "workflow.required_checks": ["Test, Linux", "CI / Format"],
    "workflow.merge_method": "squash",
    "workflow.standing_authorization": ["alice"]
  });
  assert.deepEqual(saved, {
    labels: { statuses: { triage: null, "waiting-on-review": "Ready for eyes" }, kinds: ["defect", "feature"] },
    workflow: { policy_files: ["GOVERNANCE.md"], require_resolved_threads: true,
      required_checks: ["Test, Linux", "CI / Format"], merge_method: "squash", standing_authorization: ["alice"] }
  });
  const triage = workflowFields.find((field) => field.path === "labels.statuses.triage");
  assert.equal(workflowDefaults(triage, saved), null);
});

test("advanced edits cannot silently override a field in the form", () => {
  assert.throws(() => edit({}, { workflow: { merge_method: "merge" } }), /Use the form to edit workflow.merge_method/);
  assert.throws(() => edit({}, { labels: { statuses: null } }), /labels.statuses.*must be a JSON object/);
  assert.deepEqual(edit({}, { workflow: { pr_template: "docs/template.md" } }),
    { workflow: { pr_template: "docs/template.md" } });
});
