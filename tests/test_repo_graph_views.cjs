const assert = require('node:assert/strict');
const views = require('../repo_graph/assets/views.js');
const nodes = [{id:'root',name:'Repo',kind:'repository',count:300},
  ...Array.from({length:23},(_,i) => ({id:'n'+i,name:'Component '+i,kind:'directory',count:i+1,layer:i%4}))];
for (const mode of ['atlas','tree','radial','treemap','system']) {
  const input = mode === 'system' ? nodes.slice(1) : nodes;
  const {boxes,width,height} = views.layout(input,mode);
  assert.equal(boxes.size,mode === 'treemap' ? 23 : input.length);
  for (const box of boxes.values()) {
    assert.ok(box.x >= 0 && box.y >= 0 && box.x+box.width <= width+1e-8 && box.y+box.height <= height+1e-8,mode);
    assert.ok(box.width > 0 && box.height > 0);
  }
  const values = [...boxes.values()];
  for (let i=0;i<values.length;i++) for (let j=i+1;j<values.length;j++) {
    const a=values[i],b=values[j];
    assert.ok(a.x+a.width <= b.x+1e-8 || b.x+b.width <= a.x+1e-8 || a.y+a.height <= b.y+1e-8 || b.y+b.height <= a.y+1e-8,mode+' overlaps');
  }
}
const map = views.layout(nodes,'treemap').boxes;
const unitArea = map.get('n0').width*map.get('n0').height;
for (const node of nodes.slice(1)) {
  const box = map.get(node.id);
  assert.ok(Math.abs(box.width*box.height/unitArea-node.count) < 1e-8);
}
for (const mode of ['atlas','tree','radial','treemap','system']) assert.ok(views.layout([],mode).width > 0);
const cycle = views.layout(nodes.slice(1,5),'system',[
  {source:'n0',target:'n1'}, {source:'n1',target:'n2'}, {source:'n2',target:'n1'}, {source:'n2',target:'n3'},
]).boxes;
assert.ok(cycle.get('n0').x < cycle.get('n1').x);
assert.equal(cycle.get('n1').x,cycle.get('n2').x);
assert.ok(cycle.get('n2').x < cycle.get('n3').x);
const tallLayer=nodes.slice(1,9), ring=tallLayer.map((node,index)=>({source:node.id,target:tallLayer[(index+1)%tallLayer.length].id}));
const wrapped=views.layout(tallLayer,'system',ring);
assert.equal(new Set([...wrapped.boxes.values()].map(box=>box.x)).size,2);
assert.ok(wrapped.height<800);
const counts = views.metrics([{source:'n1',target:'n2',count:4},{source:'n1',target:'n3',count:2}]);
assert.deepEqual(counts.get('n1'),{incoming:0,outgoing:6});
assert.equal(views.ordered(nodes.slice(1),'imports',counts)[0].id,'n1');
assert.equal(views.ordered(nodes.slice(1),'files',counts)[0].id,'n22');
const csv = views.csv([{name:'=unsafe,"quoted"',kind:'directory',count:2}],new Map());
assert.ok(csv.includes('"\'=unsafe,""quoted"""'));
assert.equal(nodes[1].id,'n0'); // Sorting never mutates the inventory.
console.log('View geometry, proportional areas, import metrics, sorting and CSV safety passed');

const systemNodes=nodes.slice(1).map(node=>({...node,kind:'system'}));
const systemEdges=systemNodes.slice(0,12).flatMap(a=>systemNodes.slice(0,12).filter(b=>b!==a).map(b=>({source:a.id,target:b.id,count:2,relation:'imports'})));
const overview=views.systemOverview({nodes:systemNodes,edges:systemEdges});
assert.equal(overview.nodes.length,12);
assert.equal(overview.edges.length,40);
assert.equal(overview.relations,132);
assert.equal(overview.imports,264);
assert.equal(overview.omittedAreas,11);
assert.equal(views.systemOverview({nodes:systemNodes,edges:systemEdges},'no-such-area').relations,0);
const capturedStatus={status:'ok',structural:{state:'ready',artifact_ready:true,query_available:true,freshness:'unknown',identities:{generation:'a'.repeat(64)},receipt_knowledge:'captured',receipt:{
  coverage:{files_total:62,files_unsupported:1,file_status:{parsed:60,unsupported_language:1,configuration:1},parser_error_count:0,
    by_language:{python:{files_total:60,file_status:{parsed:60}}},sites_by_role_certainty:{call:{resolved:5,candidate:2,unresolved:3},reference:{unresolved:1}}},
  versions:{schema:'structural-v2',rules:'fixture-rules',grammars:{python:'fixture-grammar'}},revision_dirty:{revision:'b'.repeat(40),dirty:null,knowledge:'captured_revision'}}},
  semantic_index:{state:'not_indexed',artifact_ready:false,query_available:false,catalog_receipt:{documents:62,truncated:0,failed:0}},
  function_evidence:{state:'ready',artifact_ready:true,query_available:true,semantic_state:'not_indexed',semantic_query_available:false}};
const flatten=status=>[...status.summary,...status.details,...status.errors].join('\n');
assert.equal(views.indexStatus(capturedStatus,true).ready,true);
assert.match(flatten(views.indexStatus(capturedStatus,true)),/62 admitted files.*60 parsed/);
assert.match(flatten(views.indexStatus(capturedStatus,true)),/3 calls.*1 references/);
assert.match(flatten(views.indexStatus(capturedStatus,true)),/dirty: unobserved/);
assert.doesNotMatch(flatten(views.indexStatus(capturedStatus,true)),/query available/);
for (const state of ['updating','interrupted','failed','stale','publication_uncertain','unknown_legacy']) {
  const status=structuredClone(capturedStatus); status.structural.state=state;
  status.structural.last_attempt={status:state,reason:'RAW_PRIVATE_CANARY',error:'RAW_PRIVATE_CANARY',traceback:'RAW_PRIVATE_CANARY'};
  status.structural.attempt_attribution='captured_repository';
  assert.match(flatten(views.indexStatus(status)),new RegExp(state.replaceAll('_',' ')));
  assert.doesNotMatch(flatten(views.indexStatus(status)),/RAW_PRIVATE_CANARY/);
}
const stale=structuredClone(capturedStatus); stale.structural.freshness='stale';
assert.match(flatten(views.indexStatus(stale)),/Freshness: stale/);
assert.doesNotMatch(flatten(views.indexStatus(stale,true)),/Freshness: stale/);
for (const freshness of ['current','stale','unknown']) {
  const status=structuredClone(capturedStatus);status.structural.freshness=freshness;
  assert.match(flatten(views.indexStatus(status,true)),new RegExp('Captured freshness: '+freshness+' · Live freshness unobserved'));
}
const partial=structuredClone(capturedStatus); partial.structural.receipt.coverage.file_status.partial_parse=2;partial.structural.receipt.coverage.parser_error_count=4;
partial.semantic_index.catalog_receipt={documents:62,truncated:3,failed:1};
assert.match(views.indexStatus(partial).errors.join(' '),/4 parser errors/);
assert.match(views.indexStatus(partial).errors.join(' '),/3 truncated.*1 failed/);
assert.equal(views.indexStatus({status:'bounded_stop',structural:{artifact_ready:true}}).ready,false);
assert.match(views.indexStatus({status:'bounded_stop'}).errors.join(' '),/storage deadline/);
assert.match(flatten(views.indexStatus(null)),/coverage unavailable/);
console.log('Captured status, unknown freshness, failure privacy and bounded truthful System counts passed');

const capture={generation:'a'.repeat(64),repository_identity:'b'.repeat(64),source_identity:'c'.repeat(64),analyzer_identity:'d'.repeat(64)};
const handle=id=>({id,name:id,path:'calls.py',range:{start_byte:0,end_byte:10,start_line:1,end_line:1},source_sha256:'e'.repeat(64)});
const entry=handle('entry'),leaf=handle('leaf'),other=handle('other');
const call=(id,target=leaf,certainty='resolved')=>({site:{...handle(id),role:'call'},caller:entry,target,certainty,targets_exhaustive:certainty==='resolved',reason:certainty==='unresolved' ? 'callback targets not enumerated' : 'source binding'});
const page=rows=>({...capture,rows,truncated:false,cursor:null,total_count:{kind:'exact',value:rows.length}});
views.queryPage(page([entry]),'symbol');
views.queryPage(page([call('one')]),'callees',entry.id,capture);
const initial=views.callScene(null,page([call('one')]),entry);
const expanded=views.callScene(initial,page([call('two',null,'unresolved')]),entry);
assert.deepEqual(expanded.handles.map(row=>row.id),['entry','leaf']);
assert.deepEqual(expanded.sites.map(row=>row.site.id),['one','two']);
assert.equal(expanded.sites[1].targets.length,0);
assert.equal(initial.sites.length,1); // Expansion is immutable and keeps preceding positions.
const candidates=views.callScene(null,page([call('possible',leaf,'candidate'),call('possible',other,'candidate')]),entry);
assert.equal(candidates.sites.length,1);
assert.equal(candidates.sites[0].targets.length,2);
assert.equal(candidates.sites[0].targets_exhaustive,false);
let bounded=initial;
for(let i=2;i<=21;i++)bounded=views.callScene(bounded,page([call('site'+i)]),entry);
assert.equal(bounded.handles.length+bounded.sites.length,23);
assert.throws(()=>views.callScene(bounded,page([call('site22'),call('site23')]),entry),/24 element/);
assert.throws(()=>views.queryPage({...page([]),generation:'f'.repeat(64)},'callees',entry.id,capture),/Index changed/);
assert.throws(()=>views.queryPage(page([call('wrong')]),'callers','wrong'),/seed mismatch/);
assert.throws(()=>views.queryPage(page(Array(9).fill(entry)),'symbol'),/bounded/);
assert.throws(()=>views.queryPage({...page([]),total_count:{kind:'exact',value:true}},'symbol'),/bounded/);
assert.throws(()=>views.queryPage({...page([]),stop_reason:'private/path'},'symbol'),/bounded/);
const evidence={schema:'captured-source-v1',status:'ok',generation:capture.generation,handle:views.sourceHandle(entry),
  identities:{...capture,structural_generation:capture.generation},evidence_kind:'static_syntax',provenance:{source_sha256:entry.source_sha256},text:'0123456789',range:entry.range,raw_digest:'f'.repeat(64),redacted:false,truncated:false};
views.sourceEvidence(evidence,entry,capture);
assert.throws(()=>views.sourceEvidence({...evidence,generation:'f'.repeat(64)},entry,capture),/identity/);
assert.throws(()=>views.sourceEvidence({...evidence,handle:{...evidence.handle,source_sha256:'0'.repeat(64)}},entry,capture),/identity/);
assert.throws(()=>views.sourceEvidence({...evidence,range:{...entry.range,end_byte:11}},entry,capture),/range/);
console.log('Bounded Calls expansion, target alternatives, unknowns, captured generations and source correlation passed');

const impactCapture={...capture,config_identity:'6'.repeat(64),impact_identity:'7'.repeat(64),contracts_available:true,contract_membership_schema:'captured-contract-membership-v1'};
const impactStatus={status:'ok',structural:{artifact_ready:true,identities:impactCapture,impact:{state:'ready',query_available:true,receipt:{...impactCapture,schema:'captured-impact-v1',identity:impactCapture.impact_identity}}}};
assert.deepEqual(views.capturedImpact(impactStatus),impactCapture);
for(const marker of [undefined,'foreign']) {
  const status=structuredClone(impactStatus);status.structural.impact.receipt.contract_membership_schema=marker;
  assert.equal(views.capturedImpact(status).contracts_available,false);
}
for(const key of ['generation','config_identity']) {
  const status=structuredClone(impactStatus);status.structural.impact.receipt[key]='0'.repeat(64);assert.equal(views.capturedImpact(status),null);
}
const impactRequest={selector:{kind:'source_area',paths:['bindings.json']},relations:['contract'],certainties:['candidate','resolved','unresolved'],services:['gateway'],protocols:['http'],namespaces:['orders-api'],depth:1,limits:{max_entities:8,max_edges:8,max_response_bytes:32768,max_excerpt_bytes:0}};
const witness={...views.sourceHandle(other),source_role:'structural_declaration',slice_sha256:'8'.repeat(64)};
const contract=(id='contract-one',unknown=false)=>({site:{...handle(id),role:unknown ? 'contract_boundary' : 'contract'},caller:entry,target:unknown ? null : leaf,
  relation:'contract',family:unknown ? 'contract_boundary' : 'contract',certainty:unknown ? 'unresolved' : 'resolved',targets_exhaustive:!unknown,reason:unknown ? 'computed_route' : '',reason_truncated:false,evidence_kind:'static_syntax',
  binding_id:'gateway.ordersHttp',service_id:'gateway',endpoint_role:'client',protocol:'http',relation_kind:'explicit_http',namespace:'orders-api',
  contract_identity:{contract_service_id:'orders',namespace:'orders-api',protocol:'http',method:'POST',path:unknown ? null : '/echo',operation:'echo',operation_ref:'#/paths/~1echo/post',request_schema:'#/components/schemas/Request',response_schema:'#/components/schemas/Reply'},
  contract_identity_asserted:!unknown,partial:false,boundary_origin:'captured_source_binding',runtime_qualified:false,evidence:[witness]});
const impactPage=(rows,request=impactRequest)=>{
  const ids=new Set(rows.flatMap(row=>[row.caller?.id,row.target?.id,...(row.evidence || []).filter(value=>value.source_role==='structural_declaration').map(value=>value.id)]).filter(Boolean));
  return {...impactCapture,impact_schema:'captured-impact-v1',runtime_complete:false,live_source_observed:false,historical_call_closure:'unavailable_current_index_only',
    scope:{claim:'possible_captured_reachability',evidence_kind:'static_syntax',depth:request.depth,path_filter:'',name_prefix:'',role:'all',relations:[...request.relations].sort(),certainties:[...request.certainties].sort(),...views.impactFilters(request,request.relations)},
    selection:{seed:null,selector:request.selector},rows,selected_files:[],selected_symbols:[],unavailable_paths:[],unknown_boundaries:{},truncated:false,cursor:null,total_count:{kind:'exact',value:rows.length},
    returned_entities:ids.size,returned_symbol_handles:ids.size,returned_file_handles:0,returned_edges:rows.length};
};
views.impactPage(impactPage([contract()]),impactRequest,impactCapture);
const unknownContract=contract('boundary',true);unknownContract.partial=true;
views.impactPage(impactPage([unknownContract]),impactRequest,impactCapture);
for(const [protocol,fields] of Object.entries({http:contract().contract_identity,rpc:{contract_service_id:'worker',namespace:'fixture.worker',protocol:'rpc',rpc_service:'EchoService',operation:'Echo',request_schema:'EchoRequest',response_schema:'EchoReply'},queue:{contract_service_id:'orders',namespace:'orders.events',protocol:'queue',topic:'created',schema:'OrderCreated'}})) {
  const request={...impactRequest,protocols:[protocol],namespaces:null},row={...contract(),protocol,relation_kind:'explicit_'+protocol,namespace:fields.namespace,contract_identity:fields};
  views.impactPage(impactPage([row],request),request,impactCapture);
  for(const key of Object.keys(fields).filter(key=>key!=='protocol'))for(const value of [null,'']) {
    const bad={...row,contract_identity:{...fields,[key]:value},...(key==='namespace' ? {namespace:value} : {})},page=impactPage([bad],request);
    assert.throws(()=>views.impactPage(page,request,impactCapture),/contract evidence/);
    assert.throws(()=>views.impactScene(null,page),/contract evidence/);
  }
}
for(const captured of [{...impactCapture,contracts_available:false},{...impactCapture,contract_membership_schema:null}])assert.throws(()=>views.impactPage(impactPage([contract()]),impactRequest,captured),/membership/);
for(const key of ['services','protocols','namespaces']) {
  const response=impactPage([contract()]);response.scope[key]=null;assert.throws(()=>views.impactPage(response,impactRequest,impactCapture),/filter/);
}
assert.deepEqual(views.impactFilters({},['call','import']),{services:null,protocols:null,namespaces:null});
assert.deepEqual(views.impactFilters({services:['worker','gateway']},['contract']).services,['gateway','worker']);
for(const filters of [{services:['gateway']},{protocols:['smtp']},{namespaces:['é'.repeat(129)]},{services:Array(9).fill('gateway')},{protocols:[]},{services:['gateway','gateway']}])assert.throws(()=>views.impactFilters(filters,filters.services?.length===1 ? ['call'] : ['contract']),/contract filter|explicit contract/i);
assert.throws(()=>views.impactFilters({namespaces:['orders\0api']},['contract']),/Invalid bounded contract filters/);
const p1Request={...impactRequest,relations:['call'],services:null,protocols:null,namespaces:null};
const p1=impactPage([call('lexical')],p1Request);p1.rows[0].relation='call';p1.rows[0].evidence_kind='static_syntax';p1.returned_entities=p1.returned_symbol_handles=2;p1.contracts_available=false;delete p1.contract_membership_schema;
views.impactPage(p1,p1Request,{...impactCapture,contracts_available:false});
p1.contracts_available=true;p1.contract_membership_schema='foreign';views.impactPage(p1,p1Request,impactCapture); // P1 does not implicitly traverse contracts.
const counter=impactPage([unknownContract]);counter.returned_entities=1;
assert.throws(()=>views.impactPage(counter,impactRequest,impactCapture),/counters/);
assert.throws(()=>views.impactPage(impactPage([unknownContract]),{...impactRequest,limits:{...impactRequest.limits,max_entities:1}},impactCapture),/counters/);
assert.throws(()=>views.impactPage(impactPage([{...unknownContract,target:leaf}]),impactRequest,impactCapture),/contract evidence/);
assert.throws(()=>views.impactPage(impactPage([{...contract(),runtime_qualified:true}]),impactRequest,impactCapture),/contract evidence/);
const badWitness=contract();badWitness.evidence=[{...witness,source_sha256:'bad'}];assert.throws(()=>views.impactPage(impactPage([badWitness]),impactRequest,impactCapture),/source handle/);
const firstContract=views.impactScene(null,impactPage([contract()])),nextContract=views.impactScene(firstContract,impactPage([unknownContract]));
assert.deepEqual(firstContract.sites.map(value=>value.site.id),['contract-one']);assert.deepEqual(nextContract.sites.map(value=>value.site.id),['contract-one','boundary']);
assert.equal(views.impactScene(firstContract,impactPage([contract()])).sites[0].targets.length,1);
const changedContractTarget=impactPage([{...contract(),target:entry}]);views.impactPage(changedContractTarget,impactRequest,impactCapture);
assert.throws(()=>views.impactScene(firstContract,changedContractTarget),/Changed impact occurrence/);
assert.equal(nextContract.sites[1].targets.length,0);assert.equal(nextContract.symbols.length,3);assert.equal(nextContract.sites[0].targets.some(value=>value.id===witness.id),false);
for(const changed of [{service_id:'worker'},{contract_identity:{...contract().contract_identity,contract_service_id:'worker'}},{partial:true,target:null,certainty:'unresolved',targets_exhaustive:false},{evidence:[{...witness,slice_sha256:'0'.repeat(64)}]}])assert.throws(()=>views.impactScene(firstContract,impactPage([{...contract(),...changed}])),/Changed impact|Invalid imported contract|Changed contract source/);
assert.throws(()=>views.impactPage(impactPage([{...contract(),service_id:'worker'}]),impactRequest,impactCapture),/row filter/);
assert.throws(()=>views.impactScene(firstContract,impactPage([{...contract('another-origin'),evidence:[{...witness,slice_sha256:'0'.repeat(64)}]}])),/Changed contract source/);
assert.throws(()=>views.impactScene(firstContract,impactPage([{...contract('new-site'),caller:{...entry,source_sha256:'0'.repeat(64)}}])),/Changed source/);
let contractScene=firstContract;
for(let i=1;i<=20;i++)contractScene=views.impactScene(contractScene,impactPage([contract('origin-'+i)]));
assert.equal(contractScene.symbols.length+contractScene.sites.length,24);
assert.throws(()=>views.impactScene(contractScene,impactPage([contract('too-many')])),/24 element/);
const savedContract={version:3,view:'impact',scope:'',sort:'name',kind:'all',page:0,selected:null,snapshot:Object.fromEntries(['generation','repository_identity','source_identity','analyzer_identity','config_identity'].map(key=>[key,impactCapture[key]])),calls:null,
  impact:{selector:impactRequest.selector,relations:['contract'],certainties:impactRequest.certainties,services:['gateway'],protocols:['http'],namespaces:['orders-api'],depth:1,size:8,identity:impactCapture.impact_identity,selected:'contract-one',intents:[{continuation:false,reset:false,limits:impactRequest.limits}]}};
assert.deepEqual(views.bookmarkView(views.bookmarkFragment(savedContract)),savedContract);
const oldImpact=structuredClone(savedContract);oldImpact.version=2;oldImpact.impact.relations=['call','import'];for(const key of ['services','protocols','namespaces'])delete oldImpact.impact[key];views.savedView(oldImpact);
const oldNavigation={...oldImpact,version:1,view:'atlas'};delete oldNavigation.impact;views.savedView(oldNavigation);
for(const key of ['rows','cursor','response','text','contract_membership_schema']) {
  const saved=structuredClone(savedContract);saved.impact[key]=key==='rows' ? [] : 'forged';assert.throws(()=>views.savedView(saved),/Invalid saved/);
}
const extraIntents=structuredClone(savedContract);extraIntents.impact.intents=Array(25).fill(savedContract.impact.intents[0]);assert.throws(()=>views.savedView(extraIntents),/Invalid saved/);
const oversized=structuredClone(savedContract);oversized.impact.selected='é'.repeat(8192);oversized.scope='é'.repeat(4096);assert.throws(()=>views.bookmarkFragment(oversized),/overflow/);
console.log('Captured contract membership, typed scope, source-only unknown witnesses, scene budgets and saved v3 compatibility passed');

// Exercise the actual event wiring without a browser or a network dependency.
const fs = require('node:fs'), vm = require('node:vm');
class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.dataset={}; this.attributes={}; this.events={}; this.className=''; this.textContent=''; this.value=''; this.style={setProperty(){}}; this.clientWidth=1000; this.clientHeight=800; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  setAttribute(key,value) { this.attributes[key]=value; if(key==='class')this.className=value; if(key.startsWith('data-'))this.dataset[key.slice(5)]=value; }
  focus() { document.activeElement=this; this.fire('focus'); }
  addEventListener(type,callback) { (this.events[type] ||= []).push(callback); }
  fire(type,event={}) { for(const callback of this.events[type] || []) callback({target:this,preventDefault(){},...event}); }
  get childElementCount() { return this.children.length; }
  get classList() { return {toggle:(name,force) => { const set=new Set(this.className.split(' ')); const enabled=force ?? !set.has(name); enabled ? set.add(name) : set.delete(name); this.className=[...set].join(' '); return enabled; },add:name=>this.classList.toggle(name,true),remove:name=>this.classList.toggle(name,false)}; }
  querySelectorAll(selector) { const names=selector.split(',').map(s=>s.trim().slice(1)), result=[]; const visit=node=>{ for(const child of node.children) { if(names.some(name=>child.className.split(' ').includes(name)))result.push(child); visit(child); } }; visit(this); return result; }
  getTotalLength() { return 100; }
  getPointAtLength() { return {x:100,y:100}; }
}
const template=fs.readFileSync(require('node:path').join(__dirname,'../repo_graph/assets/diagram.html'),'utf8');
const helpers=fs.readFileSync(require('node:path').join(__dirname,'../repo_graph/assets/views.js'),'utf8');
const elements=new Map([...template.matchAll(/id="([^"]+)"/g)].map(match=>[match[1],new Element()]));
const extra=new Map(), get=id=>elements.get(id);
const fixture={name:'Synthetic',file_count:62,roles:{},jev:'off',tree:{'':{count:62,children:['docs','src'],direct:['main.py'],sample:['main.py']},docs:{count:1,children:[],direct:['docs/guide.md'],sample:['docs/guide.md']},src:{count:60,children:[],direct:[],sample:[]}},scope_edges:{src:[{source:'src/c00',target:'src/c25',count:7,relation:'imports'}]},system:{nodes:[{id:'system:0',name:'Sources',kind:'system',count:60,layer:1,role:'runtime',files:[],paths:['src'],summary:'Source packages'}],edges:[]}};
fixture.index_status=capturedStatus;
fixture.system.nodes.push({id:'system:1',name:'Docs',kind:'system',count:1,layer:2,role:'documentation',files:['docs/guide.md'],paths:['docs'],summary:'Documentation'});
fixture.system.edges.push({source:'system:0',target:'system:1',count:7,relation:'imports'});
for(let i=0;i<60;i++) { const path='src/c'+String(i).padStart(2,'0'); fixture.tree.src.children.push(path); fixture.tree[path]={count:1,children:[],direct:[path+'/file.py'],sample:[path+'/file.py']}; }
get('graph-data').textContent=JSON.stringify(fixture);
const document={
  getElementById:get,createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),
  querySelector:selector=>{if(!extra.has(selector))extra.set(selector,new Element());return extra.get(selector);},
  querySelectorAll:selector=>selector==='.view-tabs button' ? ['tab-system','tab-explore','tab-data','tab-search','tab-calls'].map(get) : [...elements.values()].flatMap(el=>el.querySelectorAll(selector)),
  addEventListener(){},styleSheets:[],
};
vm.runInNewContext(template.replace('__VIEW_HELPERS__',helpers).match(/<script>\n([\s\S]*?)<\/script>/)[1],{
  document,location:{pathname:'/architecture.html'},window:{innerWidth:1400,addEventListener(){}},
  requestAnimationFrame:fn=>fn(),setTimeout,console,
});
const switchView=mode=>{get('view-mode').value=mode;get('view-mode').fire('change');};
for(const mode of ['tree','radial','treemap','table','matrix','system','atlas']) {
  switchView(mode);
  assert.equal(get('data-panel').hidden,!['table','matrix'].includes(mode));
}
const srcButton=get('component-list').children.find(button=>button.dataset.id==='src');
srcButton.fire('click');
assert.equal(get('breadcrumb').children.at(-1).textContent,'src');
assert.equal(get('breadcrumb').children.at(-1).attributes['aria-current'],'page');
assert.equal(get('component-list').childElementCount,23);
get('next').fire('click');
assert.equal(get('page-label').textContent,'Page 2 / 3');
switchView('matrix');
assert.equal(get('data-panel').children[0].children.at(-1).children.length,23);
get('search').value='no-such-path';get('search').fire('input');
assert.equal(get('component-list').childElementCount,0);
switchView('system');
assert.equal(get('component-list').childElementCount,2);
assert.match(get('map-subtitle').textContent,/1 of 1 grouped import relations.*7 captured imports/);
assert.match(get('index-heading').textContent,/captured export/);
assert.match(get('index-summary').children.map(node=>node.textContent).join(' '),/captured artifact ready.*Live freshness unobserved/);
get('component-list').children[0].fire('click');
assert.ok(get('inspector-content').children.length > 0);
assert.ok(get('workspace').className.includes('has-selection'));
assert.match(get('selection-location').textContent,/Sources/);
assert.equal(get('tab-system').attributes.tabindex,'0');
assert.equal(get('tab-explore').attributes.tabindex,'-1');
const renderedText=node=>node.textContent+' '+node.children.map(renderedText).join(' ');
assert.match(renderedText(get('inspector-content')).replace(/\s+/g,' '),/1 IMPORT RELATIONS.*7 CAPTURED IMPORTS/);
const sourceButton=get('inspector-content').querySelectorAll('.component-item').find(button=>button.textContent==='src →');
sourceButton.fire('click');
assert.equal(get('breadcrumb').children.at(-1).textContent,'src');
switchView('atlas');
switchView('calls');
assert.equal(get('data-panel').hidden,false);
assert.match(get('data-panel').children[0].textContent,/Calls require the local structural index/);
assert.equal(get('tab-calls').attributes['aria-selected'],'true');
switchView('atlas');
get('home').fire('click');
assert.equal(get('breadcrumb').children.at(-1).textContent,'Synthetic');
assert.equal(get('home').disabled,true);
assert.ok(!get('workspace').className.includes('has-selection'));
const transformBefore=get('world').attributes.transform;
get('map').fire('keydown',{key:'ArrowRight'});
assert.notEqual(get('world').attributes.transform,transformBefore);
console.log('Viewer switching, breadcrumbs, pagination, empty search, inspection and keyboard pan passed');
