const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('docs/index.html', 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
const elements = new Map();
for (const [,id] of html.matchAll(/id="([^"]+)"/g)) {
  elements.set(id, {value:'', checked:true, disabled:false, textContent:'', innerHTML:'',
    classList:{toggle(){}}, addEventListener(){}, appendChild(){},
    querySelector(){return this;}, on(){}, removeAllListeners(){}});
}
Object.entries({universe:'curated',metric:'ret',period:'20D',size:'market_cap',color:'sector'})
  .forEach(([id,value])=>elements.get(id).value=value);
const fixture = n=>({latest_market_date:'2026-10-14', records:Array.from({length:n},(_,i)=>({
  ticker:`TEST${i}`,company:`Company ${i}`,sector:i%2?'金融':'情報技術',theme:'Test',
  benchmark:'XLK',last_close:10,last_date:'2026-10-14',market_cap:1e10,
  ret_20D:i/1000,es_z_20D:i/100,industry_z_20D:i/100,spx_z_20D:i/100
})),universe:{selected_at_utc:'2026-10-15T00:00:00Z',source:'Test fixture'}});
let failed=false, pending=[];
const context=vm.createContext({document:{getElementById:id=>{
  assert(elements.has(id),`Unknown element ${id}`);return elements.get(id);
},createElement:()=>({})}, console:{error(){}}, window:{open(){}},
  Plotly:{react:(id,traces)=>context.plotted=traces[0]},
  fetch:async url=>{
    if(pending.length)return pending.shift()(url);
    if(failed)throw new Error('Network failure');
    return {ok:true,json:async()=>fixture(url.startsWith('snapshot_top300')?300:54)};
  }});
const flush=()=>new Promise(setImmediate);
(async()=>{
  vm.runInContext(script,context);await flush();
  assert.equal(context.plotted.x.length,54);
  elements.get('search').value='company 12';vm.runInContext('render()',context);
  assert.equal(context.plotted.x.length,1);
  elements.get('search').value='';elements.get('sector').value='金融';
  vm.runInContext('render()',context);assert.equal(context.plotted.x.length,27);
  elements.get('universe').value='top300';await vm.runInContext('init()',context);
  assert.equal(context.plotted.x.length,300);
  assert.equal(elements.get('labels').checked,false);
  assert.match(elements.get('weightedLabel').textContent,/300/);
  elements.get('search').value='no matches';vm.runInContext('render()',context);
  assert.equal(context.plotted.x.length,0);
  assert.match(elements.get('universeInfo').textContent,/条件に一致/);
  elements.get('universe').value='curated';await vm.runInContext('init()',context);
  assert.equal(context.plotted.x.length,54);
  assert.equal(elements.get('labels').checked,true);
  vm.runInContext('delete snapshotCache.top300',context);failed=true;
  elements.get('universe').value='top300';await vm.runInContext('init()',context);
  assert.equal(elements.get('universe').value,'curated');
  assert.equal(context.plotted.x.length,54);
  assert.match(elements.get('status').textContent,/取得できません/);
  failed=false;
  let release;
  pending.push(()=>new Promise(resolve=>{release=resolve;}));
  elements.get('universe').value='top300';const slow=vm.runInContext('init()',context);
  elements.get('universe').value='curated';await vm.runInContext('init()',context);
  release({ok:true,json:async()=>fixture(300)});await slow;
  assert.equal(context.plotted.x.length,54);
  console.log('UI checks passed: 54/300 switch, search, sector, empty state, failure recovery, request race');
})().catch(err=>{console.error(err);process.exitCode=1;});

