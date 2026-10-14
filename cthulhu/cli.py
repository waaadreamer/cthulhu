from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path


def load(path: str) -> dict:
    cfg = json.loads(Path(path).read_text())
    cfg["_path"] = str(Path(path).resolve())
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(prog="cthulhu")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("temple", help="run the central temple server")
    s.add_argument("--config", required=True)
    s = sub.add_parser("galaxy", help="run a galaxy server")
    s.add_argument("--config", required=True)
    s = sub.add_parser("agent", help="run one agent (started by a galaxy)")
    s.add_argument("--galaxy-config", required=True)
    s.add_argument("--launch", required=True)
    s = sub.add_parser("provision", help="register a new galaxy and print its one-line install command")
    s.add_argument("--config", required=True, help="temple config")
    s.add_argument("--id", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--no-spawn", action="store_true", help="galaxy refuses old_ones")
    for name, what in (("stop", "force-stop an agent now (aborts its turn; rejects messages until resumed)"),
                       ("resume", "resume a force-stopped agent")):
        s = sub.add_parser(name, help=what + "; local maintenance access")
        s.add_argument("--config", default="/etc/cthulhu/temple.json", help="local temple config")
        s.add_argument("agent_id")
    s = sub.add_parser("passwd", help="set the owner web password")
    s.add_argument("--config", required=True)
    s.add_argument("username")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)

    if args.cmd == "temple":
        import uvicorn
        from .temple import Temple
        cfg = load(args.config)
        temple = Temple(cfg)
        host, port = cfg.get("listen", ["127.0.0.1", 7500])
        uvicorn.run(temple.app(), host=host, port=port, log_level="warning", ws_max_size=64 * 1024 * 1024,
                    proxy_headers=True, ws_ping_interval=20, ws_ping_timeout=60)
    elif args.cmd == "galaxy":
        from .deploy import guard_startup
        guard_startup(args.config)
        from .galaxy import Galaxy
        asyncio.run(Galaxy(load(args.config)).main())
    elif args.cmd == "agent":
        from .gods import CLASSES
        from .roles import Role
        cfg = load(args.galaxy_config)
        launch = json.loads(args.launch)
        launch["token"] = os.environ.pop("CTHULHU_AGENT_TOKEN")
        role = Role(launch["role"])
        models = cfg.get("models", {})
        kw = {}
        agent = CLASSES[role](cfg["temple_ws"], launch, cfg["codex"], cfg.get("cwd") or str(Path.home()),
                              Path(cfg["state"]) / launch["id"], model=models.get(role.value, cfg.get("model")),
                              effort=cfg.get("effort"), runtime=cfg.get("runtimes", {}).get(role.value),
                              tls_pin=cfg.get("tls_pin"), **kw)
        asyncio.run(agent.main())
    elif args.cmd == "provision":
        import base64
        import ssl
        cfg = json.loads(Path(args.config).read_text())
        if any(g["id"] == args.id for g in cfg["galaxies"]):
            raise SystemExit(f"galaxy {args.id} already exists")
        public = cfg.get("public")
        if not public:
            raise SystemExit('temple config needs "public": {"ws": "wss://host:port/ws", "cert": "/path/cert.pem", "repo": "owner/name"}')
        token = secrets.token_hex(32)
        g = {"id": args.id, "name": args.name, "token": token}
        if args.no_spawn:
            g["spawn"] = False
        cfg["galaxies"].append(g)
        Path(args.config).write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        der = ssl.PEM_cert_to_DER_cert(Path(public["cert"]).read_text())
        # galaxy_defaults: config every new galaxy of this temple starts with (e.g. galaxy_ai).
        galaxy = cfg.get("galaxy_defaults", {}) | {"id": args.id, "name": args.name, "token": token, "temple_ws": public["ws"],
                                                  "tls_pin": hashlib.sha256(der).hexdigest()}
        blob = base64.urlsafe_b64encode(json.dumps(galaxy).encode()).decode()
        ref = public.get("ref", "main")
        env = " ".join(f"{k}={v}" for k, v in (public.get("install_env") or {}).items())
        print(f"galaxy {args.id} registered; restart the temple to accept it.\n")
        print("Run on the target machine, as the user the agents should run as (no root needed):\n")
        print(f"curl -fsSL https://raw.githubusercontent.com/{public['repo']}/{ref}/deploy/install-galaxy.sh | "
              f"{env + ' ' if env else ''}CTHULHU_REF={ref} bash -s -- {blob}")
    elif args.cmd in ("stop", "resume"):
        from .ops import call
        r = asyncio.run(call(args.config, f"maintenance_{args.cmd}", agent_id=args.agent_id))
        print(f"{r['name']} ({r['id']}): {r['desired']}")
    elif args.cmd == "passwd":
        import getpass
        from .store import Store
        from .temple import password_hash
        cfg = load(args.config)
        pw = getpass.getpass("password: ")
        salt = secrets.token_hex(16)
        Store(Path(cfg["db"])).set_account(args.username, salt, password_hash(pw, salt))
        print("ok")
