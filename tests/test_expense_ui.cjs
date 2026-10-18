// Exercise the real inline client script without network access or a browser.
// Run: node --test tests/test_expense_ui.cjs
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, '../docs/team/index.html'), 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];

function backend() {
  const records = new Map(), calls = [];
  let mode = 'ok', release;
  const response = (data, status=200) => ({ok:status < 400,status,json:async()=>data});
  return {
    records, calls,
    set mode(value) {mode=value;},
    release() {release();},
    async fetch(url, options) {
      const endpoint = new URL(url).pathname;
      if (endpoint === '/api/me') return response({user:{id:7,first_name:'team'}});
      if (options.method === 'POST') {
        calls.push(options);
        if (mode === '400') return response({error:'invalid input'},400);
        if (mode === 'hold') await new Promise(resolve => {release=resolve;});
        const key=options.headers['Idempotency-Key'];
        if (!records.has(key)) records.set(key,{...JSON.parse(options.body),id:records.size+1,date:'18.10.2026 20:00',who:'team',notification_status:'pending'});
        if (mode === 'lost') throw new TypeError('response lost');
        return response(records.get(key),201);
      }
      if (options.method === 'PATCH') return response({ok:true});
      const items = [...records.values()];
      return response({period:'day',month_total:0,total:items.reduce((n,r)=>n+r.amount,0),count:items.length,by_category:{},pending:0,items});
    }
  };
}

async function client(api, storage=new Map(), storageBlocked=false) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      let value='';
      elements.set(id,{
        id,disabled:false,hidden:false,textContent:'',innerHTML:'',parentElement:{hidden:false},
        get value(){return value;},set value(v){value=String(v);},
        classList:{add:v=>classes.add(v),remove:v=>classes.delete(v),contains:v=>classes.has(v)},
        querySelectorAll:()=>[],addEventListener(){},focus(){}
      });
    }
    return elements.get(id);
  }
  const timers = new Map();
  let timerId=0;
  const sandbox = {
    console,URLSearchParams,AbortController,crypto:{randomUUID},
    location:{search:'?dev=offline'},window:{},
    document:{querySelector:element,querySelectorAll:()=>[]},
    localStorage:{
      getItem:key=>storage.get(key) || null,
      setItem(key,value){if(storageBlocked)throw new Error('blocked');storage.set(key,value);},
      removeItem:key=>storage.delete(key)
    },
    fetch:(...args)=>api.fetch(...args),
    setTimeout(fn){timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id)
  };
  vm.createContext(sandbox);
  // Remove only automatic boot so tests can await startup deterministically.
  vm.runInContext(script.replace(/boot\(\);\s*$/, '') + '\nthis.clientAPI={boot,save,openSheet,closeSheet};',sandbox);
  await sandbox.clientAPI.boot();
  return {
    ...sandbox.clientAPI,element,storage,
    fill(){this.openSheet(null);element('#f-amount').value='12,50';element('#f-text').value='Лаймы, 2 кг';element('#f-need').value='';}
  };
}

test('double click/Enter submits once and blocks close while saving',async()=>{
  const api=backend();api.mode='hold';
  const ui=await client(api);ui.fill();
  const first=ui.save();await ui.save();ui.closeSheet();
  assert.equal(api.calls.length,1);
  assert.equal(ui.element('#btn-save').disabled,true);
  assert.equal(ui.element('#sheet').classList.contains('open'),true);
  api.release();await first;
  assert.equal(api.records.size,1);
  assert.equal(ui.storage.size,0);
  assert.equal(ui.element('#toast').textContent,'Расход сохранён');
  assert.match(ui.element('#list').innerHTML,/уведомление в Telegram ожидает отправки/);
});

test('lost response retains immutable key/body across closing and reloading',async()=>{
  const api=backend();api.mode='lost';
  let ui=await client(api);ui.fill();await ui.save();
  assert.equal(api.records.size,1);
  assert.equal(ui.element('#btn-save').textContent,'Проверить сохранение');
  assert.equal(ui.element('#f-amount').disabled,true);
  ui.closeSheet();assert.equal(ui.element('#resume').hidden,false);
  ui=await client(api,ui.storage);
  assert.equal(ui.element('#sheet').classList.contains('open'),true);
  assert.equal(ui.element('#f-text').value,'Лаймы, 2 кг');
  api.mode='ok';await ui.save();
  assert.equal(api.records.size,1);
  assert.equal(api.calls[0].headers['Idempotency-Key'],api.calls[1].headers['Idempotency-Key']);
  assert.equal(api.calls[0].body,api.calls[1].body);
  assert.equal(ui.storage.size,0);
});

test('new purchase after a confirmed save gets a fresh key',async()=>{
  const api=backend(),ui=await client(api);ui.fill();await ui.save();
  ui.fill();await ui.save();
  assert.equal(api.records.size,2);
  assert.notEqual(api.calls[0].headers['Idempotency-Key'],api.calls[1].headers['Idempotency-Key']);
});

test('Enter on the closed form cannot resubmit a confirmed purchase',async()=>{
  const api=backend(),ui=await client(api);ui.fill();await ui.save();
  await ui.save();
  assert.equal(api.calls.length,1);
  assert.equal(api.records.size,1);
});

test('validation failure unlocks the form without leaving an uncertain request',async()=>{
  const api=backend();api.mode='400';const ui=await client(api);ui.fill();await ui.save();
  assert.equal(ui.storage.size,0);
  assert.equal(ui.element('#f-amount').disabled,false);
  assert.equal(ui.element('#save-notice').textContent,'invalid input');
});

test('storage failure prevents a request that cannot be safely replayed',async()=>{
  const api=backend(),ui=await client(api,new Map(),true);ui.fill();await ui.save();
  assert.equal(api.calls.length,0);
  assert.match(ui.element('#toast').textContent,/Разреши хранилище/);
});

test('invalid amount and need number never submit',async()=>{
  const api=backend(),ui=await client(api);ui.fill();
  ui.element('#f-amount').value='Infinity';await ui.save();
  ui.element('#f-amount').value='12';ui.element('#f-need').value='abc';await ui.save();
  assert.equal(api.calls.length,0);
});

test('editing retains its success message',async()=>{
  const api=backend(),ui=await client(api);
  ui.openSheet({id:1,amount:12.5,text:'lime',category:'бар'});
  await ui.save();
  assert.equal(ui.element('#toast').textContent,'Сохранено');
  assert.equal(api.calls.length,0);
});
