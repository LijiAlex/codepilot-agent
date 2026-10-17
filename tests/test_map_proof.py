# tests/test_map_proof.py
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("map_proof", ROOT / "scripts" / "map_proof.py")
mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)


def test_proof_on_a_synthetic_repo_fits_budget(tmp_path):
    for i in range(400):
        (tmp_path / f"pkg{i % 20}").mkdir(exist_ok=True)
        (tmp_path / f"pkg{i % 20}" / f"mod{i}.py").write_text(f'"""Module {i}."""\nfrom pkg0.mod0 import f\n\ndef f{i}():\n    pass\n')
    r = mp.prove(tmp_path, 1000, ["mod7"])
    assert r["files"] == 400 and r["map_tokens"] <= 1000 and r["files_shown"] > 5 and r["top"][0] == "pkg0/mod0.py"
    assert mp.main([str(tmp_path), "--budget", "1000"]) == 0
