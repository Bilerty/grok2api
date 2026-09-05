#!/usr/bin/env python3
"""Import a "名称 | URL" proxy pool into Grok2API and fan out egress nodes.

Each line: "<name> | <proxy-url>" — the URL is passed through verbatim
({account} templates included, nothing re-encoded). Every line becomes one
egress-proxy-profile (the 地址库), then one node per requested scope, each
bound to its profile by id. Node name = profile name + suffix, e.g.
"pool-sid1234build" / "...web" / "...console".

Idempotent by name: reruns reuse existing profiles/nodes, creating only what
is missing. Credentials are never printed.

Stdlib only. Reports are written 0600; keep them out of git.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


class Client:
    def __init__(self, base: str, timeout: int = 60):
        self.base = base.rstrip("/")
        self.token = ""
        self.timeout = timeout
        self.lock = threading.Lock()

    def request(self, method: str, path: str, payload: dict | None = None,
                timeout: int | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode()
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"error": {"code": "raw", "message": body[:200]}}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, {"error": {"code": "transport", "message": str(e)}}

    def login(self, username: str, password: str) -> None:
        status, body = self.request("POST", "/api/admin/v1/auth/login",
                                    {"username": username, "password": password})
        if status != 200:
            raise SystemExit(f"login failed: {body.get('error', {}).get('code', status)}")
        self.token = body["data"]["tokens"]["accessToken"]

    def list_all(self, path: str) -> list[dict]:
        items, page = [], 1
        while True:
            status, body = self.request("GET", f"{path}?page={page}&pageSize=500")
            if status != 200:
                raise SystemExit(f"GET {path} failed: {body.get('error', {}).get('code', status)}")
            data = body.get("data", {})
            items.extend(data.get("items", []))
            if len(items) >= int(data.get("total", 0)) or not data.get("items"):
                return items
            page += 1


def parse_pool(text: str) -> list[dict]:
    rows = []
    seen = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " | " not in line:
            raise SystemExit(f"line {lineno}: expected '名称 | URL', got: {line[:60]}")
        name, url = (part.strip() for part in line.split(" | ", 1))
        if not name or "://" not in url:
            raise SystemExit(f"line {lineno}: invalid name or URL")
        if name in seen:
            raise SystemExit(f"line {lineno}: duplicate name {name}")
        seen.add(name)
        rows.append({"name": name, "url": url})
    if not rows:
        raise SystemExit("no pool entries found")
    return rows


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pool", help="file with '名称 | URL' per line")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=os.environ.get("GROK2API_ADMIN_PASSWORD", ""))
    parser.add_argument("--password-file", default="")
    parser.add_argument("--scopes", default="grok_build:build,grok_web:web,grok_console:console",
                        help="comma list of scope:suffix; one node per scope per pool entry")
    parser.add_argument("--out-dir", default="egress-pool")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.password_file and not args.password:
        args.password = Path(args.password_file).read_text(encoding="utf-8").strip()
    if not args.dry_run and not args.password:
        raise SystemExit("need --password, --password-file, or GROK2API_ADMIN_PASSWORD")

    scopes: list[tuple[str, str]] = []
    for part in args.scopes.split(","):
        scope, _, suffix = part.strip().partition(":")
        if not scope or not suffix:
            raise SystemExit(f"bad --scopes entry: {part!r} (want scope:suffix)")
        scopes.append((scope, suffix))

    rows = parse_pool(Path(args.pool).read_text(encoding="utf-8"))
    out = Path(args.out_dir)
    write_private(out / "pool-plan.md",
                  f"# Pool import plan\n\n- entries: {len(rows)}\n- scopes: {', '.join(s for s, _ in scopes)}\n"
                  f"- nodes to create: {len(rows) * len(scopes)}\n\n"
                  + "\n".join(f"- {r['name']} → " + ", ".join(r['name'] + suf for _, suf in scopes) for r in rows) + "\n")
    if args.dry_run:
        print(f"dry-run: {len(rows)} entries × {len(scopes)} scopes = {len(rows) * len(scopes)} nodes; plan at {out / 'pool-plan.md'}")
        return 0

    client = Client(args.api_base)
    client.login(args.username, args.password)
    profiles = {p.get("name"): p for p in client.list_all("/api/admin/v1/egress-proxy-profiles")}
    nodes = {n.get("name"): n for n in client.list_all("/api/admin/v1/egress-nodes")}

    def find_by_name(path: str, name: str) -> dict | None:
        for item in client.list_all(path):
            if item.get("name") == name:
                return item
        return None

    def one(row: dict) -> dict:
        name, url = row["name"], row["url"]
        with client.lock:
            profile = profiles.get(name)
            created_profile = False
            if profile is None:
                status, body = client.request("POST", "/api/admin/v1/egress-proxy-profiles",
                                              {"name": name, "proxyURL": url})
                if status in (200, 201):
                    profile, created_profile = body.get("data", {}), True
                elif body.get("error", {}).get("code") == "egressConflict":
                    profile = find_by_name("/api/admin/v1/egress-proxy-profiles", name)
                else:
                    return {**row, "results": [f"profile error: {body['error'].get('code')}"]}
                if profile is None:
                    return {**row, "results": ["profile error: not found after conflict"]}
                profiles[name] = profile
            profile_id = str(profile.get("id", ""))

            results = ["profile created" if created_profile else "profile reused"]
            node_ids = {}
            for scope, suffix in scopes:
                node_name = name + suffix
                node = nodes.get(node_name)
                created_node = False
                if node is None:
                    status, body = client.request("POST", "/api/admin/v1/egress-nodes",
                                                  {"name": node_name, "scope": scope, "enabled": True,
                                                   "proxyPool": False, "proxyProfileId": profile_id})
                    if status in (200, 201):
                        node, created_node = body.get("data", {}), True
                    elif body.get("error", {}).get("code") == "egressConflict":
                        node = find_by_name("/api/admin/v1/egress-nodes", node_name)
                    else:
                        results.append(f"{scope} error: {body['error'].get('code')}")
                        continue
                    if node is None:
                        results.append(f"{scope} error: not found after conflict")
                        continue
                    nodes[node_name] = node
                node_ids[scope] = str(node.get("id", ""))
                results.append(f"{scope} {'created' if created_node else 'reused'}")
            return {**row, "profile_id": profile_id, "node_ids": node_ids, "results": results}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        done = list(pool.map(one, rows))

    errors = [r for r in done if any("error" in x for x in r["results"])]
    print(f"provisioned: {len(done) - len(errors)} ok, {len(errors)} with errors")
    write_private(out / "pool-result.json", json.dumps(
        [{"name": r["name"], "profile_id": r["profile_id"], "node_ids": r.get("node_ids", {}),
          "results": r["results"]} for r in done], ensure_ascii=False, indent=2) + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
