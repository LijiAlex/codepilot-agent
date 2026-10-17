# tests/test_with_env.py
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_with_env_loads_the_file_without_printing_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("CP_TEST_SECRET=hunter2-not-real\n")
    probe = "import os; print('set' if os.environ.get('CP_TEST_SECRET') else 'missing')"
    out = subprocess.run([str(ROOT / "scripts" / "with-env"), "python", "-c", probe], capture_output=True, text=True,
                         env={**os.environ, "CODEPILOT_ENV_FILE": str(env_file)}, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "set" and "hunter2" not in out.stdout + out.stderr


def test_with_env_explains_a_missing_file(tmp_path):
    out = subprocess.run([str(ROOT / "scripts" / "with-env"), "python", "-c", "print(1)"], capture_output=True, text=True,
                         env={**os.environ, "CODEPILOT_ENV_FILE": str(tmp_path / "nope.env")}, cwd=ROOT)
    assert out.returncode == 2 and "cp .env.example .env" in out.stderr
