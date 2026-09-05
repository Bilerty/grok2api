#!/usr/bin/env python3
"""Direct-egress companion to from_residential.py (方案 A: 不装 Mihomo).

Parses the same residential dump formats, then provisions each sticky session
straight into Grok2API as one proxy-profile + one bound node (scope=grok_build,
proxyPool=false), and optionally runs the node egress test to collect exit IPs.

Parsing is imported from from_residential.py so both paths accept identical
line formats. Credentials are never printed; reports mask passwords.

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
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from from_residential import parse_dump  # noqa: E402


def masked_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.username is None:
        return url
    userinfo = parts.username
    if parts.password:
        userinfo += ":***"
    netloc = f"{userinfo}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return f"{parts.scheme}://{netloc}"


class Client:
    def __init__(self, base: str, token: str | None = None, timeout: int = 60):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict | None = None,
                timeout: int | None = None) -> tuple[int, dict]:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(url, data=data, method=method)
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

    def login(self, username: str, password: str) -> str:
        status, body = self.request("POST", "/api/admin/v1/auth/login",
                                    {"username": username, "password": password})
        if status != 200:
            code = body.get("error", {}).get("code", status)
            raise SystemExit(f"login failed: {code}")
        return body["data"]["tokens"]["accessToken"]

    def list_all(self, path: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            status, body = self.request("GET", f"{path}?page={page}&pageSize=500")
            if status != 200:
                raise SystemExit(f"GET {path} failed: {body.get('error', {}).get('code', status)}")
            data = body.get("data", {})
            items.extend(data.get("items", []))
            if len(items) >= int(data.get("total", 0)) or not data.get("items"):
                break
            page += 1
        return items

    def create(self, path: str, payload: dict) -> tuple[bool, dict]:
        """Returns (created, item_or_error). Treats 409 egressConflict as not-created."""
        status, body = self.request("POST", path, payload)
        if status in (200, 201):
            return True, body.get("data", {})
        return False, body


def fallback_label(session: dict, seq: int) -> str:
    if session["name"]:
        return session["name"]
    if session["sid"]:
        return f"use-{session['sid'][:8]}"
    return f"direct-{seq:03d}"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def plan_report(rows: list[dict]) -> str:
    lines = [
        "# Direct-egress plan (no Mihomo)",
        "",
        f"- sessions: {len(rows)}",
        "- provisioning: 1 proxy-profile + 1 node (scope=grok_build, proxyPool=false) per session",
        "",
        "## Sessions (masked; safe to print)",
        "",
    ]
    for row in rows:
        lines.append(f"- {row['label']}: {masked_url(row['session']['scheme'] + '://' + row['url_for_mask'])}")
    return "\n".join(lines) + "\n"


def provision(args: argparse.Namespace, rows: list[dict], client: Client) -> None:

    existing_profiles = {p.get("name"): p for p in client.list_all("/api/admin/v1/egress-proxy-profiles")}
    existing_nodes = {n.get("name"): n for n in client.list_all("/api/admin/v1/egress-nodes")}
    if existing_profiles or existing_nodes:
        print(f"found existing: {len(existing_profiles)} profiles, {len(existing_nodes)} nodes (will reuse by name)")

    def find_by_name(path: str, label: str) -> dict | None:
        for item in client.list_all(path):
            if item.get("name") == label:
                return item
        return None

    lock = threading.Lock()

    def one(row: dict) -> dict:
        label, session = row["label"], row["session"]
        url = row["proxy_url"]
        created_profile, created_node = False, False
        with lock:
            profile = existing_profiles.get(label)
            if profile is None:
                created_profile, profile = client.create(
                    "/api/admin/v1/egress-proxy-profiles", {"name": label, "proxyURL": url})
                if not created_profile:
                    if profile.get("error", {}).get("code") != "egressConflict":
                        return {**row, "result": f"profile error: {profile.get('error', {}).get('code')}"}
                    profile = find_by_name("/api/admin/v1/egress-proxy-profiles", label)
                if profile is None:
                    return {**row, "result": "profile error: not found after conflict"}
                existing_profiles[label] = profile
            profile_id = str(profile.get("id", ""))

            node = existing_nodes.get(label)
            if node is None:
                payload = {"name": label, "scope": args.scope, "enabled": True,
                           "proxyPool": False, "proxyProfileId": profile_id}
                if args.account_capacity is not None:
                    payload["accountCapacity"] = args.account_capacity
                created_node, node = client.create("/api/admin/v1/egress-nodes", payload)
                if not created_node:
                    if node.get("error", {}).get("code") != "egressConflict":
                        return {**row, "result": f"node error: {node.get('error', {}).get('code')}"}
                    node = find_by_name("/api/admin/v1/egress-nodes", label)
                if node is None:
                    return {**row, "result": "node error: not found after conflict"}
                existing_nodes[label] = node
            node_id = str(node.get("id", ""))

        parts = []
        parts.append("profile created" if created_profile else "profile reused")
        parts.append("node created" if created_node else "node reused")
        return {**row, "profile_id": profile_id, "node_id": node_id, "result": ", ".join(parts)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        done = list(pool.map(one, rows))
    return done


def run_tests(args: argparse.Namespace, rows: list[dict], client: Client) -> None:

    def test(row: dict) -> dict:
        node_id = row.get("node_id")
        if not node_id:
            return {**row, "exit_ip": "", "status": "skipped"}
        for attempt in (1, 2):
            status, body = client.request("POST", f"/api/admin/v1/egress-nodes/{node_id}/test",
                                          timeout=180)
            if status == 200:
                data = body.get("data", {})
                return {**row, "exit_ip": data.get("exitIp", ""),
                        "status": data.get("status", ""), "latency_ms": data.get("latencyMs")}
            time.sleep(2 * attempt)
        return {**row, "exit_ip": "", "status": f"http {status}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        return list(pool.map(test, rows))


def summary_report(rows: list[dict]) -> tuple[str, dict]:
    from collections import Counter
    tested = [r for r in rows if r.get("exit_ip")]
    ipc = Counter(r["exit_ip"] for r in tested)
    dups = {ip: c for ip, c in ipc.items() if c > 1}
    n_use = len(rows)
    lines = [
        "# Direct-egress report",
        "",
        f"- sessions: {n_use}",
        f"- tested ok: {len(tested)} / {n_use}",
        f"- unique exit IPs: {len(ipc)}; duplicate-IP ports: {sum(dups.values())} in {len(dups)} groups",
        f"- lab-like (use>=3): {n_use >= 3}",
        "",
        "## Node → exit IP",
        "",
    ]
    for r in rows:
        ip = r.get("exit_ip") or "-"
        extra = f" latency={r.get('latency_ms')}ms" if r.get("latency_ms") else ""
        lines.append(f"- {r['label']} (node {r.get('node_id', '-')}): {ip} [{r.get('status', '')}]{extra}")
    if dups:
        lines.extend(["", "## Duplicate exit IPs (same failure domain)", ""])
        for ip, c in sorted(dups.items(), key=lambda x: -x[1]):
            names = [r["label"] for r in tested if r["exit_ip"] == ip]
            lines.append(f"- {ip} x{c}: {', '.join(names)}")
    data = {
        "sessions": n_use,
        "tested_ok": len(tested),
        "unique_exit_ips": len(ipc),
        "lab_like": n_use >= 3,
        "nodes": [{"name": r["label"], "node_id": r.get("node_id"), "exit_ip": r.get("exit_ip"),
                   "status": r.get("status"), "latency_ms": r.get("latency_ms")} for r in rows],
    }
    return "\n".join(lines) + "\n", data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", help="file with one proxy per line (same formats as from_residential.py)")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=os.environ.get("GROK2API_ADMIN_PASSWORD", ""))
    parser.add_argument("--password-file", default="")
    parser.add_argument("--scope", default="grok_build")
    parser.add_argument("--account-capacity", type=int, default=None)
    parser.add_argument("--out-dir", default="egress-direct")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--skip-tests", action="store_true", help="provision only; do not run egress tests")
    parser.add_argument("--dry-run", action="store_true", help="parse + write plan only; no API calls")
    args = parser.parse_args(argv)

    if args.password_file and not args.password:
        args.password = Path(args.password_file).read_text(encoding="utf-8").strip()
    if not args.dry_run and not args.password:
        raise SystemExit("need --password, --password-file, or GROK2API_ADMIN_PASSWORD")

    sessions = parse_dump(Path(args.dump).read_text(encoding="utf-8"))
    rows = []
    for seq, session in enumerate(sessions, start=1):
        url = f"{session['scheme']}://{session['username']}:{session['password']}@{session['host']}:{session['port']}"
        rows.append({"label": fallback_label(session, seq), "session": session,
                     "proxy_url": url, "url_for_mask": f"{session['username']}:***@{session['host']}:{session['port']}"})

    out = Path(args.out_dir)
    write_private(out / "direct-plan.md", plan_report(rows))

    if args.dry_run:
        print(f"dry-run: parsed {len(rows)} sessions; plan at {out / 'direct-plan.md'}")
        return 0

    client = Client(args.api_base)
    client.token = client.login(args.username, args.password)

    done = provision(args, rows, client)
    errors = [r for r in done if "error" in r.get("result", "")]
    print(f"provisioned: {len(done) - len(errors)} ok, {len(errors)} errors")

    if not args.skip_tests:
        done = run_tests(args, done, client)

    report, data = summary_report(done)
    write_private(out / "direct-report.md", report)
    write_private(out / "exit-ips.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"report: {out / 'direct-report.md'}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
