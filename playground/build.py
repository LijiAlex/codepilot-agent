# playground/build.py
"""Build the playground repository from a local clone of guardrail-eval-pipeline.
Usage: uv run python playground/build.py A3_CLONE DEST
Creates DEST as a fresh git repo (one commit, personal noreply email); pushing it is a separate, manual step."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from plants import PLANTS  # noqa: E402

DROP = ["logs", "runs"]                         # recorded outputs, not code
NOTICE = ("> **Playground.** A copy of [guardrail-eval-pipeline](https://github.com/LijiAlex/guardrail-eval-pipeline) v0.2.0 "
          "with planted, documented defects, used to demonstrate and measure CodePilot+. Not maintained.\n\n")
EMAIL = "59106185+LijiAlex@users.noreply.github.com"


def build(a3: Path, dest: Path, tag: str = "v0.2.0") -> Path:
    if dest.exists():
        raise SystemExit(f"{dest} exists; choose a new folder")
    dest.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(a3), "archive", "--format=tar", tag], check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    for d in DROP:
        shutil.rmtree(dest / d, ignore_errors=True)
    for rel, old, new in PLANTS:
        p = dest / rel
        if old is None:                         # a new file
            if p.exists():
                raise SystemExit(f"plant would overwrite an existing file: {rel}")
            p.write_text(new)
            continue
        text = p.read_text()
        if text.count(old) != 1:
            raise SystemExit(f"plant does not apply exactly once: {rel}: {old!r}")
        p.write_text(text.replace(old, new))
    readme = dest / "README.md"
    readme.write_text(NOTICE + readme.read_text())
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", f"user.email={EMAIL}", "-c", "user.name=LijiAlex", "commit", "-qm",
                  "Playground: guardrail-eval-pipeline v0.2.0 with planted issues"]):
        subprocess.run(["git", *args], cwd=dest, check=True)
    return dest


if __name__ == "__main__":
    print(build(Path(sys.argv[1]), Path(sys.argv[2])))
