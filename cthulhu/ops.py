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


async def call(config_path, method, timeout=30, **params):
    link, task = await connect(config_path)
    try:
        return await link.call(method, timeout=timeout, **params)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def main(args):
    link, task = await connect(args.config)
    try:
        if args.action == "status":
            result = await link.call("maintenance_snapshot", timeout=15)
        elif args.action == "runtime":
            result = await link.call("maintenance_runtime", agent_id=args.agent_id,
                                     config=json.loads(args.runtime_json), timeout=15)
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
    p.add_argument("action", choices=["status", "drain", "check", "resume", "runtime"])
    p.add_argument("--galaxy")
    p.add_argument("--agent-id")
    p.add_argument("--runtime-json", help="runtime config JSON, or null to restore the default")
    args = p.parse_args()
    if args.action in ("drain", "check", "resume") and not args.galaxy:
        p.error("--galaxy is required")
    if args.action == "runtime" and (not args.agent_id or args.runtime_json is None):
        p.error("runtime requires --agent-id and --runtime-json")
    asyncio.run(main(args))
