"""Draws docs/architecture.svg (one-screen architecture). Coordinates are explicit.
Usage: python3 docs/draw_architecture.py docs/architecture.svg  (then render to PNG in a browser at 2x)."""
import html, sys

W, H = 1600, 1010
C = {  # kind: (stroke, fill, tag text)
    "ai":     ("#2F6FDB", "#EAF1FD", "AI"),
    "code":   ("#0E7C7B", "#E4F3F2", "CODE"),
    "person": ("#C46A00", "#FCEEDD", "PERSON"),
    "ext":    ("#5B6773", "#FFFFFF", ""),
    "store":  ("#5B6773", "#F2F4F6", ""),
}
PLUS = "#B4366B"
DASH_F = 'stroke-dasharray="6 5"'
DASH_A = 'stroke-dasharray="5 4"'
out = []
def esc(s): return html.escape(s, quote=False)

def box(x, y, w, h, title, lines=(), kind="code", plus=False, tag=True):
    stroke, fill, t = C[kind]
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
    ty = y + 22
    out.append(f'<text x="{x+12}" y="{ty}" class="t">{esc(title)}</text>')
    right = x + w - 10                                    # pills sit on the top edge, right-aligned, clear of the title
    if tag and t:
        tw = 10 + 7.2 * len(t)
        right -= tw
        out.append(f'<rect x="{right}" y="{y-9}" width="{tw}" height="18" rx="9" fill="{stroke}"/>')
        out.append(f'<text x="{right+tw/2}" y="{y+4}" class="tag" text-anchor="middle">{t}</text>')
        right -= 6
    if plus:
        right -= 46
        out.append(f'<rect x="{right}" y="{y-9}" width="46" height="18" rx="9" fill="{PLUS}"/>')
        out.append(f'<text x="{right+23}" y="{y+4}" class="tag" text-anchor="middle">PLUS</text>')
    for i, l in enumerate(lines):
        out.append(f'<text x="{x+12}" y="{ty+19+i*17}" class="s">{esc(l)}</text>')

def frame(x, y, w, h, label, plus=False, dashed=True):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="none" stroke="#8A96A3" stroke-width="1.4" {DASH_F if dashed else ""}/>')
    out.append(f'<text x="{x+14}" y="{y+22}" class="f">{esc(label)}</text>')
    if plus:
        lw = 7.6 * len(label) + 26
        out.append(f'<rect x="{x+lw}" y="{y+9}" width="44" height="17" rx="8.5" fill="{PLUS}"/>')
        out.append(f'<text x="{x+lw+22}" y="{y+21.5}" class="tag" text-anchor="middle">PLUS</text>')

def arrow(points, label=None, lx=None, ly=None, dashed=False, color="#4A5561"):
    d = "M " + " L ".join(f"{a},{b}" for a, b in points)
    out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.7" marker-end="url(#ah)" {DASH_A if dashed else ""}/>')
    if label:
        out.append(f'<text x="{lx}" y="{ly}" class="al" text-anchor="middle">{esc(label)}</text>')

# ---- header ----
out.append('<text x="30" y="44" class="h1">CodePilot+ — architecture</text>')
out.append('<text x="30" y="68" class="sub">GitHub issue → screened → agents in a sandbox → human approval → pull request → independent review → merge → lessons</text>')
lx = 1040
for i, (k, label) in enumerate([("ai", "AI agent / model"), ("code", "plain code"), ("person", "a person decides")]):
    stroke, fill, _ = C[k]
    out.append(f'<rect x="{lx + i*185}" y="30" width="18" height="18" rx="4" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
    out.append(f'<text x="{lx + i*185 + 26}" y="44" class="lg">{label}</text>')
out.append(f'<rect x="{lx}" y="56" width="44" height="17" rx="8.5" fill="{PLUS}"/><text x="{lx+22}" y="68.5" class="tag" text-anchor="middle">PLUS</text>')
out.append(f'<text x="{lx+52}" y="69" class="lg">beyond the assignment brief</text>')

# ---- left: GitHub + you ----
box(30, 110, 200, 72, "GitHub issues", ["labelled ai-assignable"], "ext", tag=False)
box(30, 600, 200, 100, "You · terminal UI", ["4 panels: issues, active task,", "live agent logs, approvals", "a approve · r reject · v inspect"], "person")
box(30, 760, 200, 106, "You · dashboard", ["merge pull requests,", "approve skill changes,", "evaluation, safety, costs,", "audit log"], "person", plus=True)

# ---- row 1: intake ----
box(260, 110, 195, 72, "Poller", ["every 5 min, GitHub Toolkit", "skips claimed / recently failed"], "code")
box(475, 110, 225, 72, "Clean + injection screen", ["NeMo rails: regex + Haiku check", "blocked issues never reach agents"], "code")
box(720, 110, 200, 72, "Triage", ["5 task types → which skill", "complexity score 1–10"], "ai")
box(940, 110, 190, 72, "Too complex?", ["a plan goes to approval", "instead of code"], "ai", plus=True)
arrow([(230, 146), (258, 146)])
arrow([(455, 146), (473, 146)])
arrow([(700, 146), (718, 146)])
arrow([(920, 146), (938, 146)], dashed=True)

# ---- row 2: the run ----
frame(260, 222, 870, 350, "One run · fresh Daytona sandbox, whole repo, no GitHub credentials · Claude Sonnet")
arrow([(820, 182), (820, 220)])
box(280, 262, 230, 140, "Context for the agents", ["repo map: tree + summaries,", "ranked by PageRank, ≤ 4,000", "tokens · task skill + repo skill", "top-3 similar lessons", "files read only on demand"], "code")
box(560, 262, 400, 66, "Orchestrator", ["plans with write_todos · delegates by file path, never contents"], "ai")
arrow([(510, 300), (558, 300)])
box(560, 368, 125, 72, "Repo Explorer", ["keyword or", "embedding search"], "ai", tag=False)
box(700, 368, 120, 72, "Coder", ["small edits,", "runs the code"], "ai", tag=False)
box(835, 368, 125, 72, "PR Agent", ["drafts commit", "and PR text"], "ai", tag=False)
for x in (622, 760, 897):
    arrow([(x, 328), (x, 366)])
box(680, 472, 160, 84, "Test Agent", ["writes + runs tests,", "max 3 attempts;", "Test Doctor fixes setup"], "ai", tag=False)
arrow([(760, 440), (760, 470)])
box(870, 452, 245, 98, "Guards on every tool call", ["commands: allow / refuse / ask", "write only inside the sandbox", "no secret files", "file check before publishing"], "code")
box(280, 422, 230, 128, "Save and end the run", ["the run saves its work and", "frees the sandbox; nothing", "waits on a person", "(then: approval queue)"], "code")

# ---- row 3: approval + publish ----
box(260, 600, 270, 100, "Approval queue", ["open PR to main (+ >5 files flag)", "retry after 2 failed test runs", "unknown command · complex plan"], "code")
arrow([(395, 567), (395, 598)])
arrow([(258, 640), (232, 640)])
arrow([(232, 670), (258, 670)])
box(560, 600, 290, 100, "Publish (after approval)", ["re-checks the issue and main", "one commit + PR with labels,", "via the GitHub App — the only", "path to GitHub"], "code")
arrow([(530, 650), (558, 650)])
box(880, 600, 250, 100, "PR watcher", ["merged → run done, lesson saved", "closed → run closed", "conflict → stops, never resolves"], "code")

# ---- right column: GitHub PRs, reviewer ----
box(1170, 110, 400, 72, "GitHub pull requests", ["branch codepilot/issue-N-slug · fix(#N): … · labels"], "ext", tag=False)
arrow([(850, 640), (865, 640), (865, 585), (1150, 585), (1150, 160), (1168, 160)])
frame(1170, 222, 400, 250, "Independent reviewer · GitHub Action", plus=True)
arrow([(1370, 182), (1370, 220)])
box(1190, 262, 170, 78, "Job 1: tests", ["runs the PR's tests,", "holds no secrets"], "code")
box(1375, 262, 175, 78, "Job 2: review", ["OpenAI model — a", "different company"], "ai")
box(1190, 352, 360, 104, "Score and merge-ready", ["5 criteria × 0–2 = score /10 · effort 1–5 from the diff", "caps: security → 4, tests not run/failing → 6", "merge-ready: score ≥ 8, tests green, no protected", "files · fork PRs reviewed from the diff only"], "code")
arrow([(1275, 340), (1275, 350)]); arrow([(1462, 340), (1462, 350)])

# ---- right column: self-improvement + evaluation ----
frame(1170, 500, 400, 175, "Self-improvement, after every run", plus=True)
box(1190, 538, 175, 62, "Trace analysis", ["what slowed agents", "down"], "code")
box(1380, 538, 170, 62, "Skill gap report", ["proposes add / fix /", "retire one step"], "ai")
arrow([(1365, 569), (1378, 569)])
box(1190, 610, 360, 52, "Repo skill v1 → v2 → …", ["confidence grows as proposals recur; you approve"], "code")
box(1170, 700, 400, 104, "Evaluation suite (scripts/evaluate.py)", ["13 rule checks · 14 golden issues with known outcomes", "hidden tests the agents never saw", "AI judge + 4 probes that test the judge", "injection screen on 27 labelled issues → one verdict"], "code", plus=True)

# dashboard ties
arrow([(230, 800), (1150, 800), (1150, 760), (1168, 760)], dashed=True, color="#C46A00")
arrow([(1150, 800), (1150, 640), (1168, 640)], dashed=True, color="#C46A00")
out.append('<text x="700" y="793" class="al" text-anchor="middle">merge PRs · approve skill changes · read the evaluation</text>')

# ---- storage band ----
frame(30, 880, 1540, 112, "Stored state and observability", dashed=False)
stores = [("SQLite", ["runs, approval queue,", "append-only audit log"]),
          ("LangGraph store", ["session summaries;", "last 3 read at start-up"]),
          ("ChromaDB", ["lessons by repo + task type,", "file-content embeddings"]),
          ("Repo map cache", ["per commit; only changed", "files re-parsed"]),
          ("LangSmith", ["traces of every model call,", "tokens and cost"])]
for i, (t, ls) in enumerate(stores):
    box(50 + i * 304, 914, 286, 66, t, ls, "store", tag=False)

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif">
<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#4A5561"/></marker>
<style>
.h1{{font-size:26px;font-weight:700;fill:#18212B}} .sub{{font-size:14px;fill:#5B6773}}
.t{{font-size:14.5px;font-weight:650;fill:#18212B}} .s{{font-size:12.5px;fill:#3B4651}}
.f{{font-size:13.5px;font-weight:650;fill:#5B6773}} .tag{{font-size:10px;font-weight:700;fill:#fff;letter-spacing:.04em}}
.al{{font-size:12px;fill:#4A5561;font-style:italic}} .lg{{font-size:13px;fill:#3B4651}}
</style></defs>
<rect width="{W}" height="{H}" fill="#F7F9FA"/>
{chr(10).join(out)}
</svg>'''
open(sys.argv[1], "w").write(svg)
print("written", sys.argv[1])
