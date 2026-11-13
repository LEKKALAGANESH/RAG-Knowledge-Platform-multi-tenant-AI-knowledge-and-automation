"""scripts/dev_env.py prints a complete, valid throwaway env for both shells and writes nothing."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import Settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev_env.py"
REQUIRED = {
    "DATABASE_URL", "MIGRATION_DATABASE_URL", "REDIS_URL", "JWT_SECRET", "FERNET_KEYS",
    "API_KEY_PEPPER", "POSTGRES_PASSWORD", "APP_DB_PASSWORD", "OWNER_DB_PASSWORD",
    "REDIS_PASSWORD", "S3_ACCESS_KEY", "S3_SECRET_KEY",
}  # fmt: skip
LINE = {
    "powershell": re.compile(r"^\$env:(\w+) = '((?:[^']|'')*)'$"),
    "bash": re.compile(r"^export (\w+)='([^']*)'$"),
}


def _call(shell: str, cwd: Path, env: dict[str, str]) -> str:
    cmd = [sys.executable, str(SCRIPT), "--shell", shell]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd, env=env)  # noqa: S603
    assert r.returncode == 0, r.stderr
    return r.stdout


def _run(shell: str, tmp: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in REQUIRED}
    out: dict[str, str] = {}
    for line in _call(shell, tmp, env).splitlines():
        if line.startswith("#"):
            continue
        m = LINE[shell].match(line)
        assert m, line
        out[m[1]] = m[2].replace("''", "'") if shell == "powershell" else m[2]
    return out


@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_prints_every_required_var_and_valid_settings(shell: str, tmp_path: Path) -> None:
    v = _run(shell, tmp_path)
    assert set(v) == REQUIRED
    s = Settings(_env_file=None, **{k.lower(): val for k, val in v.items()})  # type: ignore[call-arg]
    assert s.jwt_secret.get_secret_value() == v["JWT_SECRET"]
    assert "rag_owner:rag_dev_table_owner_pw@" in v["MIGRATION_DATABASE_URL"]
    assert v["REDIS_URL"] == "redis://:rag_dev_redis_pw@localhost:6379/0"
    assert list(tmp_path.iterdir()) == []  # wrote no file
    again = _run(shell, tmp_path)
    assert all(again[k] != v[k] for k in ("JWT_SECRET", "FERNET_KEYS", "API_KEY_PEPPER"))


def test_existing_env_wins_and_quotes_are_escaped(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in REQUIRED}
    env |= {"APP_DB_PASSWORD": "it's", "REDIS_URL": "redis://localhost:6379/0"}
    for shell in ("powershell", "bash"):
        out = _call(shell, tmp_path, env)
        assert "redis://localhost:6379/0'" in out
        assert ("rag_app:it''s@" if shell == "powershell" else "rag_app:it'\\''s@") in out
