// Exercise the actual UI controller without a browser, cloud model or runner.
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const presentation=require('../presentation.js'),model=require('../recording-model.js');
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attributes={};this._text='';this.open=false;this.value='';this.classList={toggle(){}}}
  set textContent(value){this._text=String(value);this.children=[]}
  get textContent(){return this._text+this.children.map(c=>c.textContent??'').join('')}
  append(...children){this.children.push(...children)}
  replaceChildren(...children){this.children=children;this._text=''}
  setAttribute(key,value){this.attributes[key]=value}
  showModal(){this.open=true}
  close(){this.open=false}
}
const scenes=[{id:'normal',case_id:'b-chinese',name:'Report',title:'Report',task:'Read',boundary:'Report only',watch:'Report',focus:'report.txt',inputs:{},files:[],records:[]},
  {id:'boundary',case_id:'g-permission',name:'Permission',title:'Permission',task:'Change',boundary:'No change',watch:'File',focus:'permissions.json',inputs:{'permissions.json':'denied'},files:[],records:[]}];
function harness(onFetch=()=>undefined){
  const elements=new Map(),calls=[],document={body:new Element(),createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),addEventListener(){},querySelector(selector){
    if(!elements.has(selector))elements.set(selector,new Element());return elements.get(selector);
  }};
  // IDs on controller-created nodes must resolve to those same nodes.
  document.createElement=tag=>{const n=new Element(tag);Object.defineProperty(n,'id',{set(id){elements.set('#'+id,n)}});return n};
  const context=vm.createContext({document,AgentGuardPresentation:presentation,AgentGuardRecording:model,
    URLSearchParams,Map,Set,Date,Promise,JSON,crypto:{randomUUID:()=> 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'},
    window:{scrollTo(){}},setInterval(){},fetch:async(path,options)=>{
      calls.push({path,options});const custom=onFetch(path,options);
      if(custom!==undefined)return custom;
      return {ok:true,json:async()=>path==='/api/demo'?{scenes:structuredClone(scenes),busy:false}:{busy:false}};
    }});
  vm.runInContext(fs.readFileSync(require.resolve('../recording.js'),'utf8'),context);
  return {context,calls,elements,run:code=>vm.runInContext(code,context)};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('opening and changing chapters never starts work or shows unrelated old results',async()=>{
  const h=harness();await tick();h.run('choose(1)');await tick();
  assert.ok(h.calls.every(c=>!c.options));
  assert.ok(!h.calls.some(c=>c.path.startsWith('/api/live')));
  assert.equal(h.elements.get('#source-badge').textContent,'尚未运行');
  assert.match(h.elements.get('#on-artifact').textContent,/运行后/);
  assert.doesNotMatch(h.elements.get('#on-stats').textContent,/ 0/);
});
test('double click submits a single real scene and immediately clears prior evidence',async()=>{
  let release;const submission=new Promise(resolve=>release=resolve);
  const h=harness((path)=>{
    if(path==='/api/run')return submission;
    if(path.startsWith('/api/live'))return {ok:true,json:async()=>({busy:true,running:true,output:'/runs/new',pairs:[],lanes:{},run_state:{phase:'ISOLATION'}})};
  });await tick();h.run('choose(1)');
  const first=h.run('start()'),second=h.run('start()');
  assert.equal(h.calls.filter(c=>c.path==='/api/run').length,1);
  const body=JSON.parse(h.calls.find(c=>c.path==='/api/run').options.body);
  assert.deepEqual(body.cases,['g-permission']);assert.equal(body.jobs,2);
  release({ok:true,json:async()=>({output:'/runs/new',started:true})});await Promise.all([first,second]);
  assert.match(h.elements.get('#record-line').textContent,/new/);
  assert.equal(h.elements.get('#run').disabled,true);
  assert.equal(h.elements.get('#stop').hidden,false);
});
test('an old async response cannot repopulate another chapter',async()=>{
  let release;const response=new Promise(resolve=>release=resolve);
  const h=harness(path=>path.startsWith('/api/live')?response:undefined);await tick();
  h.run("active=newRecord('old','b-chinese--native--r0',false)");const pending=h.run('refresh()');
  h.run('choose(1)');
  release({ok:true,json:async()=>({busy:false,output:'/runs/old',pairs:[],lanes:{on:{status:{phase:'COMPLETE',executed:99},events:[],cursor:0}}})});await pending;
  assert.equal(h.elements.get('#source-badge').textContent,'尚未运行');
  assert.doesNotMatch(h.elements.get('#on-stats').textContent,/99/);
  assert.match(h.elements.get('#scene-title').textContent,/Permission/);
});
