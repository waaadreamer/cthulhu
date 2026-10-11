"""Local, narrowly scoped maintenance client. Never impersonates an agent/galaxy."""
import argparse
import asyncio
import json
from pathlib import Path

from .protocol import Link, maintenance_token


async def connect(config_path):
    cfg = json.loads(Path(config_path).read_text())
    port = cfg.get("listen", ["127.0.0.1", 7500])[1]

    async def ignore(_):
        pass
    link = Link(f"ws://127.0.0.1:{port}/ws",
                {"kind": "maintenance", "token": maintenance_token(cfg["secret"])}, ignore, ignore)
    task = asyncio.create_task(link.run())
    await asyncio.wait_for(link.connected.wait(), 10)
    return link, task


async def main(args):
    link, task = await connect(args.config)
    try:
        if args.action == "status":
            result = await link.call("maintenance_snapshot", timeout=15)
        else:
            result = await link.call("maintenance_drain", galaxy=args.galaxy,
                                     **({"enabled": args.action == "drain"} if args.action != "check" else {}),
                                     timeout=90)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, help="local temple config; never copied")
    p.add_argument("action", choices=["status", "drain", "check", "resume"])
    p.add_argument("--galaxy")
    args = p.parse_args()
    if args.action != "status" and not args.galaxy:
        p.error("--galaxy is required")
    asyncio.run(main(args))
