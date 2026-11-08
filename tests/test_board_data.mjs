import assert from 'node:assert/strict';
import { frameSnapshot } from '../static/js/board-data.js';
const snapshot = frameSnapshot({ data: '<script>untrusted text</script>', format: 'text', token: 'secret',
  script: 'private config', html_template: '<p>template</p>', state: 'active', fresh: true },
  { status: 'collector_offline', fresh: false });
assert.equal(snapshot.data, '<script>untrusted text</script>');
assert.equal(snapshot.fresh, false);
assert.equal(snapshot.status, 'collector_offline');
for (const name of ['token', 'script', 'html_template']) assert.ok(!(name in snapshot));
const oldData = { revision: 1, data_revision: 1, data: 'old', sampled_at: 10, fresh: true };
const mixed = frameSnapshot(oldData, { revision: 2, status: 'fresh', fresh: true, sampled_at: 20 });
assert.equal(mixed.fresh, false);
assert.equal(mixed.stale, true);
assert.equal(mixed.status, 'stale');
assert.equal(mixed.sampled_at, 10);
assert.equal(mixed.data, 'old');
assert.equal(frameSnapshot(oldData, { revision: 1, status: 'fresh', fresh: true }).fresh, true);
assert.equal(frameSnapshot({ ...oldData, revision: 2 }, { revision: 2, fresh: true }).fresh, false);
console.log('Canvas fixed data contract passed');
