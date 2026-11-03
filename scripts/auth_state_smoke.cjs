// Exercise the same browser API helper with controlled network response ordering.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/app.js','utf8');
const helper = source.slice(source.indexOf('async function api('),source.indexOf('function showLogin()'));
const response = (status = 200) => ({ok:status === 200,status,json:async()=>({detail:'authentication required',ok:status === 200})});
function client(fetch) {
  let logins = 0;
  const sandbox = {fetch,showLogin:()=>{logins++;},bootstrap:()=>{}};
  vm.createContext(sandbox);
  vm.runInContext(`let authEpoch=0, authChangesInFlight=0; ${helper}; globalThis.api=api;`,sandbox);
  return {api:sandbox.api,logins:()=>logins};
}
async function main() {
  // A polling request arriving during TOTP session rotation must not open login.
  let completeEnable;
  const enable = new Promise(resolve=>{completeEnable=resolve;});
  const first = client(async url=>url.endsWith('/enable') ? enable : response(401));
  const enabling = first.api('/auth/totp/enable',{method:'POST'});
  const stale = await first.api('/state').catch(error=>error);
  assert.equal(first.logins(),0);
  assert.equal(stale.staleAuth,true);
  completeEnable(response());
  await enabling;
  // Real session expiration after rotation must still open login.
  const expired = await first.api('/state').catch(error=>error);
  assert.equal(first.logins(),1);
  assert.equal(expired.staleAuth,false);

  // An old poll that finishes after a password change cannot invalidate the new session.
  let completeOld;
  const oldResponse = new Promise(resolve=>{completeOld=resolve;});
  const second = client(async url=>url.endsWith('/state') ? oldResponse : response());
  const oldPoll = second.api('/state').catch(error=>error);
  await second.api('/auth/password',{method:'POST'});
  completeOld(response(401));
  assert.equal((await oldPoll).staleAuth,true);
  assert.equal(second.logins(),0);

  // A rejected security mutation must not hide a real authentication failure.
  const third = client(async()=>response(401));
  await third.api('/auth/totp',{method:'DELETE'}).catch(()=>{});
  assert.equal(third.logins(),1);
  await third.api('/state').catch(()=>{});
  assert.equal(third.logins(),2);
  console.log('Authentication response-order checks passed (rotation, delayed poll, real expiration)');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
