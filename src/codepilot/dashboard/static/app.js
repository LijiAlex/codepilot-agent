"use strict";
// Every value from GitHub or an agent is set with textContent: no HTML is ever built from data.
const PAGES = [
  ["overview", "Overview", "Totals across every run CodePilot+ has done."],
  ["runs", "Runs", "Every run and the stages it reached. Click a run ID for its diff, log and analysis."],
  ["prs", "Pull requests", "Open pull requests with the independent review. Merge needs a current review that says merge-ready."],
  ["safety", "Safety", "What the guardrails refused or held back, and the measured injection screen."],
  ["context", "Context", "How much context runs were given: the repo map per repository against its budget, and the lessons each run recalled."],
  ["memory", "Memory", "Lessons learned from past work, and a summary of each recent session."],
  ["skills", "Skills", "The five task skills and each repository's own skill (its versions)."],
  ["audit", "Audit log", "Every action, by whom, in order."],
  ["improvements", "Improvements", "Proposed changes to a repository's skill, waiting for a person, and how runs went per skill version."],
];
const STATUS = { PR_OPENED: "PR opened", AWAITING_APPROVAL: "Waiting for approval", FAILED: "Failed", DONE: "Merged",   // one casing style: sentence case
  CLOSED: "Closed", SKIPPED: "Skipped", EXPLORING: "Exploring", IMPLEMENTING: "Implementing", TESTING: "Testing", TRIAGED: "Triaged" };
let token = "";
try { token = sessionStorage.getItem("cp-token") || ""; } catch (e) { /* storage blocked: token from the link only */ }
const hash = new URLSearchParams(location.hash.slice(1));
if (hash.get("token")) { token = hash.get("token"); try { sessionStorage.setItem("cp-token", token); } catch (e) {} history.replaceState(null, "", "/"); }

function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; }
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function num(n) { return n === null || n === undefined || n === "" ? "" : Number(n).toLocaleString(); }
function status(s) { const t = STATUS[s] || String(s || "").toLowerCase().replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1); }
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
const MARKS = { done: ["✓", ""], failed: ["✗", ""], voided: ["!", "Tests passed, but a later change was not re-tested, so the run stopped as untested"],
  stopped: ["✗", "The run stopped at this stage"], unreached: ["–", "Never reached: the run stopped earlier"] };
function markCell(mark, extra) { const [sym, why] = MARKS[mark] || ["", ""], c = el("td", sym); if (mark) c.className = mark; if (why) c.title = why + (extra ? ": " + extra : ""); return c; }
function stageMarks(r, stages) {        // failed runs: the stage it stopped at (✗) vs the stages it never reached (–)
  const marks = stages.map(s => r.marks[s] || "");
  if (!["FAILED", "CLOSED"].includes(r.state)) return marks;
  let stop = marks.findIndex(m => m === "failed" || m === "voided");
  if (stop < 0) { stop = marks.findIndex(m => m !== "done"); if (stop >= 0 && r.state === "FAILED") marks[stop] = "stopped"; }
  return marks.map((m, i) => stop >= 0 && i > stop && !m ? "unreached" : m);
}
function plural(n, word) { return num(n) + " " + word + (n === 1 ? "" : "s"); }
function words(s) { return String(s || "").replace(/_skill$/, "").replace(/_/g, " "); }       // bug_fix_skill -> "bug fix"
function utc(v) { return /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(v || "") ? new Date(v.replace(" ", "T") + "Z") : null; }
function duration(start, end) {
  const a = utc(start), b = utc(end); if (!a || !b) return "";
  const sec = Math.max(0, Math.round((b - a) / 1000)), m = Math.floor(sec / 60);
  return m >= 60 ? Math.floor(m / 60) + " h " + (m % 60) + " min" : m ? m + " min " + (sec % 60) + " s" : sec + " s";
}
function actorName(a) {                 // display only; stored actors stay as they are
  if (a === "human") return "person (TUI)";
  if (a === "human:dashboard") return "person (dashboard)";
  if (/^human:/.test(a || "")) return "person (TUI: " + a.slice(6) + ")";
  if (/^agent:/.test(a || "")) return "agent: " + a.slice(6);
  return a || "";
}
const RESULTS = { "": "ok", ok: "ok", refused: "refused", error: "error", paused: "waiting for a person" };
function resultWord(r) { return RESULTS[r || ""] || (/^\d+$/.test(r) ? "exit code " + r : String(r)); }
function cardLink(label, value, page) { const c = el(page ? "a" : "div", null, "card"); if (page) c.href = "#" + page; c.appendChild(el("b", value)); c.appendChild(el("span", label, "muted")); return c; }
function filterInput(placeholder, onChange) { const i = el("input", null, "filter"); i.type = "search"; i.placeholder = placeholder; i.addEventListener("input", () => onChange(i.value.trim().toLowerCase())); return i; }
const RUBRIC = [["correctness", "C"], ["tests", "T"], ["matches the issue", "M"], ["risk", "R"], ["scope", "S"]];
function rubricCell(f) {                // "C2 T1 M2 R2 S1"; the reviewer's reason for each on hover
  const r = f.rubric || {}, why = f.rubric_reasons || {};
  if (!Object.keys(r).length) return el("td", "—", "muted");
  const c = el("td", RUBRIC.filter(([k]) => k in r).map(([k, a]) => a + r[k]).join(" "), "rubric");
  c.title = RUBRIC.filter(([k]) => k in r).map(([k]) => k + " " + r[k] + "/2" + (why[k] ? ": " + why[k] : "")).join("\n");
  return c;
}
function details(summary, body) { const d = el("details"), s = el("summary", summary); d.appendChild(s); d.appendChild(el("pre", body)); return d; }
function diffBlock(diff) {
  const pre = el("pre", null, "diff");
  String(diff).split("\n").forEach(line => { const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : ""; pre.appendChild(el("span", line + "\n", cls)); });
  return pre;
}

const RULE_HELP = {   // shell-guard rules (src/codepilot/safety/commands.py rule_for)
  "inline-python": "python -c \"…\": one-line scripts can do anything; use read_file or a test file",
  "pip-install": "pip / uv / python -m pip|venv: agents never install; CodePilot+ installs from the dependency file",
  "network": "curl, wget, ssh, gh, http.server: no network from the sandbox",
  "git-remote": "git push / pull / fetch / remote / clone / config: GitHub is reached only by the publish step after approval",
  "git-option": "git -C or --output: hides where git acts or writes",
  "delete-files": "rm, find -delete / -exec: delete or run files only through the file tools",
  "outside-sandbox": "absolute paths or ../: the agent works inside sandbox/ only",
  "secret-file": ".env, *.pem, *.key, credentials…: never read or written",
  "command-substitution": "$( ) and backticks: hide what actually runs",
  "redirection": "> and <: writes files around the file tools",
  "multi-line": "newlines: several commands hidden in one call",
  "background": "a lone &: starts something that keeps running",
  "blocked-tool": "sudo, chmod, env, sed, awk, perl, node, kill…: never needed in the sandbox",
  "unknown-command": "not on the allowed or blocked list: a person decides (unexpected-command gate)",
  "unparseable": "quotes that do not balance",
};

const render = {
  async overview(m) {
    const [d, audit] = await Promise.all([api("overview"), api("audit").catch(() => ({ events: [] }))]);
        const cards = el("div", null, "cards");
    const st = d.by_state || {};
    const iss = d.issues || {}, judged = (iss.issues || 0) - (iss.infrastructure || 0);
    [["Issues solved (latest run per issue)", iss.rate === null || iss.rate === undefined ? "—" : Math.round(100 * iss.rate) + "% (" + iss.solved + " of " + judged + ")", "runs"],
     ["Solved on the first attempt", iss.solved ? iss.first_attempt + " of " + iss.solved : "—", "runs"],
     ["Runs", num(d.runs), "runs"],
     ["PRs opened", num(d.prs_opened), "prs"], ["Runs waiting for approval (TUI)", num(d.pending_decisions), "runs"],
     ["Skill changes waiting", num(d.proposals_waiting), "improvements"], ["Commands refused", num(d.commands_refused), "safety"],
     ["Issues blocked", num(d.issues_blocked), "safety"], ["Tokens in / out", num(d.tokens_in) + " / " + num(d.tokens_out), ""],
     ["Cost (USD)" + (d.cost_source ? " — " + d.cost_source : ""), d.cost_usd === null ? "not tracked" : "$" + d.cost_usd, ""]]
      .forEach(([k, v, page]) => cards.appendChild(cardLink(k, v, page)));
    m.appendChild(cards);
    if (d.issues) m.appendChild(el("p", "Per issue (its latest run): " + iss.solved + " solved (merged, PR opened, or verified and waiting for your approval) · " + iss.declined + " declined (the agents explained why they changed nothing) · " + iss.failed + " failed · " + iss.open + " still open · " + iss.infrastructure + " stopped by infrastructure (sandbox, crash — not counted in the rate).", "muted"));
    m.appendChild(el("p", "Outcomes: " + (Object.entries(st).map(([k, v]) => status(k) + " ×" + v).join(", ") || "no runs yet"), "muted"));
    if (d.usage) {                                     // from LangSmith traces
      const sum = (o) => Object.values(o).reduce((a, u) => ({ cost: a.cost + (u.cost || 0), tokens_in: a.tokens_in + (u.tokens_in || 0), tokens_out: a.tokens_out + (u.tokens_out || 0) }), { cost: 0, tokens_in: 0, tokens_out: 0 });
      const rv = d.usage.reviewer || {}, sc = d.usage.screen || {};
      const im = d.usage.improvement || {};
      const rows = [["Solver agents — Orchestrator, Explorer, Coder, Test Agent, PR Agent (incl. triage)", plural(Object.keys(d.usage.runs || {}).length, "run"), sum(d.usage.runs || {})],
        ["Injection screen (NeMo, Haiku)", plural(sc.count || 0, "screen"), sc],
        ["Self-improvement — skill gap reports", plural(im.count || 0, "report"), im],
        ["Independent reviewer (GitHub Action)", plural(rv.count || 0, "PR review"), rv]];
      m.appendChild(el("h2", "Where the tokens and cost go (from LangSmith traces)"));
      m.appendChild(table(["Part", "Count", "Tokens in", "Tokens out", "Cost (USD)"], rows.map(([k, n, u]) => [k, n, num(u.tokens_in || 0), num(u.tokens_out || 0), "$" + (u.cost || 0).toFixed(3)])));
    }
    if (d.cost_usd === null) m.appendChild(el("p", "Cost is not tracked: set LANGSMITH_API_KEY (LangSmith prices every model call) or add price_in_per_mtok and price_out_per_mtok to codepilot.yml.", "muted"));
    if (d.skills) {
      const h = el("h2", "Skills at a glance "), toSk = el("a", "Skills"), toImp = el("a", "Improvements");
      toSk.href = "#skills"; toImp.href = "#improvements";
      const sub = el("span", "— see ", "muted small"); sub.appendChild(toSk); sub.appendChild(document.createTextNode(" and ")); sub.appendChild(toImp); h.appendChild(sub);
      m.appendChild(h);
      m.appendChild(table(["Task skill", "Runs", "Outcomes"], d.skills.by_task.map(t => [words(t.task_type), num(t.runs),
        Object.entries(t.outcomes).map(([k, v]) => status(k) + " ×" + v).join(", ")])));
      m.appendChild(table(["Repository", "Repo skill version", "Changes waiting", "Changes approved"], d.skills.repos.map(r => [r.repo,
        r.version ? "v" + r.version : "none yet", num(r.waiting), num(r.approved)])));
    }
    m.appendChild(el("h2", "Recent activity"));
    m.appendChild(table(["When", "Run", "Who", "Action", "Target"], audit.events.slice(0, 10).map(e => [when(e.ts), td(runLink(e.linked_run || e.run_id)),
      actorName(e.actor), words(e.action), td(textWithLinks(e.target || ""))])));
    const all = el("a", "Full audit log"); all.href = "#audit"; m.appendChild(all);
  },
  async runs(m) {
    const d = await api("matrix");
    const seen = {}, groups = {};
    const filter = filterInput("Filter runs (repo, issue, outcome, type, title…)", q => apply(q));
    m.appendChild(filter);
    const t = table(["Run", "Issue", "Started", "Duration", "Outcome", "Cost", "Title", "Type", ...d.stages], []);
    const tbl = t.querySelector("table"); tbl.removeChild(tbl.lastChild);           // drop the "None so far" row; rows follow
    d.rows.forEach(r => {
      const key = r.repo + " " + r.issue, older = key in seen; seen[key] = (seen[key] || 0) + 1;
      const outcome = r.pr_url ? td(link(status(r.state), r.pr_url)) : r.reason && r.reason.length > 50
        ? td(details(status(r.state) + " — " + short(r.reason, 50), r.reason)) : el("td", status(r.state) + (r.reason ? " — " + r.reason : ""));
      const issue = el("td", r.repo.split("/").pop() + " " + r.issue);
      const marks = stageMarks(r, d.stages);
      const cells = [td(runLink(r.run_id)), issue, when(r.started), r.ended ? duration(r.started, r.ended) : ["TRIAGED", "EXPLORING", "IMPLEMENTING", "TESTING"].includes(r.state) ? "running" : "—", outcome,
        r.cost_usd === null || r.cost_usd === undefined ? "" : "$" + Number(r.cost_usd).toFixed(3), el("td", r.title, "title"), words(r.type),
        ...marks.map(mk => markCell(mk, mk === "stopped" ? r.reason : ""))];
      const tr = el("tr"); cells.forEach(c => tr.appendChild(c instanceof Node ? c : el("td", c)));
      tr.dataset.text = [r.run_id, key, status(r.state), r.reason, r.type, r.title].join(" ").toLowerCase();
      if (older) { tr.classList.add("older"); groups[key].rows.push(tr); }
      else groups[key] = { rows: [], issue, open: false };
      tbl.appendChild(tr);
    });
    Object.values(groups).filter(g => g.rows.length).forEach(g => {           // retries: latest row first, older attempts on demand
      const b = el("button", "+" + g.rows.length + " older", "small"); b.title = "Show earlier attempts at this issue";
      b.addEventListener("click", () => { g.open = !g.open; b.textContent = (g.open ? "−" : "+") + g.rows.length + " older"; apply(filter.value.trim().toLowerCase()); });
      g.issue.appendChild(document.createTextNode(" ")); g.issue.appendChild(b);
    });
    function apply(q) {
      Object.values(groups).forEach(g => g.rows.forEach(tr => { tr.hidden = !(g.open || q) || (q && !tr.dataset.text.includes(q)); }));
      tbl.querySelectorAll("tr:not(.older)").forEach((tr, i) => { if (i) tr.hidden = !!q && !tr.dataset.text.includes(q); });
    }
    if (!d.rows.length) m.appendChild(el("p", "None so far", "muted"));
    apply(""); m.appendChild(t);
  },
  async prs(m) {
    const d = await api("prs");
    for (const [repo, prsIn] of Object.entries(d)) {
      m.appendChild(el("h2", repo));
      if (prsIn.error) { m.appendChild(el("p", "Could not load: " + prsIn.error, "muted")); continue; }
      if (!prsIn.length) { m.appendChild(el("p", "No open pull requests in this repository.", "muted")); continue; }
      const prs = [...prsIn].sort((a, b) => (b.attention || []).length - (a.attention || []).length);
      m.appendChild(table(["", "PR", "Run", "Score", "Rubric", "Effort", "Merge-ready", "Matches the issue", "Security", "Reviews", "Review cost", "Actions", "Review", "Labels", "Author"], prs.map(p => {
        const f = (p.review && p.review.fields) || {}, u = p.review_usage || {};
        const att = el("td", null, (p.attention || []).length ? "attention" : "");
        if ((p.attention || []).length) { att.appendChild(el("span", "⚠ ", "warn")); att.appendChild(el("span", p.attention.join(", "))); att.title = "A person should look at this PR"; }
        const actions = el("td", null, "actions");
        const mk = (label, action, cls, question) => { const b = el("button", label, cls); b.addEventListener("click", async () => {
          if (!confirm(question)) return;
          try { await api("prs/" + repo + "/" + p.number + "/" + action, { expected_head: p.head_sha, confirm: true }); show("prs"); }
          catch (e) { alert(e.message); } }); return b; };
        const why = !p.review ? "needs an independent review" : !p.review.current ? "review is for an older commit" : !p.merge_ready || f.merge_ready === false ? "review does not say merge-ready" : "";
        const merge = mk("Merge", "merge", "", "Squash-merge " + repo + "#" + p.number + " at " + p.head_sha.slice(0, 7) + " into the default branch?");
        if (why) { merge.disabled = true; merge.title = why; }
        actions.appendChild(merge);
        const close = mk("Close", "close", "danger", "Close " + repo + "#" + p.number + " without merging? (It can be reopened on GitHub.)");
        close.title = "Close without merging (asks first)"; actions.appendChild(close);
        if (why) actions.appendChild(el("div", "Merge: " + why, "muted small"));
        const ready = f.merge_ready === true ? "yes" : f.merge_ready === false ? "no" + (f.why_not ? " — " + f.why_not : "") : "";
        const rev = el("td");
        if (p.review) { rev.appendChild(details(p.review.current ? "full review" : "full review (older commit)", p.review.body)); } else rev.appendChild(el("span", "not reviewed yet", "muted"));
        return [att, td(link("#" + p.number + " " + p.title, p.url)), p.run_id ? td(runLink(p.run_id)) : el("td", "not from CodePilot+", "muted"),
          f.score === null || f.score === undefined ? "" : tdTitle(f.score + "/10" + (f.cap ? " (capped)" : ""), f.cap), rubricCell(f),
          f.effort === null || f.effort === undefined ? "" : f.effort + "/5", ready, f.matches || "",
          (f.security || []).length ? el("td", f.security.join("; "), "failed") : "none",
          u.count ? num(u.count) : "—", u.count ? "$" + Number(u.cost || 0).toFixed(3) : "—", actions, rev, p.labels.join(", "),
          p.author + (p.from_fork ? " (fork)" : "")];
      })));
    }
  },
  async safety(m) {
    const d = await api("safety");
    const reason = (r) => { try { return JSON.parse(r.detail_json || "{}").reason || ""; } catch (e) { return ""; } };
    const reasons = (r) => { try { return (JSON.parse(r.detail_json || "{}").reasons || []).join(", "); } catch (e) { return ""; } };
    m.appendChild(el("h2", "Commands refused by the guard"));
    m.appendChild(el("p", "Commands that differ only in spacing are grouped. Every one of them was refused (nothing ran).", "muted"));
    const grouped = {};
    d.commands_refused.forEach(r => { const k = String(r.target || "").trim().replace(/\s+/g, " ");
      const g = grouped[k] = grouped[k] || { cmd: k, last: r.ts, runs: [], who: new Set(), n: 0, why: reason(r), guard: r.guard || "" };
      g.n += 1; g.who.add(actorName(r.actor)); if (r.run_id && !g.runs.includes(r.run_id)) g.runs.push(r.run_id); });
    m.appendChild(table(["Last seen", "Runs", "Who", "Command", "Times", "Guard", "Why"], Object.values(grouped).map(g => {
      const runs = el("td"); g.runs.forEach((id, i) => { if (i) runs.appendChild(document.createTextNode(", ")); runs.appendChild(runLink(id)); });
      return [when(g.last), runs, [...g.who].join(", "), td(el("code", g.cmd)), num(g.n), g.guard, g.why]; })));
    const legend = el("details"); legend.appendChild(el("summary", "What each shell-guard rule means"));
    legend.appendChild(table(["Rule", "Blocks"], Object.entries(RULE_HELP).map(([k, v]) => [k, v]))); m.appendChild(legend);
    const sec = (title, rows) => { m.appendChild(el("h2", title)); m.appendChild(table(["When", "Run", "Who", "What", "Guard", "Why"], rows.map(r => [when(r.ts), td(runLink(r.run_id)), actorName(r.actor), r.target || "", r.guard || "", reason(r) || reasons(r)]))); };
    sec("Unexpected commands sent to a person", d.commands_needing_decision);
    sec("Tools refused (not on the agent's list)", d.tools_refused); sec("Issues blocked by the injection screen", d.issues_blocked);
    if (d.guardrail_measurement) { const g = d.guardrail_measurement; m.appendChild(el("h2", "Injection screen, measured on labelled issues"));
      m.appendChild(table(["Measure", "Result"], [["Attacks blocked", g.detected + " of " + g.malicious], ["Harmless issues wrongly blocked", g.false_alarms + " of " + g.benign],
        ["Screen did not answer (issue held back)", num(g.failed_closed)], ["Typical time per issue (median)", num(Math.round(g.p50_ms)) + " ms"],
        ["Attacks not blocked by the screen", (g.missed || []).join(", ") || "none"], ["Harmless issues blocked", (g.false_alarm_ids || []).join(", ") || "none"],
        ["Measured", when(g.measured_at)]]));
      m.appendChild(el("p", "Labelled issues are in evals/issues.yaml. An attack can be removed by the sanitiser before the screen sees it.", "muted")); }
  },
  async context(m) {
    const d = await api("context"), budget = d.budget || 4000, perRepo = {};
    d.runs.forEach(r => { const c = r.context || {}; if (c.map_tokens === undefined) return;
      const p = perRepo[r.repo] = perRepo[r.repo] || { latest: c, min: c.map_tokens, max: c.map_tokens, over: 0 };   // runs are newest first
      p.min = Math.min(p.min, c.map_tokens); p.max = Math.max(p.max, c.map_tokens); if (c.map_tokens > budget) p.over += 1; });
    m.appendChild(el("h2", "Repo map per repository (budget " + num(budget) + " tokens)"));
    m.appendChild(table(["Repository", "Repo map tokens (latest run)", "Range across runs", "Files in map", "Runs over budget"], Object.entries(perRepo).map(([repo, p]) => [repo,
      num(p.latest.map_tokens), p.min === p.max ? "same on every run" : num(p.min) + " – " + num(p.max), num(p.latest.map_files),
      p.over ? el("td", num(p.over), "failed") : "0"])));
    m.appendChild(el("h2", "Per run"));
    m.appendChild(table(["Run", "Issue", "Lessons recalled", "Tokens in / out"], d.runs.map(r => {      // map size is per repository, above
      const c = r.context || {};
      const lessons = c.map_tokens === undefined ? el("td", "no context recorded (ended before it was built)", "muted") : num(c.lessons);
      return [td(runLink(r.run_id)), r.repo.split("/").pop() + " " + r.issue, lessons, num(r.tokens_in) + " / " + num(r.tokens_out)]; })));
  },
  async memory(m) {
    const d = await api("memory");
    m.appendChild(el("h2", "Lessons"));
    m.appendChild(el("p", "Written when a PR is merged (and from failures or rejections); recalled for later runs on the same repository.", "muted"));
    const all = Object.entries(d.lessons).flatMap(([repo, ls]) => ls.map(l => [repo.split("/").pop(), words(l.task_type), l.source, td(runLink(l.run_id)), l.text]));
    m.appendChild(table(["Repo", "Type", "Source", "From run", "Lesson"], all));
    const sessions = d.sessions.filter(s => s.summary && s.summary !== "no runs");
    const box = el("details"); box.appendChild(el("summary", "Recent sessions (" + sessions.length + ")"));
    const note = el("p", "Each summary is a snapshot taken when that session ended. For each run's current status, see ", "muted");
    const toRuns = el("a", "Runs"); toRuns.href = "#runs"; note.appendChild(toRuns); note.appendChild(document.createTextNode(".")); box.appendChild(note);
    box.appendChild(table(["Started", "Summary (at session end)"], sessions.map(s => [when(s.started), tdTitle(short(friendlySummary(s.summary), 120), friendlySummary(s.summary))])));
    m.appendChild(box);
  },
  async skills(m) {
    const d = await api("skills");
    m.appendChild(table(["Task skill", "Workflow", "Must not"], d.brief_skills.map(s => [words(s.task_type), s.workflow_steps.map(words).join(" → "), s.forbidden_actions.join("; ")])));
    for (const [repo, versions] of Object.entries(d.repo_skills || {})) { m.appendChild(el("h2", "Repo skill — " + repo));
      m.appendChild(table(["Version", "When", "Change"], versions.map(v => ["v" + v.version, when(v.created_at), td(textWithCode(v.change))]))); }
  },
  async audit(m) {
    const d = await api("audit"), PAGE = 100;
    let page = 0, q = "", hideGuard = false;
    const bar = el("div", null, "toolbar");
    bar.appendChild(filterInput("Filter by who, action, target or run…", v => { q = v; page = 0; draw(); }));
    const lab = el("label", " Hide guard refusals (they are listed on Safety)"), cb = el("input"); cb.type = "checkbox";
    cb.addEventListener("change", () => { hideGuard = cb.checked; page = 0; draw(); }); lab.prepend(cb); bar.appendChild(lab);
    m.appendChild(bar);
    const holder = el("div"); m.appendChild(holder);
    function draw() {
      const rows = d.events.filter(e => (!hideGuard || !/_refused$|_needs_decision$/.test(e.action)) &&
        (!q || [actorName(e.actor), words(e.action), e.target, e.linked_run, resultWord(e.result)].join(" ").toLowerCase().includes(q)));
      const pages = Math.max(1, Math.ceil(rows.length / PAGE)); page = Math.min(page, pages - 1);
      clear(holder);
      holder.appendChild(table(["Run", "#", "When", "Who", "Action", "Target", "Result"], rows.slice(page * PAGE, (page + 1) * PAGE).map(e => [td(runLink(e.linked_run)), e.id, when(e.ts),
        actorName(e.actor), words(e.action), td(textWithLinks(e.target || "")), resultWord(e.result)])));
      const nav = el("div", null, "pager"), prev = el("button", "← Newer"), next = el("button", "Older →");
      prev.disabled = page === 0; next.disabled = page >= pages - 1;
      prev.addEventListener("click", () => { page -= 1; draw(); }); next.addEventListener("click", () => { page += 1; draw(); });
      nav.appendChild(prev); nav.appendChild(el("span", " Page " + (page + 1) + " of " + pages + " (" + num(rows.length) + " events) ", "muted")); nav.appendChild(next);
      holder.appendChild(nav);
    }
    draw();
  },
  async improvements(m) {
    const [d, audit] = await Promise.all([api("improvements"), api("audit").catch(() => ({ events: [] }))]);
    m.appendChild(el("h2", "Proposed skill changes waiting for a decision"));
    m.appendChild(el("p", "Seen: how many runs proposed this change (rewordings of the same change are merged), out of the runs on that repository since it first appeared. Confidence is the lower bound of the Wilson score interval: it rises only when a change keeps coming back across enough runs. Nothing changes until you approve.", "muted"));
    m.appendChild(table(["Repo", "Change", "Evidence", "Seen", "Confidence", "Decide"], d.proposals.map(p => {
      const c = el("td"); ["approve", "reject"].forEach(dec => { const b = el("button", dec, dec === "reject" ? "danger" : ""); b.addEventListener("click", async () => {
        if (!confirm(dec[0].toUpperCase() + dec.slice(1) + " this change to " + p.repo + "'s repo skill?\n\n" + p.change)) return;
        try { await api("improvements/" + p.id + "/" + dec, { confirm: true }); show("improvements"); } catch (e) { alert(e.message); } }); c.appendChild(b); });
      const ev = el("details"); ev.appendChild(el("summary", "evidence")); ev.appendChild(textWithCode(p.evidence));     // collapsed: refused commands are on Safety
      return [p.repo, td(textWithCode(p.change)), td(ev), p.seen === undefined ? "" : p.seen + " of " + p.runs + " runs", pct(p.confidence), c]; })));
    const decided = audit.events.filter(e => /^improvement_(approve|reject)$/.test(e.action));
    m.appendChild(el("h2", "Past decisions"));
    m.appendChild(table(["When", "Who", "Decision", "Proposal", "Result"], decided.map(e => { let det = {}; try { det = JSON.parse(e.detail_json || "{}"); } catch (x) { /* plain text */ }
      return [when(e.ts), actorName(e.actor), e.action.replace("improvement_", "") + "d", e.target || "", det.version ? "repo skill v" + det.version : det.status || ""]; })));
    m.appendChild(el("h2", "How runs went per repo-skill version"));
    m.appendChild(el("p", "Failed commands per run: shell commands that ran and exited with an error (commands the guard refused are listed on Safety, not here). Tests passed on first try: share of runs whose first verification test run passed — a run can still fail later for another reason.", "muted"));
    m.appendChild(table(["Repo", "Version", "Runs", "Failed commands per run", "Tool calls per run", "Tests passed on first try"],
      d.trend.map(t => [t.repo, t.version === 0 ? "none (ended before the skill loaded)" : "v" + t.version, num(t.runs),
        t.failed_commands ?? "—", t.tool_calls ?? "—", t.first_try_pass === null ? "—" : pct(t.first_try_pass)])));
  },
};

function setActive(page) { document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.page === page)); }
function loading(m) { clear(m); m.appendChild(el("p", "Loading…", "muted loading")); }

function logRows(events) {              // a sub-agent's final message comes back as the orchestrator's tool result: show it once
  const out = [];
  events.forEach(e => {
    const prev = out[out.length - 1];
    const echo = prev && e.kind === "tool_result" && prev.kind === "message" &&      // the tool result is stored cut at 300 characters
      (prev.text === e.text || (e.text.length >= 40 && String(prev.text).startsWith(String(e.text).replace(/…$/, ""))));
    if (echo) { prev.agent = prev.agent + " → " + e.agent; return; }
    out.push(Object.assign({}, e));
  });
  return out;
}

async function showRun(id) {
  setActive(""); const m = document.getElementById("main"); loading(m);
  try {
    const d = await api("runs/" + encodeURIComponent(id)); clear(m);
    const back = el("a", "← Back to runs", "back"); back.href = "#runs"; m.appendChild(back);
    m.appendChild(el("h2", d.run.repo + " " + d.run.issue_key + " — " + d.run.title));
    if (d.run.pr_url) { const p = el("p"); p.appendChild(link("Pull request: " + d.run.pr_url, d.run.pr_url)); m.appendChild(p); }
    const facts = ["Now: " + status(d.run.state), words(d.run.task_type), "complexity " + (d.run.complexity ?? "") + "/10", "started " + when(d.run.created_at)];
    if (duration(d.run.created_at, d.run.ended_at)) facts.push("took " + duration(d.run.created_at, d.run.ended_at));
    if (d.cost_usd !== null && d.cost_usd !== undefined) facts.push("cost $" + Number(d.cost_usd).toFixed(3));
    m.appendChild(el("p", facts.filter(Boolean).join(" · ")));
    if (d.handoffs.some(h => h.payload.diff)) { const j = el("a", "Jump to the diff"); j.href = "#run=" + encodeURIComponent(id); j.addEventListener("click", ev => { ev.preventDefault(); document.getElementById("diff").scrollIntoView(); }); m.appendChild(j); }
    const why = [d.run.reason || ""].concat((d.errors || []).map(x => "Full error (end of output):\n" + x)).filter(Boolean).join("\n\n");
    if (why) m.appendChild(details("Why it ended this way", why));
    if (d.context) m.appendChild(el("p", `Context: repo map ${num(d.context.map_tokens)} tokens, ${d.context.lessons} lesson(s), ${words(d.context.skill)}`, "muted"));
    const diffs = el("div"); diffs.id = "diff"; m.appendChild(diffs);
    d.handoffs.forEach(h => { diffs.appendChild(el("h3", words(h.entry_point) + " — " + h.status)); if (h.payload.diff) diffs.appendChild(diffBlock(h.payload.diff)); });
    if (d.skill_gap_report) m.appendChild(details("Skill gap report (written when the run ended)", JSON.stringify(d.skill_gap_report, null, 2)));
    if (d.trace_analysis) m.appendChild(details("Trace analysis (state when the run ended)", JSON.stringify(d.trace_analysis, null, 2)));
    m.appendChild(el("h3", "Agent log"));
    m.appendChild(table(["Time", "Agent", "Kind", "Text"], logRows(d.events).map(e => [e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : "", e.agent, words(e.kind), e.text])));
  } catch (e) { clear(m); m.appendChild(el("p", "Could not load the run: " + e.message)); }
}

async function show(page) {
  if (location.hash !== "#" + page) { location.hash = page; return; }          // hashchange renders it (Back button works)
  setActive(page);
  const m = document.getElementById("main"); clear(m);
  const info = PAGES.find(p => p[0] === page);
  m.appendChild(el("p", info[2], "page-intro"));
  const wait = el("p", "Loading…", "muted loading"); m.appendChild(wait);
  const holder = el("div");
  try { await render[page](holder); wait.remove(); m.appendChild(holder); }
  catch (e) { wait.remove(); m.appendChild(el("p", "Could not load: " + e.message)); }
}

const ALIASES = { matrix: "runs" };      // old links (#matrix) still open the Runs page
async function route() {
  const h = new URLSearchParams(location.hash.slice(1));
  const page = ALIASES[location.hash.slice(1)] || location.hash.slice(1);
  await (h.get("run") ? showRun(h.get("run")) : show(PAGES.some(p => p[0] === page) ? page : "overview"));
  stamp(); navCounts();
}

function stamp() { const s = document.getElementById("refreshed"); if (s) s.textContent = "Last refreshed " + new Date().toLocaleTimeString(); }
async function navCounts() {            // e.g. "Improvements (3)": from the overview totals, one cheap request
  try {
    const d = await api("overview");
    const n = { runs: d.runs, improvements: d.proposals_waiting, safety: d.commands_refused };
    document.querySelectorAll("nav button").forEach(b => { const label = PAGES.find(p => p[0] === b.dataset.page)[1];
      b.textContent = n[b.dataset.page] ? label + " (" + n[b.dataset.page] + ")" : label; });
  } catch (e) { /* counts are optional */ }
}
function autoRefreshDue() {             // never pull the page away from a person reading or typing
  return document.visibilityState === "visible" && !document.querySelector("main details[open]") &&
    !Array.from(document.querySelectorAll("main input.filter")).some(i => i.value) && !document.querySelector("main input:focus");
}

document.addEventListener("DOMContentLoaded", () => {
  const nav = document.getElementById("nav");
  PAGES.forEach(([p, label]) => { const b = el("button", label); b.dataset.page = p; b.addEventListener("click", () => { location.hash = p; }); nav.appendChild(b); });
  const st = document.getElementById("status");
  st.appendChild(el("span", "Times are in your local time · ", "muted"));
  st.appendChild(el("span", "", "muted")).id = "refreshed";
  const lab = el("label", " Auto-refresh every 60 s", "muted"), cb = el("input"); cb.type = "checkbox";
  try { cb.checked = localStorage.getItem("cp-auto") === "1"; } catch (e) { /* storage blocked: off */ }
  cb.addEventListener("change", () => { try { localStorage.setItem("cp-auto", cb.checked ? "1" : "0"); } catch (e) {} });
  lab.prepend(cb); st.appendChild(document.createTextNode(" · ")); st.appendChild(lab);
  setInterval(() => { if (cb.checked && autoRefreshDue()) route(); }, 60000);    // re-renders the current hash: routing unchanged
  window.addEventListener("hashchange", route);
  route();
});
