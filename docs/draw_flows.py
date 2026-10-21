"""Draws the flow diagrams in the same style as docs/architecture.svg. Coordinates are explicit.
Usage: python3 docs/draw_flows.py docs/flows   (writes one SVG per flow; render to PNG in a browser at 2x)."""
import html, sys
from pathlib import Path

C = {  # kind: (stroke, fill, tag text)
    "ai":     ("#2F6FDB", "#EAF1FD", "AI"),
    "code":   ("#0E7C7B", "#E4F3F2", "CODE"),
    "person": ("#C46A00", "#FCEEDD", "PERSON"),
    "ext":    ("#5B6773", "#FFFFFF", ""),
    "store":  ("#5B6773", "#F2F4F6", ""),
}
SCOPE = {"brief": ("BRIEF", "#46546A"), "bonus": ("BONUS", "#6B4FBB"), "plus": ("PLUS", "#B4366B")}
PLUS = SCOPE["plus"][1]
DASH_F = 'stroke-dasharray="6 5"'
DASH_A = 'stroke-dasharray="5 4"'
out = []
def esc(s): return html.escape(s, quote=False)

def box(x, y, w, h, title, lines=(), kind="code", plus=False, tag=True, scope=None):
    scope = "plus" if plus else scope
    stroke, fill, t = C[kind]
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
    ty = y + 22
    out.append(f'<text x="{x+12}" y="{ty}" class="t">{esc(title)}</text>')
    right = x + w - 10
    if tag and t:
        tw = 10 + 7.2 * len(t)
        right -= tw
        out.append(f'<rect x="{right}" y="{y-9}" width="{tw}" height="18" rx="9" fill="{stroke}"/>')
        out.append(f'<text x="{right+tw/2}" y="{y+4}" class="tag" text-anchor="middle">{t}</text>')
        right -= 6
    if scope:
        st, sc = SCOPE[scope]
        sw = 10 + 7.2 * len(st)
        right -= sw
        out.append(f'<rect x="{right}" y="{y-9}" width="{sw}" height="18" rx="9" fill="{sc}"/>')
        out.append(f'<text x="{right+sw/2}" y="{y+4}" class="tag" text-anchor="middle">{st}</text>')
    for i, l in enumerate(lines):
        out.append(f'<text x="{x+12}" y="{ty+19+i*17}" class="s">{esc(l)}</text>')

def frame(x, y, w, h, label, plus=False, dashed=True, scope=None, lx=0):
    scope = "plus" if plus else scope
    x0 = x
    x = x + lx
    out.append(f'<rect x="{x0}" y="{y}" width="{w}" height="{h}" rx="14" fill="none" stroke="#8A96A3" stroke-width="1.4" {DASH_F if dashed else ""}/>')
    out.append(f'<text x="{x+14}" y="{y+22}" class="f">{esc(label)}</text>')
    if scope:
        st, sc = SCOPE[scope]
        lw = 7.6 * len(label) + 26
        sw = 8 + 7.2 * len(st)
        out.append(f'<rect x="{x+lw}" y="{y+9}" width="{sw}" height="17" rx="8.5" fill="{sc}"/>')
        out.append(f'<text x="{x+lw+sw/2}" y="{y+21.5}" class="tag" text-anchor="middle">{st}</text>')

def arrow(points, label=None, lx=None, ly=None, dashed=False, color="#4A5561"):
    d = "M " + " L ".join(f"{a},{b}" for a, b in points)
    out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.7" marker-end="url(#ah)" {DASH_A if dashed else ""}/>')
    if label:
        out.append(f'<text x="{lx}" y="{ly}" class="al" text-anchor="middle">{esc(label)}</text>')

def header(title, sub, scope=None):
    out.append(f'<text x="30" y="44" class="h1">{esc(title)}</text>')
    if scope:
        st, sc = SCOPE[scope]
        sw = 10 + 7.2 * len(st)
        tx = 30 + 14.5 * len(title) + 12
        out.append(f'<rect x="{tx}" y="27" width="{sw}" height="20" rx="10" fill="{sc}"/>')
        out.append(f'<text x="{tx+sw/2}" y="41" class="tag" text-anchor="middle">{st}</text>')
    out.append(f'<text x="30" y="68" class="sub">{esc(sub)}</text>')
    lx = 1040
    for i, (k, label) in enumerate([("ai", "AI agent / model"), ("code", "plain code"), ("person", "a person decides")]):
        stroke, fill, _ = C[k]
        out.append(f'<rect x="{lx + i*185}" y="30" width="18" height="18" rx="4" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
        out.append(f'<text x="{lx + i*185 + 26}" y="44" class="lg">{label}</text>')
    for i, k in enumerate(["brief", "bonus", "plus"]):
        st, sc = SCOPE[k]
        sw = 10 + 7.2 * len(st)
        out.append(f'<rect x="{lx + i*185}" y="56" width="{sw}" height="17" rx="8.5" fill="{sc}"/>')
        out.append(f'<text x="{lx + i*185 + sw/2}" y="68.5" class="tag" text-anchor="middle">{st}</text>')
        out.append(f'<text x="{lx + i*185 + sw + 8}" y="69" class="lg">{["assignment brief", "brief optional extra", "beyond the brief"][i]}</text>')

def save(path, W, H):
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
    Path(path).write_text(svg)
    out.clear()
    print("written", path)


def solver():
    header("Solver", "From a labelled issue to a pull request, with a person deciding before anything reaches GitHub")
    box(30, 110, 190, 72, "GitHub issue", ["labelled ai-assignable"], "ext", tag=False)
    box(250, 110, 190, 72, "Poller", ["every 5 min, skips claimed", "or recently failed issues"], "code", scope="brief")
    box(470, 110, 220, 72, "Clean + injection screen", ["NeMo rails: regex + Haiku", "check"], "code", plus=True)
    box(720, 110, 190, 72, "Triage", ["5 task types → skill", "complexity 1–10 (bonus)"], "ai", scope="brief")
    box(940, 110, 260, 72, "Context for the agents", ["repo map ≤ 4,000 tokens,", "task skill + repo skill, 3 lessons"], "code", scope="brief")
    arrow([(220, 146), (248, 146)]); arrow([(440, 146), (468, 146)])
    arrow([(690, 146), (718, 146)]); arrow([(910, 146), (938, 146)])
    box(470, 210, 220, 56, "Blocked", ["skipped and logged"], "code")
    arrow([(580, 182), (580, 208)], "attack", 610, 200)
    box(720, 210, 190, 56, "Too complex", ["a plan instead of code"], "ai", plus=True)
    arrow([(815, 182), (815, 208)])
    frame(30, 300, 1540, 175, "One run · fresh Daytona sandbox · whole repository · no GitHub credentials", scope="bonus", lx=230)
    arrow([(1070, 182), (1070, 282), (155, 282), (155, 343)])
    box(60, 345, 190, 80, "Orchestrator", ["plans, hands out work", "by file path"], "ai", scope="brief")
    box(300, 345, 210, 80, "Repo Explorer", ["only if the issue doesn't", "name the files"], "ai", scope="brief")
    box(560, 345, 190, 80, "Coder", ["small edits,", "runs the code"], "ai", scope="brief")
    box(800, 345, 210, 80, "Test Agent", ["writes + runs tests, max 3;", "Test Doctor fixes setup (bonus)"], "ai", scope="brief")
    box(1060, 345, 200, 80, "PR Agent", ["drafts commit", "and PR text"], "ai", scope="brief")
    box(1310, 345, 240, 80, "Guards on every tool call", ["commands: allow / refuse / ask", "writes only inside the sandbox"], "code", scope="brief")
    arrow([(250, 385), (298, 385)], dashed=True)
    arrow([(510, 385), (558, 385)]); arrow([(750, 385), (798, 385)]); arrow([(1010, 385), (1058, 385)])
    arrow([(155, 425), (155, 450), (655, 450), (655, 427)])
    out.append('<text x="405" y="465" class="al" text-anchor="middle">issue already names the files</text>')
    box(30, 530, 230, 80, "Save and end the run", ["no sandbox waits", "for a person"], "code", scope="plus")
    arrow([(1160, 425), (1160, 500), (145, 500), (145, 528)])
    out.append('<text x="650" y="494" class="al" text-anchor="middle">also on: over 5 files · a 3rd test attempt · an unknown command · a plan</text>')
    box(300, 530, 260, 80, "You decide · terminal UI", ["inspect the diff, tests, PR text", "approve or reject"], "person", scope="brief")
    box(600, 530, 280, 80, "Publish", ["re-checks the issue and main,", "one commit with the approved change"], "code", scope="brief")
    box(920, 530, 260, 80, "Pull request on GitHub", ["labelled, linked to the issue", "→ independent review"], "ext", tag=False)
    arrow([(260, 570), (298, 570)]); arrow([(560, 570), (598, 570)])
    arrow([(880, 570), (918, 570)])


def review():
    header("PR review", scope="plus", sub="Every pull request is scored by a model from a different company; code decides merge-ready")
    box(30, 150, 230, 90, "Pull request", ["opened or updated by", "CodePilot+, a person or a fork"], "ext", tag=False)
    frame(300, 100, 520, 230, "Independent reviewer · GitHub Action")
    box(320, 140, 220, 74, "Job 1: tests", ["runs the PR's tests, no secrets;", "not run for forks"], "code")
    box(320, 236, 220, 74, "Job 2: review", ["OpenAI model scores", "5 criteria × 0–2"], "ai")
    box(580, 180, 220, 90, "Score caps", ["security concerns → max 4", "tests not run or failing", "→ max 6"], "code")
    arrow([(260, 195), (280, 195), (280, 177), (318, 177)]); arrow([(280, 195), (280, 273), (318, 273)])
    arrow([(540, 177), (560, 177), (560, 215), (578, 215)]); arrow([(540, 273), (560, 273), (560, 235), (578, 235)])
    box(860, 180, 230, 90, "Merge-ready?", ["score ≥ 8, tests pass,", "no protected files"], "code")
    arrow([(800, 225), (858, 225)])
    box(1140, 110, 200, 74, "You merge", ["from the dashboard"], "person")
    box(1370, 110, 200, 74, "PR watcher", ["merged → lesson saved", "conflict → run stops"], "code")
    box(1140, 260, 200, 74, "A person decides", ["labelled needs-review"], "person")
    arrow([(1090, 205), (1115, 205), (1115, 147), (1138, 147)], "yes", 1104, 170)
    arrow([(1090, 245), (1115, 245), (1115, 297), (1138, 297)], "no", 1104, 280)
    arrow([(1340, 147), (1368, 147)])


def improve():
    header("Self-improvement loop", scope="plus", sub="After every run, CodePilot+ proposes one change to the repository's skill; nothing changes until you approve")
    xs = [30, 250, 470, 690, 910, 1130, 1350]
    items = [("Run ends", ["any outcome"], "code", False),
             ("Trace analysis", ["refused commands,", "files hard to find"], "code", False),
             ("Skill gap report", ["what slowed the", "agents down"], "ai", False),
             ("One proposal", ["add, correct or", "retire one step"], "ai", False),
             ("Confidence", ["rises when later runs", "propose it again"], "code", False),
             ("You decide", ["on the dashboard"], "person", False),
             ("Repo skill v+1", ["old versions kept"], "code", False)]
    for x, (t, ls, k, p) in zip(xs, items):
        box(x, 130, 190, 78, t, ls, k, plus=p)
    for a, b in zip(xs, xs[1:]):
        arrow([(a + 190, 169), (b - 2, 169)])
    box(1130, 260, 190, 56, "Not applied", ["the rejection is recorded"], "code")
    arrow([(1225, 208), (1225, 258)], "reject", 1255, 238)
    arrow([(1445, 208), (1445, 345), (125, 345), (125, 210)])
    out.append('<text x="785" y="338" class="al" text-anchor="middle">the next run on this repository uses the new skill</text>')


def lessons():
    header("Lessons", scope="brief", sub="What happened on past issues is recalled for similar new ones")
    box(30, 110, 220, 64, "Pull request merged", ["what worked"], "ext", tag=False)
    box(30, 210, 220, 64, "Run failed or rejected", ["what went wrong"], "ext", tag=False)
    box(300, 160, 200, 64, "Lesson saved", ["short and specific"], "code")
    arrow([(250, 142), (275, 142), (275, 185), (298, 185)]); arrow([(250, 242), (275, 242), (275, 200), (298, 200)])
    box(550, 160, 220, 64, "ChromaDB", ["per repository + task type"], "store", tag=False)
    arrow([(500, 192), (548, 192)])
    box(820, 160, 210, 64, "Recall the 3 most similar", ["by meaning"], "code")
    arrow([(770, 192), (818, 192)])
    box(820, 270, 210, 56, "New issue", ["same repository"], "ext", tag=False)
    arrow([(925, 270), (925, 226)])
    box(1080, 160, 260, 64, "Orchestrator and agents", ["plan with them, marked untrusted"], "ai")
    arrow([(1030, 192), (1078, 192)])


def connect():
    header("Connecting a repository", scope="plus", sub="One command prepares a repository for CodePilot+")
    items = [("You run connect", ["codepilot connect", "OWNER/REPO --write"], "person"),
             ("Check access", ["through the GitHub App"], "code"),
             ("Create labels", ["ai-assignable and", "review labels"], "code"),
             ("Detect setup", ["install and test", "commands"], "code"),
             ("Build repo map", ["ranked, within", "the token budget"], "code"),
             ("Repo skill v1", ["notes for this", "repository"], "code"),
             ("Code index", ["embeddings for", "search"], "code"),
             ("codepilot.yml", ["the repository's", "entry is added"], "code")]
    x = 30
    for i, (t, ls, k) in enumerate(items):
        box(x, 120, 170, 78, t, ls, k)
        if i: arrow([(x - 22, 159), (x - 2, 159)])
        x += 192


if __name__ == "__main__":
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/flows"); d.mkdir(parents=True, exist_ok=True)
    solver(); save(d / "solver.svg", 1600, 640)
    review(); save(d / "pr-review.svg", 1600, 360)
    improve(); save(d / "self-improvement.svg", 1600, 380)
    lessons(); save(d / "lessons.svg", 1600, 350)
    connect(); save(d / "connect.svg", 1600, 240)
