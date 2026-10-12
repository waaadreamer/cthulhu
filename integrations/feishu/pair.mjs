// One foreground owner-pairing capture using the bridge's existing official
// channel and encrypted-secret protocol. No model, reply, history or daemon.
import {randomBytes} from 'node:crypto';
import {execFile} from 'node:child_process';
import {readFile, writeFile, stat} from 'node:fs/promises';
import {resolve, join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {parseArgs} from 'node:util';

const quiet = Object.fromEntries(['trace', 'debug', 'info', 'warn', 'error'].map(k => [k, () => {}]));

export function candidate(msg, phrase, start, end, now = Date.now()) {
  const raw = msg.raw;
  if (now < start || now > end || msg.createTime < start || msg.createTime > now ||
      !Number.isFinite(msg.createTime) || msg.chatType !== 'p2p' || msg.senderType !== 'user' ||
      msg.rawContentType !== 'text' || msg.threadId || msg.content !== phrase ||
      !raw?.sender?.sender_id?.open_id || raw.sender.sender_id.open_id !== msg.senderId ||
      raw.sender.sender_type !== 'user' || raw.message?.message_type !== 'text' ||
      raw.message?.chat_id !== msg.chatId || raw.message?.message_id !== msg.messageId ||
      !msg.chatId || !msg.messageId || raw.message?.chat_type !== 'p2p') return null;
  let content;
  try { content = JSON.parse(raw.message.content); } catch { return null; }
  if (content.text !== phrase || Number(raw.message.create_time) !== msg.createTime) return null;
  return {sender_id: msg.senderId, chat_id: msg.chatId, message_id: msg.messageId,
          event_at: msg.createTime};
}

export async function capture(channel, {appId, output, ready, ttl = 3600000, settle = 5000}) {
  let start, end, phrase, timer, settling, finished = false;
  const matches = new Map();
  let finish;
  const completed = new Promise((resolveDone, rejectDone) => {
    finish = async (error) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer); clearTimeout(settling);
      try {
        await channel.disconnect();
        if (error) throw error;
        const identities = new Set([...matches.values()].map(m => m.sender_id + ':' + m.chat_id));
        if (identities.size !== 1) throw new Error('pairing ambiguous');
        const match = [...matches.values()][0];
        const evidence = {app_id: appId, ...match, ready_at: start, expires_at: end,
          verified_by: 'owner-console-challenge:' + match.message_id,
          legacy_scope_id: match.chat_id}; // Existing bridge p2p scope is chatId.
        await writeFile(output, JSON.stringify(evidence, null, 2) + '\n', {flag: 'wx', mode: 0o600});
        resolveDone(evidence);
      } catch (e) { rejectDone(e); }
    };
  });
  // A signal can arrive while connect() is still pending.
  completed.catch(() => {});
  // Install handlers before connecting; pre-ready traffic cannot match.
  channel.on('message', msg => {
    if (!start || finished) return;
    const match = candidate(msg, phrase, start, end);
    if (!match) return;
    matches.set(match.sender_id + ':' + match.chat_id + ':' + match.message_id, match);
    if (!settling) settling = setTimeout(() => void finish(), settle);
  });
  channel.on('error', () => { if (start) void finish(new Error('pairing transport failed')); });
  const stop = () => void finish(new Error('pairing interrupted'));
  process.once('SIGTERM', stop); process.once('SIGINT', stop);
  try {
    await channel.connect(); // SDK resolves only after successful WS handshake.
    if (finished) return await completed;
    start = Date.now(); end = start + ttl;
    phrase = '奈亚配对 ' + randomBytes(24).toString('hex');
    timer = setTimeout(() => void finish(new Error('pairing expired')), ttl);
    await ready({state: 'ready', phrase, ready_at: start, expires_at: end});
    return await completed;
  } finally {
    clearTimeout(timer); clearTimeout(settling);
    process.removeListener('SIGTERM', stop); process.removeListener('SIGINT', stop);
    await channel.disconnect();
  }
}

function secret(node, bridge, appId, env) {
  return new Promise((resolveSecret, reject) => {
    const child = execFile(node, [bridge, 'secrets', 'get'], {env, maxBuffer: 65536, timeout: 10000},
      (error, stdout) => {
        if (error) return reject(new Error('official secret provider failed'));
        try {
          const value = JSON.parse(stdout).values?.['app-' + appId];
          if (typeof value !== 'string' || !value) throw new Error('missing secret');
          resolveSecret(value);
        } catch { reject(new Error('official secret provider failed')); }
      });
    child.stdin.end(JSON.stringify({protocolVersion: 1, ids: ['app-' + appId]}));
  });
}

export async function main() {
  const {values} = parseArgs({options: {'root': {type:'string'}, 'install-root': {type:'string'},
    'stop-receipt': {type:'string'}, 'profile': {type:'string', default:'nyarl'}}});
  if (!values.root || !values['install-root'] || !values['stop-receipt'] ||
      !/^[a-zA-Z0-9_-]+$/.test(values.profile)) throw new Error('paths required');
  const root = resolve(values.root), install = resolve(values['install-root']);
  const info = await stat(root);
  if (info.uid !== process.getuid() || info.mode & 0o077) throw new Error('private service-owned root required');
  const disabled = JSON.parse(await readFile(join(root, 'temple-feishu.disabled.json'))).feishu;
  if (!disabled || disabled.enabled || disabled.worker?.enabled) throw new Error('normal route must remain disabled');
  const profile = JSON.parse(await readFile(join(root, 'config.json'))).profiles?.[values.profile];
  const app = profile?.accounts?.app;
  if (!app?.id || !['feishu', 'lark'].includes(app.tenant)) throw new Error('original app configuration required');
  const receipt = JSON.parse(await readFile(values['stop-receipt']));
  if (receipt.app_id !== app.id || receipt.listener_stopped !== true || receipt.refresh_stopped !== true ||
      !receipt.verified_by || !receipt.task_id || !Number.isFinite(receipt.stopped_at) || receipt.stopped_at > Date.now())
    throw new Error('verified old listener and refresh stop receipt required');
  const output = join(root, 'pairing-evidence.json');
  try { await stat(output); throw new Error('existing pairing evidence must be reconciled first'); }
  catch (e) { if (e.code !== 'ENOENT') throw e; }
  const node = join(install, 'node_modules/node/bin/node');
  const bridge = join(install, 'node_modules/lark-channel-bridge/dist/cli.js');
  const env = {...process.env, LARK_CHANNEL_HOME: root, LARK_CHANNEL_PROFILE: values.profile,
    LARK_CHANNEL_CONFIG: join(root, 'config.json')};
  const appSecret = await secret(node, bridge, app.id, env);
  const {createLarkChannel} = await import(pathToFileURL(join(install, 'node_modules/@larksuite/channel/dist/index.mjs')));
  const lockfile = (await import(pathToFileURL(join(install, 'node_modules/proper-lockfile/index.js')))).default;
  const release = await lockfile.lock(root, {lockfilePath: join(root, 'pairing.lock'), retries: 0});
  try {
    const channel = createLarkChannel({appId: app.id, appSecret,
      domain: app.tenant === 'lark' ? 'https://open.larksuite.com' : 'https://open.feishu.cn',
      source: 'cthulhu-owner-pairing', logger: quiet, includeRawEvent: true,
      policy: {dmMode: 'open', requireMention: false}, handshakeTimeoutMs: 10000, httpTimeoutMs: 30000});
    try { await capture(channel, {appId: app.id, output,
      ready: async value => { await writeFile(join(root, 'pairing-ready.json'), JSON.stringify(value) + '\n', {mode:0o600});
                             console.log(JSON.stringify(value)); }});
    } finally { await writeFile(join(root, 'pairing-ready.json'), JSON.stringify({state:'closed', closed_at:Date.now()}) + '\n', {mode:0o600}); }
    console.log(JSON.stringify({state:'paired', evidence:output, normal_route_enabled:false}));
  } finally { await release(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(() => { console.error('Pairing did not complete; check prerequisites and private local receipt.'); process.exitCode = 1; });
}
