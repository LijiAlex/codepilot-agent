"use strict";
// Every value from GitHub or an agent is set with textContent: no HTML is ever built from data.
const PAGES = ["overview", "matrix", "prs", "safety", "context", "memory", "skills", "audit", "improvements"];
let token = "";
try { token = sessionStorage.getItem("cp-token") || ""; } catch (e) { /* storage blocked: token from the link only */ }
const hash = new URLSearchParams(location.hash.slice(1));
if (hash.get("token")) { token = hash.get("token"); try { sessionStorage.setItem("cp-token", token); } catch (e) {} history.replaceState(null, "", "/"); }

function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; }
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
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
  rows.forEach(cells => { const r = el("tr"); cells.forEach(c => r.appendChild(c instanceof Node ? c : el("td", c))); t.appendChild(r); });
  wrap.appendChild(t); return wrap;
}
function td(node) { const c = el("td"); c.appendChild(node); return c; }
function link(text, href) { const a = el("a", text); if (/^https:\/\/github\.com\//.test(href || "")) { a.href = href; a.rel = "noopener noreferrer"; a.target = "_blank"; } return a; }
function runLink(id) { const a = el("a", id); a.href = "#run=" + encodeURIComponent(id); a.addEventListener("click", ev => { ev.preventDefault(); showRun(id); }); return a; }

const render = {
  async overview(m) {
    const d = await api("overview"), cards = el("div", null, "cards");
    [["Runs", d.runs], ["PRs opened", d.prs_opened], ["Waiting for a decision", d.pending_decisions], ["Commands refused", d.commands_refused],
     ["Issues blocked", d.issues_blocked], ["Tokens in / out", d.tokens_in + " / " + d.tokens_out], ["Cost (USD)", d.cost_usd]]
      .forEach(([k, v]) => { const c = el("div", null, "card"); c.appendChild(el("b", v)); c.appendChild(el("span", k, "muted")); cards.appendChild(c); });
    m.appendChild(cards);
    m.appendChild(table(["State", "Runs"], Object.entries(d.by_state).map(([k, v]) => [k, v])));
  },
  async matrix(m) {
    const d = await api("matrix");
    m.appendChild(table(["Run", "Issue", "Title", "Type", ...d.stages, "Outcome"], d.rows.map(r => [
      td(runLink(r.run_id)), r.repo + " " + r.issue, r.title, r.type || "",
      ...d.stages.map(s => { const c = el("td", r.marks[s] === "done" ? "✓" : r.marks[s] === "failed" ? "✗" : ""); if (r.marks[s]) c.className = r.marks[s]; return c; }),
      td(r.pr_url ? link(r.state, r.pr_url) : el("span", r.state + (r.reason ? " (" + r.reason + ")" : "")))])));
  },
  async prs(m) {
    const d = await api("prs");
    for (const [repo, prs] of Object.entries(d)) {
      m.appendChild(el("h2", repo));
      if (prs.error) { m.appendChild(el("p", "Could not load: " + prs.error, "muted")); continue; }
      m.appendChild(table(["PR", "Author", "Labels", "Independent review", "Actions"], prs.map(p => {
        const actions = el("td");
        const mk = (label, action, cls) => { const b = el("button", label, cls); b.addEventListener("click", async () => {
          if (!confirm(label + " " + repo + "#" + p.number + " at " + p.head_sha.slice(0, 7) + "?")) return;
          try { await api("prs/" + repo + "/" + p.number + "/" + action, { expected_head: p.head_sha, confirm: true }); show("prs"); }
          catch (e) { alert(e.message); } }); return b; };
        if (p.merge_ready && p.review && p.review.current) actions.appendChild(mk("Merge", "merge"));
        actions.appendChild(mk("Close", "close", "danger"));
        const rev = el("td"); if (p.review) { rev.appendChild(el("pre", p.review.body)); if (!p.review.current) rev.appendChild(el("p", "review is for an older commit", "muted")); } else rev.textContent = "review unavailable";
        return [td(link("#" + p.number + " " + p.title, p.url)), p.author, p.labels.join(", "), rev, actions];
      })));
    }
  },
  async safety(m) {
    const d = await api("safety");
    const sec = (title, rows) => { m.appendChild(el("h2", title)); m.appendChild(table(["When", "Run", "Who", "What", "Result"], rows.map(r => [r.ts, r.run_id || "", r.actor, r.target || "", r.result]))); };
    sec("Commands refused by the guard", d.commands_refused); sec("Unexpected commands sent to a person", d.commands_needing_decision);
    sec("Tools refused (not on the agent's list)", d.tools_refused); sec("Issues blocked by the injection check", d.issues_blocked);
    if (d.guardrail_measurement) { const g = d.guardrail_measurement; m.appendChild(el("h2", "Guardrail measurement"));
      m.appendChild(el("p", `Malicious blocked ${g.detected}/${g.malicious} · benign wrongly blocked ${g.false_alarms}/${g.benign} · failed closed ${g.failed_closed} · p50 ${g.p50_ms} ms`)); }
  },
  async context(m) {
    const d = await api("context");
    m.appendChild(table(["Run", "Issue", "Repo map tokens", "Files in map", "Lessons recalled", "Skill", "Tokens in / out"], d.runs.map(r => {
      const c = r.context || {}; return [td(runLink(r.run_id)), r.repo + " " + r.issue, c.map_tokens ?? "", c.map_files ?? "", c.lessons ?? "", c.skill ?? "", r.tokens_in + " / " + r.tokens_out]; })));
  },
  async memory(m) {
    const d = await api("memory");
    for (const [repo, lessons] of Object.entries(d.lessons)) { m.appendChild(el("h2", "Lessons — " + repo));
      m.appendChild(table(["Type", "Source", "Lesson"], lessons.map(l => [l.task_type, l.source, l.text]))); }
    m.appendChild(el("h2", "Sessions")); m.appendChild(table(["Session", "Summary"], d.sessions.map(s => [s.session_id, s.summary])));
  },
  async skills(m) {
    const d = await api("skills");
    m.appendChild(table(["Skill", "Task type", "Workflow", "Forbidden"], d.brief_skills.map(s => [s.display_name, s.task_type, s.workflow_steps.join(" → "), s.forbidden_actions.join("; ")])));
    for (const [repo, versions] of Object.entries(d.repo_skills || {})) { m.appendChild(el("h2", "Repo skill — " + repo));
      m.appendChild(table(["Version", "When", "Change"], versions.map(v => [v.version, v.created_at, v.change]))); }
  },
  async audit(m) {
    const d = await api("audit");
    m.appendChild(table(["#", "When", "Run", "Actor", "Action", "Target", "Result"], d.events.map(e => [e.id, e.ts, e.run_id || "", e.actor, e.action, e.target || "", e.result])));
  },
  async improvements(m) {
    const d = await api("improvements");
    m.appendChild(el("h2", "Proposals waiting for a decision"));
    m.appendChild(table(["Repo", "Change", "Evidence", "Confidence", "Decide"], d.proposals.map(p => {
      const c = el("td"); ["approve", "reject"].forEach(dec => { const b = el("button", dec, dec === "reject" ? "danger" : ""); b.addEventListener("click", async () => {
        if (!confirm(dec + " this change to the repo skill?")) return; try { await api("improvements/" + p.id + "/" + dec, { confirm: true }); show("improvements"); } catch (e) { alert(e.message); } }); c.appendChild(b); });
      return [p.repo, p.change, p.evidence, p.confidence, c]; })));
    m.appendChild(el("h2", "Friction per repo-skill version"));
    m.appendChild(table(["Repo", "Version", "Runs", "Failed commands / run", "Tool calls / run", "First-try test pass"], d.trend.map(t => [t.repo, t.version, t.runs, t.failed_commands, t.tool_calls, t.first_try_pass])));
  },
};

async function showRun(id) {
  const m = document.getElementById("main"); clear(m);
  try {
    const d = await api("runs/" + encodeURIComponent(id));
    m.appendChild(el("h2", d.run.repo + " " + d.run.issue_key + " — " + d.run.title));
    m.appendChild(el("p", d.run.state + (d.run.reason ? " (" + d.run.reason + ")" : "") + " · " + (d.run.task_type || "") + " · complexity " + (d.run.complexity ?? "")));
    if (d.context) m.appendChild(el("p", `Context: repo map ${d.context.map_tokens} tokens, ${d.context.lessons} lesson(s), ${d.context.skill}`, "muted"));
    d.handoffs.forEach(h => { m.appendChild(el("h3", h.entry_point + " — " + h.status)); if (h.payload.diff) m.appendChild(el("pre", h.payload.diff)); });
    if (d.skill_gap_report) { m.appendChild(el("h3", "Skill gap report")); m.appendChild(el("pre", JSON.stringify(d.skill_gap_report, null, 2))); }
    if (d.trace_analysis) { m.appendChild(el("h3", "Trace analysis")); m.appendChild(el("pre", JSON.stringify(d.trace_analysis, null, 2))); }
    m.appendChild(el("h3", "Agent log"));
    m.appendChild(table(["Agent", "Kind", "Text"], d.events.map(e => [e.agent, e.kind, e.text])));
  } catch (e) { m.appendChild(el("p", "Could not load the run: " + e.message)); }
}

async function show(page) {
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.page === page));
  const m = document.getElementById("main"); clear(m);
  try { await render[page](m); document.getElementById("status").textContent = ""; }
  catch (e) { m.appendChild(el("p", "Could not load: " + e.message + (token ? "" : " — open the link printed by `codepilot dashboard`."))); }
}

document.addEventListener("DOMContentLoaded", () => {
  const nav = document.getElementById("nav");
  PAGES.forEach(p => { const b = el("button", p[0].toUpperCase() + p.slice(1)); b.dataset.page = p; b.addEventListener("click", () => show(p)); nav.appendChild(b); });
  const run = new URLSearchParams(location.hash.slice(1)).get("run");
  run ? showRun(run) : show("overview");
});
