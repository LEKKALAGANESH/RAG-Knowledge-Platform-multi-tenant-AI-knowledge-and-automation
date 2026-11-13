"""Prints a throwaway local dev environment as shell export lines; never writes a file.

    uv run python scripts/dev_env.py --shell powershell | Invoke-Expression
    eval "$(uv run python scripts/dev_env.py --shell bash)"

JWT_SECRET, FERNET_KEYS and API_KEY_PEPPER are new on every run: sign-ins and stored connector
credentials from an earlier run stop working. Any variable already set in the environment wins
(e.g. REDIS_URL without a password for a Redis started before compose required one).
"""

import argparse
import base64
import os
import secrets

# The dev passwords tests/conftest.py falls back to (local containers only).
DEFAULTS = {
    "POSTGRES_PASSWORD": "rag_dev_owner_pw",
    "APP_DB_PASSWORD": "rag_dev_app_pw",
    "OWNER_DB_PASSWORD": "rag_dev_table_owner_pw",
    "REDIS_PASSWORD": "rag_dev_redis_pw",
    "S3_ACCESS_KEY": "rag_dev_minio",
    "S3_SECRET_KEY": "rag_dev_minio_secret",
}


def build(environ: dict[str, str]) -> dict[str, str]:
    v = {**DEFAULTS, **{k: environ[k] for k in DEFAULTS if k in environ}}
    pg = "postgresql+asyncpg://{}@localhost:5432/rag"
    v["DATABASE_URL"] = pg.format(f"rag_app:{v['APP_DB_PASSWORD']}")
    v["MIGRATION_DATABASE_URL"] = pg.format(f"rag_owner:{v['OWNER_DB_PASSWORD']}")
    v["REDIS_URL"] = f"redis://:{v['REDIS_PASSWORD']}@localhost:6379/0"
    v["JWT_SECRET"] = secrets.token_urlsafe(48)
    v["FERNET_KEYS"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()  # Fernet key
    v["API_KEY_PEPPER"] = secrets.token_urlsafe(32)
    return {k: environ.get(k, val) for k, val in v.items()}


def render(values: dict[str, str], shell: str) -> str:
    if shell == "powershell":
        lines = [f"$env:{k} = '{val.replace(chr(39), chr(39) * 2)}'" for k, val in values.items()]
    else:
        lines = [f"export {k}='{val.replace(chr(39), chr(39) + chr(92) + chr(39) * 2)}'"
                 for k, val in values.items()]  # fmt: skip
    return "\n".join(["# throwaway dev env: secrets are new on every run", *lines]) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(prog="python scripts/dev_env.py")
    p.add_argument("--shell", choices=("powershell", "bash"), required=True)
    print(render(build(dict(os.environ)), p.parse_args().shell), end="")


if __name__ == "__main__":
    main()
