# scripts/daytona_smoke.py
"""Live check of the Daytona sandbox (needs DAYTONA_API_KEY and a mirror).
Usage: uv run python scripts/daytona_smoke.py LijiAlex layered-memory   (any public repo the mirror can clone)
Prints one line per check and a JSON summary; exit 1 if a required check fails."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def smoke(provider, owner: str, name: str, sha: str) -> dict:
    t0 = time.monotonic()
    h = provider.create("smoke", owner, name, sha, install_command="python -m pip --version")
    res = {"create_s": round(time.monotonic() - t0, 1), "box_root": h.extra.get("box_root")}
    b = h.backend
    try:
        res["tools"] = b.execute("git --version && python3 --version").exit_code == 0
        res["no_remote"] = b.execute("git -C sandbox remote -v | wc -l").output.split()[0] == "0"
        b.write("/sandbox/smoke_probe.py", "probe = 1\n")
        res["write_read"] = "probe = 1" in str(b.read("/sandbox/smoke_probe.py"))
        g = b.glob("**/*.py", "/sandbox/")
        matches = g.matches if hasattr(g, "matches") else g
        paths = [m["path"] if isinstance(m, dict) else m.path for m in matches]
        res["glob_virtual_paths"] = bool(paths) and not any(str(res["box_root"]) in p for p in paths)
        res["grep"] = "smoke_probe.py" in str(b.grep("probe = 1", "/sandbox/"))
        net = b.execute("python3 -c \"import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=5)\"")
        res["egress_open"] = net.exit_code == 0          # recorded, not required: free tier cannot block egress
    finally:
        provider.delete(h)
    res["required_ok"] = all(res[k] for k in ("tools", "no_remote", "write_read", "glob_virtual_paths", "grep"))
    return res


def main() -> int:
    from codepilot.config import load_settings
    from codepilot.repomap.mirror import HostMirror
    from codepilot.sandbox.daytona import DaytonaSandboxProvider
    owner, name = sys.argv[1], sys.argv[2]
    cfg = Path("codepilot.yml") if Path("codepilot.yml").exists() else Path("codepilot.example.yml")
    settings = load_settings(cfg)
    mirror = HostMirror(settings.home)
    path = mirror.ensure(owner, name)
    prov = DaytonaSandboxProvider(settings.home, settings.home / "repos", snapshot=settings.daytona_snapshot,
                                  auto_stop_minutes=settings.daytona_auto_stop_minutes)
    res = smoke(prov, owner, name, mirror.head_sha(path))
    print(json.dumps(res, indent=2))
    return 0 if res["required_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
