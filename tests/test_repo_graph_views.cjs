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
for(let i=0;i<60;i++) { const path='src/c'+String(i).padStart(2,'0'); fixture.tree.src.children.push(path); fixture.tree[path]={count:1,children:[],direct:[path+'/file.py'],sample:[path+'/file.py']}; }
get('graph-data').textContent=JSON.stringify(fixture);
const document={
  getElementById:get,createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),
  querySelector:selector=>{if(!extra.has(selector))extra.set(selector,new Element());return extra.get(selector);},
  querySelectorAll:selector=>selector==='.view-tabs button' ? ['tab-system','tab-explore','tab-data'].map(get) : [...elements.values()].flatMap(el=>el.querySelectorAll(selector)),
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
assert.equal(get('component-list').childElementCount,1);
get('component-list').children[0].fire('click');
assert.ok(get('inspector-content').children.length > 0);
assert.ok(get('workspace').className.includes('has-selection'));
assert.match(get('selection-location').textContent,/Sources/);
assert.equal(get('tab-system').attributes.tabindex,'0');
assert.equal(get('tab-explore').attributes.tabindex,'-1');
switchView('atlas');
get('home').fire('click');
assert.equal(get('breadcrumb').children.at(-1).textContent,'Synthetic');
assert.equal(get('home').disabled,true);
assert.ok(!get('workspace').className.includes('has-selection'));
const transformBefore=get('world').attributes.transform;
get('map').fire('keydown',{key:'ArrowRight'});
assert.notEqual(get('world').attributes.transform,transformBefore);
console.log('Viewer switching, breadcrumbs, pagination, empty search, inspection and keyboard pan passed');
