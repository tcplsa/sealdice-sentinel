// ==UserScript==
// @name         Sentinel 回复计时
// @author       SealDice Sentinel
// @version      0.6.0
// @description  只记录指令进入消息钩子到发送 API 完成回调的时间，不记录消息正文或发送测试消息。
// @license      MIT
// ==/UserScript==
(function () {
  'use strict';
  const name = 'sentinel-reply-observer';
  let ext = seal.ext.find(name);
  if (ext) return;
  ext = seal.ext.new(name, 'SealDice Sentinel', '0.6.0');
  ext.autoActive = true;
  let pending = [];
  let sequence = 0;
  let lastLimited = 0;
  const run = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  const classes = {r: 'roll', ra: 'check', rc: 'check', coc: 'core', bot: 'core', help: 'core'};

  function account(ctx) {
    const value = String(ctx.endPoint && ctx.endPoint.userId || '');
    return /^(QQ|OpenQQ):[1-9][0-9]{4,19}$/.test(value) ? value : null;
  }
  function emit(event, entry, duration) {
    const value = {v: 1, event: event};
    if (entry) {
      value.trace_id = entry.trace;
      value.self_id = entry.self;
      value.command_class = entry.category;
      value.duration_ms = Math.max(0, Math.round(duration));
      value.synthetic = entry.synthetic;
    }
    console.log('SENTINEL_REPLY ' + JSON.stringify(value));
  }
  function prune(now) {
    pending = pending.filter(function (entry) {
      if (now - entry.started > 120000 || now < entry.started) {
        clearTimeout(entry.timer);
        return false;
      }
      return true;
    });
    while (pending.length >= 64) {
      clearTimeout(pending.shift().timer);
      if (now - lastLimited >= 60000) { emit('coverage_limited'); lastLimited = now; }
    }
  }
  function matches(ctx) {
    const self = account(ctx);
    if (!self) return [];
    // Goja preserves native Go-pointer equality across callback argument wrappers.
    // Verified against SealDice 1.6.1 without invoking an actual QQ send. Never
    // guess by group/user/FIFO: concurrent requests and context proxies can differ.
    return pending.filter(function (entry) {
      return entry.self === self && entry.context === ctx;
    });
  }
  function expire(entry) {
    entry.timer = setTimeout(function () {
      const index = pending.indexOf(entry);
      if (index >= 0) pending.splice(index, 1);
    }, Math.max(1, 120000 - (Date.now() - entry.started)));
  }

  ext.onMessageReceived = function (ctx, msg) {
    const self = account(ctx);
    if (!self || (msg.messageType === 'group' && !ctx.isCurGroupBotOn)) return;
    const text = String(msg.message || '').replace(/^\s*\[CQ:at,[^\]]{1,128}\]\s*/, '').trim();
    const command = /^[.!。．/]([a-z]+|[\u4e00-\u9fff]+)/i.exec(text);
    const now = Date.now();
    prune(now);
    const category = command ? classes[command[1].toLowerCase()] || 'other' : 'chat';
    if (pending.some(function (entry) { return entry.context === ctx; })) return;
    const entry = {context: ctx, self: self, category: category, started: now,
      trace: run + '-' + (++sequence), synthetic: msg.platform === 'UI' || ctx.__sentinelSynthetic === true,
      timer: null};
    // A pending callback is a warning about unobserved progress, not a confirmed failure.
    if (command) {
      entry.timer = setTimeout(function () {
        if (pending.indexOf(entry) >= 0) {
          emit('reply_waiting', entry, Date.now() - entry.started);
          expire(entry);
        }
      }, category === 'other' ? 30000 : 10000);
    } else {
      expire(entry); // Ordinary conversation is not assumed to require a reply.
    }
    pending.push(entry);
  };
  ext.onMessageSend = function (ctx) {
    const found = matches(ctx);
    if (found.length !== 1) return; // Never guess a pairing under ambiguous contexts.
    const entry = found[0];
    clearTimeout(entry.timer);
    pending.splice(pending.indexOf(entry), 1);
    const duration = Date.now() - entry.started;
    if (duration >= 0 && duration <= 120000) emit('reply_api_completed', entry, duration);
  };
  ext.onCommandReceived = function (ctx) {
    // This hook runs AFTER solve in 1.6.1, and must not be used as a start timestamp.
    const found = matches(ctx);
    if (found.length !== 1) return;
    const entry = found[0];
    clearTimeout(entry.timer);
    // Silent/censored/unhandled commands are not automatically "missing replies".
    emit('command_finished_without_reply', entry, Date.now() - entry.started);
    expire(entry);
  };
  seal.ext.register(ext);
  emit('observer_loaded');
})();
