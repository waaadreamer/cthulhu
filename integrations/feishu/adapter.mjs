import net from 'node:net';
import readline from 'node:readline';

export function connect(method, params) {
  const socket = net.createConnection(process.env.CTHULHU_FEISHU_SOCKET);
  const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
  socket.on('connect', () => socket.write(JSON.stringify({method, params}) + '\n'));
  // The iterator surfaces connection errors; do not leak request bodies.
  socket.on('error', () => lines.close());
  return {socket, lines};
}
export async function rpc(method, params) {
  const {socket, lines} = connect(method, params);
  try {
    for await (const line of lines) {
      const reply = JSON.parse(line);
      if (reply.error) throw new Error(reply.error);
      if ('result' in reply) return reply.result;
    }
    throw new Error('Nyarl connection closed before acknowledgement');
  } finally { socket.destroy(); }
}
export function sourceEvent(msg, appId) {
  return {app_id: appId, event_id: msg.messageId, sender_id: msg.senderId,
    chat_id: msg.chatId, topic_id: msg.threadId || ''};
}
export async function authorizeMessage(msg, appId) {
  await rpc('authorize', sourceEvent(msg, appId));
}
// Only live correlations, not a second session catalog or durable queue.
const replies = new Map();
export class CodexAdapter {
  id = 'codex'; // Keep the existing catalog namespace, not a local Codex engine.
  displayName = '奈亚拉托提普';
  setBotIdentity() {} // Platform display name never replaces harness identity.
  async isAvailable() {
    if (!process.env.CTHULHU_FEISHU_SOCKET) return false;
    return Boolean((await rpc('health', {})).ready);
  }
  async checkAvailability() { return {ok: await this.isAvailable()}; }
  async prepareRun(opts) {
    if (!opts.templeEvent) throw new Error('This scope has no verified owner event');
    await rpc('authorize', opts.templeEvent);
  }
  run(opts) {
    let resolveExit;
    const exited = new Promise(resolve => {resolveExit = resolve;});
    const {socket, lines} = connect('run', {...opts, event: opts.templeEvent, text: opts.templeText});
    const key = opts.templeEvent.event_id;
    const events = {
      async *[Symbol.asyncIterator]() {
        let terminal = false;
        try {
          for await (const line of lines) {
            const wire = JSON.parse(line);
            if (wire.error) throw new Error(wire.error);
            const evt = wire.event;
            if (!evt) continue;
            if (evt.type === 'correlation') { replies.set(key, {message_id: evt.message_id}); continue; }
            if (evt.type === 'duplicate') {
              replies.set(key, {duplicate: true});
              terminal = true;
              yield {type: 'done', terminationReason: 'interrupted'};
              continue;
            }
            if (evt.type === 'done' || evt.type === 'error') terminal = true;
            yield evt;
          }
          if (!terminal) yield {type:'error', message:'奈亚连接中断；未自动重放操作', terminationReason:'failed'};
        } catch {
          yield {type:'error', message:'奈亚连接中断；未自动重放操作', terminationReason:'failed'};
        } finally { socket.destroy(); resolveExit(true); }
      }
    };
    return {runId: opts.runId, events,
      stop: () => rpc('cancel', {runId: opts.runId}),
      waitForExit: (timeoutMs) => Promise.race([exited, new Promise(resolve => {
        const t = setTimeout(() => resolve(false), timeoutMs); t.unref();
      })])};
  }
}
export async function finalReply(input, send) {
  const key = input.sendOpts?.replyTo;
  const correlation = replies.get(key);
  if (!correlation || correlation.duplicate) return;
  if (!send) {
    await rpc('receipt', {message_id:correlation.message_id, receipt:{state:'failed'}});
    replies.delete(key);
    return;
  }
  // Persist sending before the platform side effect. Lost ACK is ambiguous.
  await rpc('receipt', {message_id:correlation.message_id, receipt:{state:'sending'}});
  let result;
  try {
    result = await send();
    if (!result?.messageId) throw new Error('No platform receipt');
  } catch (err) {
    await rpc('receipt', {message_id:correlation.message_id, receipt:{state:'ambiguous'}}).catch(() => {});
    replies.delete(key);
    throw err;
  }
  await rpc('receipt', {message_id:correlation.message_id,
    receipt:{state:'sent', message_id: result.messageId}});
  replies.delete(key);
  return result;
}

// The upstream renderer may fail before finalReply is reached. Always settle
// its live correlation so an abandoned publisher cannot deadlock drain.
export async function abandonReply(input) {
  const key = input.sendOpts?.replyTo;
  const correlation = replies.get(key);
  if (!correlation || correlation.duplicate) { replies.delete(key); return; }
  try {
    await rpc('receipt', {message_id:correlation.message_id, receipt:{state:'ambiguous'}});
  } finally { replies.delete(key); }
}
