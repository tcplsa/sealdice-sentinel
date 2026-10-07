const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('scripts/sentinel-reply-observer.js', 'utf8');
let time = 1000, extension, timerID = 0;
const logs = [], timers = new Map();
const sandbox = {
  Date: {now: () => time}, Math,
  console: {log: line => logs.push(JSON.parse(line.slice('SENTINEL_REPLY '.length)))},
  setTimeout: (callback, delay) => { const id=++timerID;timers.set(id,{callback,at:time+delay});return id; },
  clearTimeout: id => timers.delete(id),
  seal: {ext: {find: () => undefined,new: () => ({}),register: value => {extension=value;}}},
};
vm.runInNewContext(source, sandbox);
function context(account='QQ:2325552935') {
  return {endPoint:{userId:account},isCurGroupBotOn:true,commandID:0};
}
function receive(ctx,text='.r') { extension.onMessageReceived(ctx,{messageType:'group',message:text}); }
function advance(ms) {
  time+=ms;
  for (const [id,timer] of [...timers]) if(timer.at<=time){timers.delete(id);timer.callback();}
}
const one=context(); receive(one); one.commandID=1; advance(3500); extension.onMessageSend(one);
assert.equal(logs.at(-1).event,'reply_api_completed');assert.equal(logs.at(-1).duration_ms,3500);
extension.onMessageSend(one); assert.equal(logs.filter(x=>x.event==='reply_api_completed').length,1);
const a=context(),b=context(); receive(a);a.commandID=2;advance(10);receive(b);b.commandID=3;
advance(200);extension.onMessageSend(b);advance(100);extension.onMessageSend(a);
assert.deepEqual(logs.filter(x=>x.event==='reply_api_completed').slice(-2).map(x=>x.duration_ms),[200,310]);
const silent=context();receive(silent,'.unknown');silent.commandID=4;advance(20);extension.onCommandReceived(silent);
const waitingBefore=logs.filter(x=>x.event==='reply_waiting').length;advance(31000);
assert.equal(logs.filter(x=>x.event==='reply_waiting').length,waitingBefore);
const slow=context();receive(slow);slow.commandID=5;advance(10001);
assert.equal(logs.at(-1).event,'reply_waiting');advance(10);extension.onMessageSend(slow);
assert.equal(logs.at(-1).event,'reply_api_completed');
const disabled=context();disabled.isCurGroupBotOn=false;const logCount=logs.length;receive(disabled);
assert.equal(logs.length,logCount);
const unmatched=context();receive(unmatched);const copy={...unmatched};extension.onMessageSend(copy);
assert.equal(logs.length,logCount);extension.onCommandReceived(unmatched);
advance(130000);assert.equal(timers.size,0);
assert(!JSON.stringify(logs).includes('.unknown'));assert(!JSON.stringify(logs).includes('message'));
console.log('Reply observer pairing, latency, silent commands, duplicate callbacks and cleanup passed.');
