import assert from 'node:assert/strict';
import { frameSnapshot } from '../static/js/board-data.js';
const snapshot = frameSnapshot({ data: '<script>untrusted text</script>', format: 'text', token: 'secret',
  script: 'private config', html_template: '<p>template</p>', state: 'active', fresh: true },
  { status: 'collector_offline', fresh: false });
assert.equal(snapshot.data, '<script>untrusted text</script>');
assert.equal(snapshot.fresh, false);
assert.equal(snapshot.status, 'collector_offline');
for (const name of ['token', 'script', 'html_template']) assert.ok(!(name in snapshot));
console.log('Canvas fixed data contract passed');
