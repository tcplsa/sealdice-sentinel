// ==UserScript==
// @name         Sentinel 回复计时
// @author       SealDice Sentinel
// @version      0.7.1
// @description  已停用发送钩子：避免海豹 1.6.1 的 JavaScript 发送成功回调重入死锁。协议代理负责发送计时。
// @license      MIT
// ==/UserScript==
(function () {
  'use strict';
  // SealDice 1.6.1 calls JS OnMessageSend through callWithJsCheck: it queues the
  // callback on the same event loop and waits synchronously. A JS plugin's native
  // reply already occupies that loop, so even an empty callback can deadlock.
  // Do not register any hook here. A compatibility marker keeps coverage honest.
  console.log('SENTINEL_REPLY {"v":1,"event":"coverage_limited"}');
})();
