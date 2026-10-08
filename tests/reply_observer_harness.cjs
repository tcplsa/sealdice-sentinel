const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('scripts/sentinel-reply-observer.js', 'utf8');
const logs = [];
let registrations = 0;
const sandbox = {
  console: {log: line => logs.push(JSON.parse(line.slice('SENTINEL_REPLY '.length)))},
  seal: {ext: {
    find: () => undefined,
    new: () => ({}),
    register: () => {registrations++; throw new Error('Unsafe synchronous JS send hook registered');},
  }},
};
vm.runInNewContext(source, sandbox);
assert.equal(registrations, 0);
assert.deepEqual(logs, [{v:1,event:'coverage_limited'}]);
assert(!source.includes('ext.onMessageSend ='));
assert(!source.includes('reply_api_completed'));
console.log('Legacy observer registers no hooks and reports unavailable coverage.');
