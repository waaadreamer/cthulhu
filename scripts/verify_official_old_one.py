"""Isolated real old_one / hub / broker / provider canary, with real upstream.

Run on the credential-owning host. Reads only api_provider and codex from a
local galaxy config; all WS links, state, tasks and agent processes are isolated.
No production registration, config mutation, fallback, or forced tool choice.
"""
import argparse
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cthulhu.galaxy import Galaxy
from cthulhu.temple import Temple
import uvicorn

ALIAS = "claude-opus-5-5-official"
UPSTREAM = "claude-opus-5-5"
RUNTIME = {"backend": "galaxy_api", "provider": "ali", "model": ALIAS}


@contextmanager
def no_signals():
    yield


async def until(check, seconds=60):
    async with asyncio.timeout(seconds):
        while True:
            value = check()
            if value:
                return value
            await asyncio.sleep(.2)


async def verify(config_path, output):
    local = json.loads(Path(config_path).read_text())
    cfg = dict(local["api_provider"])
    cfg["models"] = list(dict.fromkeys([*cfg["models"], ALIAS]))
    cfg["model_backends"] = cfg.get("model_backends", {}) | {ALIAS: "codex"}
    cfg["model_capabilities"] = cfg.get("model_capabilities", {}) | {ALIAS: {"web_search": False}}
    cfg["model_mapping"] = cfg.get("model_mapping", {}) | {ALIAS: UPSTREAM}
    cfg["model_labels"] = cfg.get("model_labels", {}) | {ALIAS: "LLM Official", UPSTREAM: "Claude Opus 5.5（中转站）"}
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {"status": "running", "runtime": RUNTIME, "requests": []}
    def save():
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    save()
    with tempfile.TemporaryDirectory(prefix="official-old-one-") as root:
        root = Path(root)
        work = root / "work"
        work.mkdir()
        artifact = work / "official-proof.txt"
        artifact.write_text("seed\n")
        nonce = secrets.token_hex(16)
        token = secrets.token_hex(32)
        temple = Temple({"secret": secrets.token_hex(32), "db": str(root / "temple.db"),
                         "galaxies": [{"id": "ali", "name": "isolated", "token": token}],
                         "old_one_runtime": RUNTIME,
                         "api_providers": {"ali": {"models": cfg["models"], "consumers": ["ali"]}}})
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        server = uvicorn.Server(uvicorn.Config(temple.app(), log_level="error", access_log=False, lifespan="off"))
        server.capture_signals = no_signals
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        galaxy = Galaxy({"id": "ali", "name": "isolated", "token": token,
                         "temple_ws": f"ws://127.0.0.1:{sock.getsockname()[1]}/ws",
                         "state": str(root / "galaxy"), "cwd": str(work), "codex": local["codex"],
                         "api_provider": cfg, "check_every": 3600})
        original = galaxy.provider.run
        async def observed(sid, body, generation, protocol="responses", beta=""):
            # Record only route and schema metadata, never prompts, headers or tokens.
            evidence["requests"].append({"model": body.get("model"), "protocol": protocol,
                "tool_choice": body.get("tool_choice"), "stream": body.get("stream"),
                "body_keys": sorted(body), "reasoning": body.get("reasoning"), "text": body.get("text"),
                "instructions_present": bool(body.get("instructions")),
                "instructions_length": len(body.get("instructions") or ""),
                "request_options": {k: body[k] for k in ("store", "max_output_tokens", "parallel_tool_calls") if k in body},
                "namespace_tools": [{"count": len(t.get("tools", [])),
                    "types": sorted({n.get("type", "") for n in t.get("tools", [])})}
                    for t in body.get("tools", []) if t.get("type") == "namespace"],
                "tool_types": sorted({t.get("type", "") for t in body.get("tools", [])}),
                "has_tool_output": any(i.get("type") == "function_call_output" for i in body.get("input", [])
                                       if isinstance(i, dict)), "has_previous_response_id": "previous_response_id" in body})
            save()
            return await original(sid, body, generation, protocol, beta)
        galaxy.provider.run = observed
        tasks = [serving, asyncio.create_task(galaxy.link.run()), asyncio.create_task(galaxy.hub.serve())]
        try:
            await until(lambda: temple.api_broker.catalog("ali") and galaxy.hub.port)
            catalog = temple.api_broker.catalog("ali")[0]
            assert catalog["model_backends"][ALIAS] == "codex"
            assert catalog["model_backends"][UPSTREAM] == "claude"
            evidence["catalog"] = {k: catalog[k] for k in ("id", "models", "model_backends", "model_labels", "model_capabilities")}
            assigned = await temple.new_task("ali", (
                f"隔离接入验证任务。工作目录 {work}。第一步登记探针：wc -c < {artifact}。"
                f"然后只在本目录使用 exec_command 将文件 {artifact} 的内容替换为精确字符串 {nonce}，"
                "再读回验证，最后调用 finish_task，evidence 写实际文件路径和读回内容。"
                "禁止联网、改其他目录、创建子agent或改配置；不需要研究代码。"),
                "文件内容匹配随机串，真实工具结果续接，finish_task提交。", cwd=str(work))
            aid, tid = assigned["agent_id"], assigned["task_id"]
            await until(lambda: galaxy.hub.store.task(tid)["state"] == "review", 360)
            await until(lambda: galaxy.hub.status.get(aid, {}).get("state") == "idle", 60)
            live = galaxy.hub.status[aid]["runtime"]
            assert live["config"] == RUNTIME, live
            assert not live["fallback_active"]
            assert live["api_result"]["requested_model"] == ALIAS
            assert live["api_result"]["upstream_model"] == UPSTREAM
            assert live["api_result"]["returned_model"] in (UPSTREAM, "anthropic/" + UPSTREAM)
            assert artifact.read_text().strip() == nonce
            requests = evidence["requests"]
            assert all("web_search" not in r["tool_types"] for r in requests)
            assert requests and all(r["model"] == ALIAS and r["protocol"] == "responses" and
                r["tool_choice"] == "auto" and r["stream"] and not r["has_previous_response_id"] for r in requests)
            assert any(r["has_tool_output"] for r in requests)
            rows = galaxy.hub.store.turn_log(aid)
            assert rows and rows[0]["status"] == "completed", rows
            proof = output.with_suffix(".proof.txt")
            proof.write_text(artifact.read_text())
            evidence.update(status="passed", agent_id=aid, task_id=tid, api_result=live["api_result"],
                            effective_runtime=live["config"], fallback_active=False, artifact=str(proof),
                            artifact_matches=True, turn_status=rows[0]["status"])
        except BaseException as error:
            evidence.update(status="failed", error_type=type(error).__name__)
            # Local logs can contain prompts; keep only runtime metadata in shared evidence.
            evidence["live"] = {aid: {"state": s.get("state"), "runtime": s.get("runtime")}
                                for aid, s in galaxy.hub.status.items()}
            raise
        finally:
            save()
            for aid in list(galaxy.procs):
                await galaxy.stop(aid)
            await galaxy.provider.disconnect()
            server.should_exit = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            temple.store.db.close()
            galaxy.hub.store.db.close()
    return evidence


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--galaxy-config", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    result = asyncio.run(verify(args.galaxy_config, args.output))
    print(json.dumps({"status": result["status"], "output": args.output}))
