"use strict";
// Every value from GitHub or an agent is set with textContent: no HTML is ever built from data.
const PAGES = [
  ["overview", "Overview", "Totals across every run CodePilot+ has done."],
  ["matrix", "Runs", "Every run and the stages it reached. Click a run ID for its diff, log and analysis."],
  ["prs", "Pull requests", "Open pull requests with the independent review. Merge needs a current review that says merge-ready."],
  ["safety", "Safety", "What the guardrails refused or held back, and the measured injection screen."],
  ["context", "Context", "How much context each run was given: repo map size against its budget, lessons recalled, skill used."],
  ["memory", "Memory", "Lessons learned from past work, and a summary of each recent session."],
  ["skills", "Skills", "The five task skills and each repository's own skill (its versions)."],
  ["audit", "Audit log", "Every action, by whom, in order."],
  ["improvements", "Improvements", "Proposed changes to a repository's skill, waiting for a person, and how runs went per skill version."],
];
const STATUS = { PR_OPENED: "PR opened", AWAITING_APPROVAL: "waiting for approval", FAILED: "failed", DONE: "merged",
  CLOSED: "closed", SKIPPED: "skipped", EXPLORING: "exploring", IMPLEMENTING: "implementing", TESTING: "testing", TRIAGED: "triaged" };
let token = "";
try { token = sessionStorage.getItem("cp-token") || ""; } catch (e) { /* storage blocked: token from the link only */ }
const hash = new URLSearchParams(location.hash.slice(1));
if (hash.get("token")) { token = hash.get("token"); try { sessionStorage.setItem("cp-token", token); } catch (e) {} history.replaceState(null, "", "/"); }

function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; }
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function num(n) { return n === null || n === undefined || n === "" ? "" : Number(n).toLocaleString(); }
function status(s) { return STATUS[s] || String(s || "").toLowerCase().replace(/_/g, " "); }
function short(text, n = 70) { const first = String(text || "").split("\n").map(x => x.trim()).filter(Boolean); const t = first.join(" "); return t.length > n ? t.slice(0, n - 1) + "…" : t; }
function when(v) {                      // session times are nanosecond timestamps; other times are already text
  if (typeof v === "number") { const ms = v > 1e15 ? v / 1e6 : v > 1e12 ? v : v * 1000; return new Date(ms).toLocaleString(); }
  return v || "";
}
function pct(x) { return x === null || x === undefined || x === "" ? "" : Math.round(Number(x) * 100) + "%"; }
async function api(path, body) {
  const opts = { headers: { "X-CodePilot-Token": token } };
  if (body) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch("/api/" + path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || r.status);
  return j;
}
function table(headers, rows) {
  const wrap = el("div", null, "table-wrap"), t = el("table"), tr = el("tr");
  headers.forEach(h => tr.appendChild(el("th", h))); t.appendChild(tr);
  if (!rows.length) { const r = el("tr"), c = el("td", "None so far", "muted"); c.colSpan = headers.length; r.appendChild(c); t.appendChild(r); }
  rows.forEach(cells => { const r = el("tr"); cells.forEach(c => r.appendChild(c instanceof Node ? c : el("td", c))); t.appendChild(r); });
  wrap.appendChild(t); return wrap;
}
function td(node) { const c = el("td"); c.appendChild(node); return c; }
function tdTitle(text, full) { const c = el("td", text); if (full && full !== text) c.title = full; return c; }
function link(text, href) { const a = el("a", text); if (/^https:\/\/github\.com\//.test(href || "")) { a.href = href; a.rel = "noopener noreferrer"; a.target = "_blank"; } return a; }
function runLink(id) { if (!id) return el("span", ""); const a = el("a", id); a.href = "#run=" + encodeURIComponent(id); a.addEventListener("click", ev => { ev.preventDefault(); showRun(id); }); return a; }
function textWithLinks(text) {           // GitHub URLs become links; everything else stays plain text
  const span = el("span"); String(text || "").split(/(https:\/\/github\.com\/\S+)/).forEach(part => span.appendChild(/^https:\/\/github\.com\//.test(part) ? link(part, part) : document.createTextNode(part)));
  return span;
}
function textWithCode(text) {           // `code` spans shown as code, built with textContent only
  const span = el("span"); String(text || "").split(/(`[^`]+`)/).forEach(part => span.appendChild(/^`[^`]+`$/.test(part) ? el("code", part.slice(1, -1)) : document.createTextNode(part)));
  return span;
}
function details(summary, body) { const d = el("details"), s = el("summary", summary); d.appendChild(s); d.appendChild(el("pre", body)); return d; }
function diffBlock(diff) {
  const pre = el("pre", null, "diff");
  String(diff).split("\n").forEach(line => { const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : ""; pre.appendChild(el("span", line + "\n", cls)); });
  return pre;
}

const render = {
  async overview(m) {
    const d = await api("overview"), cards = el("div", null, "cards");
    [["Runs", num(d.runs)], ["PRs opened", num(d.prs_opened)], ["Runs waiting for approval (TUI)", num(d.pending_decisions)],
     ["Skill changes waiting (Improvements)", num(d.proposals_waiting)], ["Commands refused", num(d.commands_refused)],
     ["Issues blocked", num(d.issues_blocked)], ["Tokens in / out", num(d.tokens_in) + " / " + num(d.tokens_out)],
     ["Cost (USD)", d.cost_usd === null ? "not tracked" : "$" + d.cost_usd]]
      .forEach(([k, v]) => { const c = el("div", null, "card"); c.appendChild(el("b", v)); c.appendChild(el("span", k, "muted")); cards.appendChild(c); });
    m.appendChild(cards);
    if (d.cost_usd === null) m.appendChild(el("p", "Cost is not tracked: add price_in_per_mtok and price_out_per_mtok to codepilot.yml.", "muted"));
    m.appendChild(table(["Status", "Runs"], Object.entries(d.by_state).map(([k, v]) => [status(k), num(v)])));
  },
  async matrix(m) {
    const d = await api("matrix");
    m.appendChild(table(["Run", "Issue", "Outcome", "Title", "Type", ...d.stages], d.rows.map(r => [
      td(runLink(r.run_id)), r.repo.split("/").pop() + " " + r.issue,
      r.pr_url ? td(link(status(r.state), r.pr_url)) : tdTitle(status(r.state) + (r.reason ? " — " + short(r.reason, 50) : ""), r.reason || ""),
      r.title, (r.type || "").replace(/_/g, " "),
      ...d.stages.map(s => { const c = el("td", r.marks[s] === "done" ? "✓" : r.marks[s] === "failed" ? "✗" : ""); if (r.marks[s]) c.className = r.marks[s]; return c; })])));
  },
  async prs(m) {
    const d = await api("prs");
    for (const [repo, prs] of Object.entries(d)) {
      m.appendChild(el("h2", repo));
      if (prs.error) { m.appendChild(el("p", "Could not load: " + prs.error, "muted")); continue; }
      m.appendChild(table(["PR", "Actions", "Independent review", "Labels", "Author"], prs.map(p => {
        const actions = el("td", null, "actions");
        const mk = (label, action, cls, question) => { const b = el("button", label, cls); b.addEventListener("click", async () => {
          if (!confirm(question)) return;
          try { await api("prs/" + repo + "/" + p.number + "/" + action, { expected_head: p.head_sha, confirm: true }); show("prs"); }
          catch (e) { alert(e.message); } }); return b; };
        const why = !p.review ? "needs an independent review" : !p.review.current ? "review is for an older commit" : !p.merge_ready ? "review does not say merge-ready" : "";
        const merge = mk("Merge", "merge", "", "Squash-merge " + repo + "#" + p.number + " at " + p.head_sha.slice(0, 7) + " into the default branch?");
        if (why) { merge.disabled = true; merge.title = why; }
        actions.appendChild(merge);
        actions.appendChild(mk("Close", "close", "danger", "Close " + repo + "#" + p.number + " without merging? (It can be reopened on GitHub.)"));
        if (why) actions.appendChild(el("div", "Merge: " + why, "muted small"));
        const rev = el("td"); if (p.review) { rev.appendChild(el("pre", p.review.body)); if (!p.review.current) rev.appendChild(el("p", "review is for an older commit", "muted")); } else rev.appendChild(el("span", "not reviewed yet", "muted"));
        return [td(link("#" + p.number + " " + p.title, p.url)), actions, rev, p.labels.join(", "), p.author];
      })));
    }
  },
  async safety(m) {
    const d = await api("safety");
    const sec = (title, rows) => { m.appendChild(el("h2", title)); m.appendChild(table(["When", "Run", "Who", "What", "Result"], rows.map(r => [r.ts, td(runLink(r.run_id)), r.actor, r.target || "", r.result]))); };
    sec("Commands refused by the guard", d.commands_refused); sec("Unexpected commands sent to a person", d.commands_needing_decision);
    sec("Tools refused (not on the agent's list)", d.tools_refused); sec("Issues blocked by the injection screen", d.issues_blocked);
    if (d.guardrail_measurement) { const g = d.guardrail_measurement; m.appendChild(el("h2", "Injection screen, measured on labelled issues"));
      m.appendChild(table(["Measure", "Result"], [["Attacks blocked", g.detected + " of " + g.malicious], ["Harmless issues wrongly blocked", g.false_alarms + " of " + g.benign],
        ["Screen did not answer (issue held back)", num(g.failed_closed)], ["Typical time per issue (median)", num(g.p50_ms) + " ms"]])); }
  },
  async context(m) {
    const d = await api("context"), budget = d.budget || 4000;
    m.appendChild(table(["Run", "Issue", "Repo map tokens (budget " + num(budget) + ")", "Files in map", "Lessons recalled", "Skill", "Tokens in / out"], d.runs.map(r => {
      const c = r.context || {}, over = c.map_tokens > budget;
      const mapCell = el("td", c.map_tokens === undefined ? "no context recorded" : num(c.map_tokens) + " / " + num(budget) + (over ? " — over budget" : ""), over ? "failed" : (c.map_tokens === undefined ? "muted" : ""));
      return [td(runLink(r.run_id)), r.repo.split("/").pop() + " " + r.issue, mapCell, num(c.map_files), num(c.lessons), (c.skill || "").replace(/_/g, " "), num(r.tokens_in) + " / " + num(r.tokens_out)]; })));
  },
  async memory(m) {
    const d = await api("memory");
    for (const [repo, lessons] of Object.entries(d.lessons)) { m.appendChild(el("h2", "Lessons — " + repo));
      m.appendChild(table(["Type", "Source", "Lesson"], lessons.map(l => [(l.task_type || "").replace(/_/g, " "), l.source, l.text]))); }
    m.appendChild(el("h2", "Recent sessions"));
    m.appendChild(el("p", "Each summary is a snapshot taken when that session ended; a run's current status is on the Runs page.", "muted"));
    const sessions = d.sessions.filter(s => s.summary && s.summary !== "no runs");
    m.appendChild(table(["Started", "Summary (at session end)"], sessions.map(s => [when(s.started), tdTitle(short(s.summary, 120), s.summary)])));
  },
  async skills(m) {
    const d = await api("skills");
    m.appendChild(table(["Skill", "Task type", "Workflow", "Must not"], d.brief_skills.map(s => [s.display_name, s.task_type.replace(/_/g, " "), s.workflow_steps.join(" → "), s.forbidden_actions.join("; ")])));
    for (const [repo, versions] of Object.entries(d.repo_skills || {})) { m.appendChild(el("h2", "Repo skill — " + repo));
      m.appendChild(table(["Version", "When", "Change"], versions.map(v => [v.version, v.created_at, td(textWithCode(v.change))]))); }
  },
  async audit(m) {
    const d = await api("audit");
    m.appendChild(table(["#", "When", "Run", "Who", "Action", "Target", "Result"], d.events.map(e => [e.id, e.ts, td(runLink(e.run_id)),
      e.actor === "human" ? "human (TUI)" : e.actor, e.action.replace(/_/g, " "), td(textWithLinks(e.target || "")), e.result])));
  },
  async improvements(m) {
    const d = await api("improvements");
    m.appendChild(el("h2", "Proposed skill changes waiting for a decision"));
    m.appendChild(el("p", "Confidence rises each time later runs propose the same change (3 sightings = 100%). Nothing changes until you approve.", "muted"));
    m.appendChild(table(["Repo", "Change", "Evidence", "Confidence", "Decide"], d.proposals.map(p => {
      const c = el("td"); ["approve", "reject"].forEach(dec => { const b = el("button", dec, dec === "reject" ? "danger" : ""); b.addEventListener("click", async () => {
        if (!confirm(dec[0].toUpperCase() + dec.slice(1) + " this change to " + p.repo + "'s repo skill?\n\n" + p.change)) return;
        try { await api("improvements/" + p.id + "/" + dec, { confirm: true }); show("improvements"); } catch (e) { alert(e.message); } }); c.appendChild(b); });
      return [p.repo, td(textWithCode(p.change)), td(textWithCode(p.evidence)), pct(p.confidence), c]; })));
    m.appendChild(el("h2", "How runs went per repo-skill version"));
    m.appendChild(table(["Repo", "Version", "Runs", "Failed commands per run", "Tool calls per run", "Tests passed on first try"],
      d.trend.map(t => [t.repo, t.version, num(t.runs), t.failed_commands, t.tool_calls, t.first_try_pass === null ? "—" : pct(t.first_try_pass)])));
  },
};

function setActive(page) { document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.page === page)); }
function loading(m) { clear(m); m.appendChild(el("p", "Loading…", "muted loading")); }

async function showRun(id) {
  setActive(""); const m = document.getElementById("main"); loading(m);
  try {
    const d = await api("runs/" + encodeURIComponent(id)); clear(m);
    const back = el("a", "← Back to runs", "back"); back.href = "#"; back.addEventListener("click", ev => { ev.preventDefault(); show("matrix"); }); m.appendChild(back);
    m.appendChild(el("h2", d.run.repo + " " + d.run.issue_key + " — " + d.run.title));
    m.appendChild(el("p", "Now: " + status(d.run.state) + " · " + (d.run.task_type || "").replace(/_/g, " ") + " · complexity " + (d.run.complexity ?? "") + "/10"));
    if (d.run.reason) m.appendChild(details("Why it ended this way", d.run.reason));
    if (d.run.pr_url) { const p = el("p"); p.appendChild(link("Pull request: " + d.run.pr_url, d.run.pr_url)); m.appendChild(p); }
    if (d.context) m.appendChild(el("p", `Context: repo map ${num(d.context.map_tokens)} tokens, ${d.context.lessons} lesson(s), ${(d.context.skill || "").replace(/_/g, " ")}`, "muted"));
    d.handoffs.forEach(h => { m.appendChild(el("h3", h.entry_point.replace(/_/g, " ") + " — " + h.status)); if (h.payload.diff) m.appendChild(diffBlock(h.payload.diff)); });
    if (d.skill_gap_report) m.appendChild(details("Skill gap report (written when the run ended)", JSON.stringify(d.skill_gap_report, null, 2)));
    if (d.trace_analysis) m.appendChild(details("Trace analysis (state when the run ended)", JSON.stringify(d.trace_analysis, null, 2)));
    m.appendChild(el("h3", "Agent log"));
    m.appendChild(table(["Agent", "Kind", "Text"], d.events.map(e => [e.agent, e.kind.replace(/_/g, " "), e.text])));
  } catch (e) { clear(m); m.appendChild(el("p", "Could not load the run: " + e.message)); }
}

async function show(page) {
  setActive(page);
  const m = document.getElementById("main"); loading(m);
  const info = PAGES.find(p => p[0] === page);
  const holder = el("div");
  try { await render[page](holder); clear(m); m.appendChild(el("p", info[2], "page-intro")); m.appendChild(holder); }
  catch (e) { clear(m); m.appendChild(el("p", "Could not load: " + e.message + (token ? "" : " — open the link printed by `codepilot dashboard`."))); }
}

document.addEventListener("DOMContentLoaded", () => {
  const nav = document.getElementById("nav");
  PAGES.forEach(([p, label]) => { const b = el("button", label); b.dataset.page = p; b.addEventListener("click", () => show(p)); nav.appendChild(b); });
  const run = new URLSearchParams(location.hash.slice(1)).get("run");
  run ? showRun(run) : show("overview");
});
