"use strict";
// Every value from GitHub or an agent is set with textContent: no HTML is ever built from data.
const PAGES = [
  ["overview", "Overview", "Totals across every run CodePilot+ has done."],
  ["runs", "Runs", "Every run and the stages it reached. Click a run ID for its diff, log and analysis."],
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
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(v || "")) return new Date(v.replace(" ", "T") + "Z").toLocaleString();   // stored in UTC
  return v || "";
}
function pct(x) { return x === null || x === undefined || x === "" ? "" : Math.round(Number(x) * 100) + "%"; }
async function api(path, body) {
  const opts = { headers: { "X-CodePilot-Token": token } };
  if (body) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch("/api/" + path, opts);
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) throw new Error("this page's access link is out of date or missing — open the link printed by `codepilot dashboard` (it stays the same across restarts)");
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
function runLink(id) { if (!id) return el("span", ""); const a = el("a", id); a.href = "#run=" + encodeURIComponent(id); return a; }   // routed by hashchange
function textWithLinks(text) {           // GitHub URLs become links; everything else stays plain text
  const span = el("span"); String(text || "").split(/(https:\/\/github\.com\/\S+)/).forEach(part => span.appendChild(/^https:\/\/github\.com\//.test(part) ? link(part, part) : document.createTextNode(part)));
  return span;
}
function textWithCode(text) {           // `code` spans as code, [run id] as a run link; built with textContent only
  const span = el("span");
  String(text || "").split(/(`[^`]+`|\[[0-9a-f]{12}\])/).forEach(part => span.appendChild(
    /^`[^`]+`$/.test(part) ? el("code", part.slice(1, -1)) : /^\[[0-9a-f]{12}\]$/.test(part) ? runLink(part.slice(1, -1)) : document.createTextNode(part)));
  return span;
}
function friendlySummary(text) {        // "FAILED(error: X): 1 | PR_OPENED: 2" -> "failed (error: X) ×1, PR opened ×2"
  const flat = String(text || "").replace(/\s+/g, " ");               // stored reasons may span lines
  return flat.replace(/\b([A-Z_]{4,})(\(((?:[^()]|\([^()]*\))*)\))?: (\d+)/g,    // one level of nested brackets, e.g. "failed (1)"
    (m, st, _p, why, n) => status(st) + (why ? " (" + why.trim() + ")" : "") + " ×" + n);
}
const MARKS = { done: ["✓", ""], failed: ["✗", ""], voided: ["!", "Tests passed, but a later change was not re-tested, so the run stopped as untested"] };
function markCell(mark) { const [sym, why] = MARKS[mark] || ["", ""], c = el("td", sym); if (mark) c.className = mark; if (why) c.title = why; return c; }
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
     ["Cost (USD)" + (d.cost_source ? " — " + d.cost_source : ""), d.cost_usd === null ? "not tracked" : "$" + d.cost_usd]]
      .forEach(([k, v]) => { const c = el("div", null, "card"); c.appendChild(el("b", v)); c.appendChild(el("span", k, "muted")); cards.appendChild(c); });
    m.appendChild(cards);
    if (d.usage) {                                     // from LangSmith traces
      const sum = (o) => Object.values(o).reduce((a, u) => ({ cost: a.cost + (u.cost || 0), tokens_in: a.tokens_in + (u.tokens_in || 0), tokens_out: a.tokens_out + (u.tokens_out || 0) }), { cost: 0, tokens_in: 0, tokens_out: 0 });
      const rows = [["Agents (all runs)", sum(d.usage.runs)], ["Injection screen (NeMo, Haiku)", d.usage.screen], ["Independent reviewer (GitHub Action)", d.usage.reviewer]];
      m.appendChild(el("h2", "Where the tokens and cost go (from LangSmith traces)"));
      m.appendChild(table(["Part", "Tokens in", "Tokens out", "Cost (USD)"], rows.map(([k, u]) => [k, num(u.tokens_in || 0), num(u.tokens_out || 0), "$" + (u.cost || 0).toFixed(3)])));
    }
    if (d.cost_usd === null) m.appendChild(el("p", "Cost is not tracked: set LANGSMITH_API_KEY (LangSmith prices every model call) or add price_in_per_mtok and price_out_per_mtok to codepilot.yml.", "muted"));
    m.appendChild(table(["Status", "Runs"], Object.entries(d.by_state).map(([k, v]) => [status(k), num(v)])));
  },
  async runs(m) {
    const d = await api("matrix");
    m.appendChild(table(["Run", "Issue", "Outcome", "Cost", "Title", "Type", ...d.stages], d.rows.map(r => [
      td(runLink(r.run_id)), r.repo.split("/").pop() + " " + r.issue,
      r.pr_url ? td(link(status(r.state), r.pr_url)) : tdTitle(status(r.state) + (r.reason ? " — " + short(r.reason, 50) : ""), r.reason || ""),
      r.cost_usd === null || r.cost_usd === undefined ? "" : "$" + Number(r.cost_usd).toFixed(3),
      el("td", r.title, "title"), (r.type || "").replace(/_/g, " "),
      ...d.stages.map(s => markCell(r.marks[s]))])));
  },
  async prs(m) {
    const d = await api("prs");
    for (const [repo, prsIn] of Object.entries(d)) {
      m.appendChild(el("h2", repo));
      if (prsIn.error) { m.appendChild(el("p", "Could not load: " + prsIn.error, "muted")); continue; }
      const prs = [...prsIn].sort((a, b) => (b.attention || []).length - (a.attention || []).length);
      m.appendChild(table(["", "PR", "Score", "Effort", "Merge-ready", "Matches the issue", "Security", "Actions", "Review", "Labels", "Author"], prs.map(p => {
        const f = (p.review && p.review.fields) || {};
        const att = el("td", null, (p.attention || []).length ? "attention" : "");
        if ((p.attention || []).length) { att.appendChild(el("span", "⚠ ", "warn")); att.appendChild(el("span", p.attention.join(", "))); att.title = "A person should look at this PR"; }
        const actions = el("td", null, "actions");
        const mk = (label, action, cls, question) => { const b = el("button", label, cls); b.addEventListener("click", async () => {
          if (!confirm(question)) return;
          try { await api("prs/" + repo + "/" + p.number + "/" + action, { expected_head: p.head_sha, confirm: true }); show("prs"); }
          catch (e) { alert(e.message); } }); return b; };
        const why = !p.review ? "needs an independent review" : !p.review.current ? "review is for an older commit" : !p.merge_ready ? "review does not say merge-ready" : "";
        const merge = mk("Merge", "merge", "", "Squash-merge " + repo + "#" + p.number + " at " + p.head_sha.slice(0, 7) + " into the default branch?");
        if (why) { merge.disabled = true; merge.title = why; }
        actions.appendChild(merge);
        const close = mk("Close", "close", "danger", "Close " + repo + "#" + p.number + " without merging? (It can be reopened on GitHub.)");
        close.title = "Close without merging (asks first)"; actions.appendChild(close);
        if (why) actions.appendChild(el("div", "Merge: " + why, "muted small"));
        const ready = f.merge_ready === true ? "yes" : f.merge_ready === false ? "no" + (f.why_not ? " — " + f.why_not : "") : "";
        const rev = el("td");
        if (p.review) { rev.appendChild(details(p.review.current ? "full review" : "full review (older commit)", p.review.body)); } else rev.appendChild(el("span", "not reviewed yet", "muted"));
        return [att, td(link("#" + p.number + " " + p.title, p.url)), f.score === null || f.score === undefined ? "" : f.score + "/10",
          f.effort === null || f.effort === undefined ? "" : f.effort + "/5", ready, f.matches || "",
          (f.security || []).length ? el("td", f.security.join("; "), "failed") : "none", actions, rev, p.labels.join(", "),
          p.author + (p.from_fork ? " (fork)" : "")];
      })));
    }
  },
  async safety(m) {
    const d = await api("safety");
    const sec = (title, rows) => { m.appendChild(el("h2", title)); m.appendChild(table(["When", "Run", "Who", "What", "Result"], rows.map(r => [when(r.ts), td(runLink(r.run_id)), r.actor, r.target || "", r.result]))); };
    sec("Commands refused by the guard", d.commands_refused); sec("Unexpected commands sent to a person", d.commands_needing_decision);
    sec("Tools refused (not on the agent's list)", d.tools_refused); sec("Issues blocked by the injection screen", d.issues_blocked);
    if (d.guardrail_measurement) { const g = d.guardrail_measurement; m.appendChild(el("h2", "Injection screen, measured on labelled issues"));
      m.appendChild(table(["Measure", "Result"], [["Attacks blocked", g.detected + " of " + g.malicious], ["Harmless issues wrongly blocked", g.false_alarms + " of " + g.benign],
        ["Screen did not answer (issue held back)", num(g.failed_closed)], ["Typical time per issue (median)", num(Math.round(g.p50_ms)) + " ms"],
        ["Attacks not blocked by the screen", (g.missed || []).join(", ") || "none"], ["Harmless issues blocked", (g.false_alarm_ids || []).join(", ") || "none"],
        ["Measured", when(g.measured_at)]]));
      m.appendChild(el("p", "Labelled issues are in evals/issues.yaml. An attack can be removed by the sanitiser before the screen sees it.", "muted")); }
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
    m.appendChild(el("h2", "Lessons"));
    m.appendChild(el("p", "Written when a PR is merged (and from failures or rejections); recalled for later runs on the same repository.", "muted"));
    const all = Object.entries(d.lessons).flatMap(([repo, ls]) => ls.map(l => [repo.split("/").pop(), (l.task_type || "").replace(/_/g, " "), l.source, l.text]));
    m.appendChild(table(["Repo", "Type", "Source", "Lesson"], all));
    m.appendChild(el("h2", "Recent sessions"));
    const note = el("p", "Each summary is a snapshot taken when that session ended. For each run's current status, see ", "muted");
    const toRuns = el("a", "Runs"); toRuns.href = "#runs"; note.appendChild(toRuns); note.appendChild(document.createTextNode(".")); m.appendChild(note);
    const sessions = d.sessions.filter(s => s.summary && s.summary !== "no runs");
    m.appendChild(table(["Started", "Summary (at session end)"], sessions.map(s => [when(s.started), tdTitle(short(friendlySummary(s.summary), 120), friendlySummary(s.summary))])));
  },
  async skills(m) {
    const d = await api("skills");
    m.appendChild(table(["Skill", "Task type", "Workflow", "Must not"], d.brief_skills.map(s => [s.display_name, s.task_type.replace(/_/g, " "), s.workflow_steps.join(" → "), s.forbidden_actions.join("; ")])));
    for (const [repo, versions] of Object.entries(d.repo_skills || {})) { m.appendChild(el("h2", "Repo skill — " + repo));
      m.appendChild(table(["Version", "When", "Change"], versions.map(v => [v.version, when(v.created_at), td(textWithCode(v.change))]))); }
  },
  async audit(m) {
    const d = await api("audit");
    m.appendChild(table(["Run", "#", "When", "Who", "Action", "Target", "Result"], d.events.map(e => [td(runLink(e.run_id)), e.id, when(e.ts),
      e.actor === "human" ? "human (TUI)" : e.actor, e.action.replace(/_/g, " "), td(textWithLinks(e.target || "")), e.result])));
  },
  async improvements(m) {
    const d = await api("improvements");
    m.appendChild(el("h2", "Proposed skill changes waiting for a decision"));
    m.appendChild(el("p", "Seen: how many runs proposed this change (rewordings of the same change are merged), out of the runs on that repository since it first appeared. Confidence is the lower bound of the Wilson score interval: it rises only when a change keeps coming back across enough runs. Nothing changes until you approve.", "muted"));
    m.appendChild(table(["Repo", "Change", "Evidence", "Seen", "Confidence", "Decide"], d.proposals.map(p => {
      const c = el("td"); ["approve", "reject"].forEach(dec => { const b = el("button", dec, dec === "reject" ? "danger" : ""); b.addEventListener("click", async () => {
        if (!confirm(dec[0].toUpperCase() + dec.slice(1) + " this change to " + p.repo + "'s repo skill?\n\n" + p.change)) return;
        try { await api("improvements/" + p.id + "/" + dec, { confirm: true }); show("improvements"); } catch (e) { alert(e.message); } }); c.appendChild(b); });
      return [p.repo, td(textWithCode(p.change)), td(textWithCode(p.evidence)), p.seen === undefined ? "" : p.seen + " of " + p.runs + " runs", pct(p.confidence), c]; })));
    m.appendChild(el("h2", "How runs went per repo-skill version"));
    m.appendChild(el("p", "Failed commands per run: shell commands that ran and exited with an error (commands the guard refused are listed on Safety, not here). Tests passed on first try: share of runs whose first verification test run passed — a run can still fail later for another reason.", "muted"));
    m.appendChild(table(["Repo", "Version", "Runs", "Failed commands per run", "Tool calls per run", "Tests passed on first try"],
      d.trend.map(t => [t.repo, t.version === 0 ? "none (ended before the skill loaded)" : "v" + t.version, num(t.runs),
        t.failed_commands ?? "—", t.tool_calls ?? "—", t.first_try_pass === null ? "—" : pct(t.first_try_pass)])));
  },
};

function setActive(page) { document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.page === page)); }
function loading(m) { clear(m); m.appendChild(el("p", "Loading…", "muted loading")); }

async function showRun(id) {
  setActive(""); const m = document.getElementById("main"); loading(m);
  try {
    const d = await api("runs/" + encodeURIComponent(id)); clear(m);
    const back = el("a", "← Back to runs", "back"); back.href = "#runs"; m.appendChild(back);
    m.appendChild(el("h2", d.run.repo + " " + d.run.issue_key + " — " + d.run.title));
    m.appendChild(el("p", "Now: " + status(d.run.state) + " · " + (d.run.task_type || "").replace(/_/g, " ") + " · complexity " + (d.run.complexity ?? "") + "/10"));
    const why = [d.run.reason || ""].concat((d.errors || []).map(x => "Full error (end of output):\n" + x)).filter(Boolean).join("\n\n");
    if (why) m.appendChild(details("Why it ended this way", why));
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
  if (location.hash !== "#" + page) { location.hash = page; return; }          // hashchange renders it (Back button works)
  setActive(page);
  const m = document.getElementById("main"); clear(m);
  const info = PAGES.find(p => p[0] === page);
  m.appendChild(el("p", info[2] + " Times are in your local time.", "page-intro"));
  const wait = el("p", "Loading…", "muted loading"); m.appendChild(wait);
  const holder = el("div");
  try { await render[page](holder); wait.remove(); m.appendChild(holder); }
  catch (e) { wait.remove(); m.appendChild(el("p", "Could not load: " + e.message)); }
}

const ALIASES = { matrix: "runs" };      // old links (#matrix) still open the Runs page
function route() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("run")) return showRun(h.get("run"));
  const page = ALIASES[location.hash.slice(1)] || location.hash.slice(1);
  return show(PAGES.some(p => p[0] === page) ? page : "overview");
}

document.addEventListener("DOMContentLoaded", () => {
  const nav = document.getElementById("nav");
  PAGES.forEach(([p, label]) => { const b = el("button", label); b.dataset.page = p; b.addEventListener("click", () => { location.hash = p; }); nav.appendChild(b); });
  window.addEventListener("hashchange", route);
  route();
});
