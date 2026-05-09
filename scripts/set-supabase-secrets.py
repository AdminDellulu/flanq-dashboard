#!/usr/bin/env python3
"""
Set Supabase edge-function secrets for the Flanq project.

Reads scripts/.env (KEY=value per line), uploads every non-empty var to the
Supabase Management API. No redeploy required — edge functions pick up the
new values on next invocation.

Usage:
    # First time: copy template + fill in
    cp scripts/.env.example scripts/.env
    $EDITOR scripts/.env

    # Get a Supabase Personal Access Token (one-time, reusable):
    # https://supabase.com/dashboard/account/tokens
    export SUPABASE_ACCESS_TOKEN=sbp_...
    # OR put it in ~/.supabase/access-token (one line, no prefix)

    # Preview what will be set:
    python3 scripts/set-supabase-secrets.py --dry-run

    # Do it:
    python3 scripts/set-supabase-secrets.py

    # See what's currently set:
    python3 scripts/set-supabase-secrets.py --list

    # Delete a specific secret:
    python3 scripts/set-supabase-secrets.py --delete VAR_NAME
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

DEFAULT_PROJECT_REF = "wvndlwxwgfrzuexnfirx"  # flanq-finance
DEFAULT_ENV_FILE = Path(__file__).parent / ".env"
API_BASE = "https://api.supabase.com"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes if present
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def get_access_token() -> str:
    tok = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    cli_path = Path.home() / ".supabase" / "access-token"
    if cli_path.exists():
        return cli_path.read_text().strip()
    sys.exit(
        "ERROR: No Supabase access token found.\n"
        "  Set env var SUPABASE_ACCESS_TOKEN, or create ~/.supabase/access-token.\n"
        "  Generate one at: https://supabase.com/dashboard/account/tokens"
    )


def api(method: str, path: str, token: str, body=None) -> tuple[int, str]:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "supabase-cli/2.90.0 (flanq-setter)")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def cmd_list(token: str, project: str) -> None:
    status, body = api("GET", f"/v1/projects/{project}/secrets", token)
    if status != 200:
        sys.exit(f"ERROR [{status}]: {body}")
    data = json.loads(body) if body else []
    print(f"Current secrets on project {project} ({len(data)} total):")
    for s in sorted(data, key=lambda x: x["name"]):
        print(f"  {s['name']}")


def cmd_delete(token: str, project: str, names: list[str]) -> None:
    status, body = api("DELETE", f"/v1/projects/{project}/secrets", token, names)
    if 200 <= status < 300:
        print(f"✓ Deleted {len(names)} secret(s) [{status}]")
    else:
        sys.exit(f"ERROR [{status}]: {body}")


def cmd_set(token: str, project: str, env_file: Path, dry_run: bool) -> None:
    env = load_env(env_file)
    if not env:
        sys.exit(
            f"ERROR: no vars found in {env_file}.\n"
            f"  First time? Run:  cp {env_file.with_suffix('.env.example').name} {env_file.name}"
        )

    secrets = [{"name": k, "value": v} for k, v in env.items() if v]
    missing = sorted(k for k, v in env.items() if not v)

    print(f"Target project: {project}")
    print(f"Source file:    {env_file}")
    print(f"Will set {len(secrets)} secret(s):")
    for s in secrets:
        print(f"  {s['name']}")
    if missing:
        print(f"\nSkipping {len(missing)} empty var(s) (fill them in to set):")
        for k in missing:
            print(f"  {k}")

    if dry_run:
        print("\n(dry-run, nothing sent)")
        return

    status, body = api(
        "POST", f"/v1/projects/{project}/secrets", token, secrets
    )
    if 200 <= status < 300:
        print(f"\n✓ OK [{status}] — {len(secrets)} secret(s) set")
    else:
        sys.exit(f"\n✗ FAIL [{status}]: {body}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(DEFAULT_ENV_FILE), help="path to .env file (default: scripts/.env)")
    ap.add_argument("--project", default=DEFAULT_PROJECT_REF, help=f"Supabase project ref (default: {DEFAULT_PROJECT_REF})")
    ap.add_argument("--dry-run", action="store_true", help="preview only; do not send")
    ap.add_argument("--list", action="store_true", help="list current secrets and exit")
    ap.add_argument("--delete", nargs="+", metavar="NAME", help="delete secret(s) by name and exit")
    args = ap.parse_args()

    token = get_access_token()

    if args.list:
        cmd_list(token, args.project)
    elif args.delete:
        cmd_delete(token, args.project, args.delete)
    else:
        cmd_set(token, args.project, Path(args.env), args.dry_run)


if __name__ == "__main__":
    main()
