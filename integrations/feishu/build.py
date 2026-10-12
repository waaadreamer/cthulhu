"""Adapt the pinned bridge source at its real execution boundary.

The generated artifact retains upstream transport/catalog/publishers and removes
both local model adapters. Unknown source layouts fail closed, not approximate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def replace_once(s, before, after):
    if s.count(before) != 1:
        raise ValueError('upstream integration anchor changed: ' + before[:100])
    return s.replace(before, after, 1)


def custom_source(source):
    """Apply only the reviewed delta to the exact public dependency, offline."""
    upstream = Path(__file__).with_name('upstream')
    manifest = json.loads((upstream / 'manifest.json').read_text())
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest == manifest['patched_cli_sha256']:
        return data.decode()
    if digest != manifest['base_cli_sha256']:
        raise ValueError('bridge source does not match the reviewed manifest')
    patch = upstream / 'bridge-runtime.patch'
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest['patch_sha256']:
        raise ValueError('bridge delta does not match the reviewed manifest')
    with tempfile.TemporaryDirectory() as tmp:
        dist = Path(tmp) / 'dist'
        dist.mkdir()
        (dist / 'cli.js').write_bytes(data)
        subprocess.run(['git', 'apply', str(patch.resolve())], cwd=tmp,
                       check=True, capture_output=True)
        data = (dist / 'cli.js').read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest['patched_cli_sha256']:
        raise ValueError('patched bridge does not match the reviewed manifest')
    return data.decode()


def build(source, target):
    s = custom_source(source)
    a = s.index('// src/agent/claude/adapter.ts')
    b = s.index('// src/runtime/errors.ts', a)
    s = s[:a] + 'var CLAUDE_DEFAULT_PERMISSION_MODE = "bypassPermissions";\n' + s[b:]
    a = s.index('// src/agent/codex/argv.ts')
    # The following channel module owns imports used later by startChannel.
    # esbuild interleaves modules; capability is already past those imports.
    b = s.index('// src/bot/channel.ts', a)
    s = s[:a] + s[b:]
    a = s.index('function createRuntimeAgent(')
    b = s.index('async function checkRuntimeAgentAvailability(', a)
    s = s[:a] + 'function createRuntimeAgent() { return new CodexAdapter(); }\n' + s[b:]
    s = s.replace('#!/usr/bin/env node\n', '')
    s = ('import {CodexAdapter, authorizeMessage, sourceEvent, finalReply as nyarlFinalReply, abandonReply} from "./adapter.mjs";\n' + s)
    s = replace_once(s, 'message: async (msg) => {', 'message: async (msg) => {\n      try { await authorizeMessage(msg, cfg.accounts.app.id); } catch { return; }')
    s = replace_once(s, 'cardAction: async (evt) => {', 'cardAction: async (evt) => {\n      try { await authorizeMessage({senderId: evt.operator?.openId, chatId: evt.chatId, messageId: evt.messageId}, cfg.accounts.app.id); } catch { return; }')
    a = s.index('    comment: async (evt) => {')
    b = s.index('    reconnecting: () => {', a)
    s = s[:a] + '    comment: async () => {}, // Historical external scopes remain dormant.\n' + s[b:]
    s = replace_once(s, '  const meetingConfig = () => controls.profileConfig.meeting;',
                     '  const meetingConfig = () => ({...controls.profileConfig.meeting, enabled: false});')
    a = s.index('  const ownerRefresh = createOwnerRefreshController({')
    b = s.index('  const identity = channel.botIdentity;', a)
    s = s[:a] + s[b:]  # Verified temple binding replaces creator/whole-chat discovery.
    s = replace_once(s, '      ownerRefresh.stop();\n      knownChatsRefresh.stop();\n', '')
    # Each platform message keeps its own unique durable event correlation.
    s = replace_once(s, 'await runAgentBatch({', 'for (const singleMessage of batch) await runAgentBatch({')
    s = replace_once(s, '          batch,\n          controls,', '          batch: [singleMessage],\n          controls,')
    s = replace_once(s, '      policy,\n      sessionId,', '      policy,\n      templeEvent: input.scope.templeEvent,\n      templeText: input.scope.templeText,\n      sessionId,')
    s = replace_once(s, '      prompt: input.policy.prompt,', '      prompt: input.policy.prompt,\n      templeEvent: input.templeEvent,\n      templeText: input.templeText,')
    s = replace_once(s, '    actorId: firstMsg.senderId,', '    actorId: firstMsg.senderId,\n    templeEvent: sourceEvent(firstMsg, controls.cfg.accounts.app.id),\n    templeText: firstMsg.content,')
    s = replace_once(s, '    activePolicyFingerprints.delete(scope);',
                     '    await abandonReply({sendOpts}).catch(() => {});\n    activePolicyFingerprints.delete(scope);')
    # Owner natural-language operations run through Nyarl, not the old bridge's
    # profile/admin/model control plane. Cancellation stays in existing callbacks.
    s = replace_once(s, 'async function tryHandleCommand(', 'async function legacyHandleCommand(')
    s += '\nasync function tryHandleCommand(ctx) { return ctx.msg.content.trim() === "/stop" ? legacyHandleCommand(ctx) : false; }\n'
    a = s.index('async function sendFinalReply(input) {')
    b = s.index('function requireMessageReceipt(', a)
    s = s[:a] + '''async function sendFinalReply(input) {
      const body = renderText(input.state);
      if (!body.trim()) return nyarlFinalReply(input, null);
      return nyarlFinalReply(input, () => input.channel.send(input.chatId,
        input.replyMode === 'card' ? {card: renderCard(input.state, input.cardRenderOptions)} : {markdown: body}, input.sendOpts));
    }
    ''' + s[b:]
    assert 'buildCodexArgs' not in s and 'var ClaudeAdapter' not in s and 'new ClaudeAdapter' not in s
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(s)
    shutil.copyfile(Path(__file__).with_name('adapter.mjs'), target.with_name('adapter.mjs'))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('source', type=Path)
    p.add_argument('target', type=Path)
    args = p.parse_args()
    build(args.source, args.target)
