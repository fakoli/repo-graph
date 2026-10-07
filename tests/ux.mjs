import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { once } from 'node:events';

const scratch = mkdtempSync(resolve(tmpdir(),'repo-graph-ux-'));
const repo = resolve(scratch,'source'), output = process.env.REPO_GRAPH_UX_OUTPUT || resolve(scratch,'output');
const python = process.env.REPO_GRAPH_PYTHON || 'python3';
let gitBase=null;
if (!process.env.REPO_GRAPH_UX_OUTPUT) {
  for (let i=0;i<75;i++) { const dir=resolve(repo,'src','component'+String(i).padStart(2,'0')); mkdirSync(dir,{recursive:true}); writeFileSync(resolve(dir,'main.py'),'def process():\n    """Apply access control permissions to a request."""\n'); }
  writeFileSync(resolve(repo,'src','component00','main.py'),'def process():\n    """Apply access control permissions to a request."""\n'+
    'def view_leaf():\n    return 1\ndef view_middle():\n    return view_leaf()\ndef view_entry():\n    view_middle()\n    unknown_handler()\n'+
    'def view_callback(fn):\n    return fn()\ndef view_fanout():\n'+Array(30).fill('    view_leaf()\n').join(''));
  writeFileSync(resolve(repo,'src','component01','main.py'),'from ..component00.main import view_leaf\nimport unavailable_service\n\ndef impact_caller():\n    return view_leaf()\n');
  const git=(...args)=>{const result=spawnSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',...args],{encoding:'utf8'});assert.equal(result.status,0,result.stderr);return result.stdout.trim();};
  git('init','-q');
  const changedPath=resolve(repo,'src','component00','main.py'),postimage=readFileSync(changedPath,'utf8');
  writeFileSync(resolve(repo,'obsolete.py'),'def removed():\n    return 1\n');
  writeFileSync(changedPath,postimage.replace('    return 1\n','    return 0\n'));git('add','.');git('commit','-qm','Synthetic base');gitBase=git('rev-parse','HEAD');
  writeFileSync(changedPath,postimage);rmSync(resolve(repo,'obsolete.py'));git('add','.');git('commit','-qm','Synthetic body change and deletion');
  const analysis=spawnSync(python,['scripts/repo_graph.py','analyze',repo,'--output',output,'--mode','serial','--git-base',gitBase],{encoding:'utf8'});
  assert.equal(analysis.status,0,analysis.stderr);
  const captured=spawnSync(python,['scripts/repo_graph.py','map',repo,'--output',output],{encoding:'utf8'});
  assert.equal(captured.status,0,captured.stderr);
}
const method=process.env.REPO_GRAPH_UX_RERANK || 'none';
const server=spawn(python,['scripts/repo_graph.py','serve',output,'--offline',...(method==='local'?['--local-reranker']:method==='jev'?['--allow-jev']:[])],{stdio:['ignore','pipe','pipe']});
let stderr=''; server.stderr.on('data',chunk=>{stderr+=chunk;});
const closed=once(server,'close');
const url=await new Promise((accept,reject)=>{ const timer=setTimeout(()=>reject(new Error('Server start timeout: '+stderr)),30000); server.stdout.on('data',chunk=>{const value=String(chunk).match(/http:\/\/127\.0\.0\.1:\d+\/architecture.html/);if(value){clearTimeout(timer);accept(value[0]);}}); server.once('exit',()=>{clearTimeout(timer);reject(new Error(stderr));}); });
const browser=await chromium.launch({executablePath:process.env.REPO_GRAPH_CHROME === 'chromium' ? undefined : process.env.REPO_GRAPH_CHROME || '/usr/bin/google-chrome',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage(), errors=[];
page.on('pageerror',error=>errors.push(error.message));
const checks=[],callsResponses=[],responseReads=[],callsChecks=[],savedChecks=[],savedObservations=[],impactChecks=[],impactObservations=[],searchChecks=[],searchResponses=[];
page.on('response',response=>{if(new URL(response.url()).pathname==='/api/search' && response.status()===200)responseReads.push((async()=>{try{searchResponses.push({request:response.request().postDataJSON(),response:await response.json()});}catch{}})());});
try {
  const start=Date.now(); await page.goto(url); await page.locator('.node').first().waitFor();
  const loadMs=Date.now()-start;
  const graph=JSON.parse(readFileSync(resolve(output,'graph.json'),'utf8'));
  assert.equal(await page.locator('#stat-files').innerText(),graph.file_count.toLocaleString('en-US'));
  checks.push('inventory count');
  const liveStatus=await (await page.request.get(new URL('/api/status',url).toString())).json();
  await page.waitForFunction(()=>document.querySelector('#index-heading').textContent.includes('local index'));
  assert.match(await page.locator('#index-summary').innerText(),/Live freshness unobserved/);
  if (!process.env.REPO_GRAPH_UX_OUTPUT) {
    assert.equal(liveStatus.structural.artifact_ready,true);
    assert.match(await page.locator('#index-summary').innerText(),/captured artifact ready/);
    assert.match(await page.locator('#index-summary').innerText(),new RegExp(liveStatus.structural.receipt.coverage.files_total+' admitted files'));
  }
  checks.push('captured API readiness and explicit live freshness knowledge');
  for (const state of ['updating','interrupted','stale']) {
    const changed=structuredClone(liveStatus);
    changed.structural.state=state;
    changed.structural.freshness=state==='stale' ? 'stale' : 'unknown';
    changed.structural.last_attempt={status:state==='stale' ? 'failed' : state,reason:'RAW_PRIVATE_DIAGNOSTIC',error:'RAW_PRIVATE_DIAGNOSTIC'};
    changed.structural.attempt_attribution='captured_repository';
    await page.route('**/api/status',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(changed)}));
    await page.reload(); await page.waitForFunction(state=>document.querySelector('#index-summary').textContent.includes('Structural: '+state),state);
    if (state==='stale') {
      assert.match(await page.locator('#index-summary').innerText(),/Freshness: stale/);
      if(process.env.REPO_GRAPH_UX_REPORT) await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-stale.png'});
    }
    assert.ok((await page.locator('#index-errors').innerText()).length>0);
    assert.doesNotMatch(await page.locator('.index-status').innerText(),/RAW_PRIVATE_DIAGNOSTIC/);
    await page.unroute('**/api/status');
  }
  await page.route('**/api/status',route=>route.abort());
  await page.reload(); await page.waitForFunction(()=>document.querySelector('#index-errors').textContent.includes('Live status unavailable'));
  assert.match(await page.locator('#index-heading').innerText(),/captured export/);
  await page.unroute('**/api/status'); await page.reload();
  await page.waitForFunction(()=>document.querySelector('#index-heading').textContent.includes('local index'));
  checks.push('visible lifecycle/stale states; safe errors; unavailable service keeps captured status');
  const offline=await browser.newPage({viewport:{width:1440,height:1000}}), offlineNetwork=[];
  offline.on('pageerror',error=>errors.push(error.message));
  offline.on('request',request=>{if(/^https?:/.test(request.url()))offlineNetwork.push(request.url());});
  try {
    await offline.goto(pathToFileURL(resolve(output,'architecture.html')).toString());
    await offline.click('#tab-system');
    assert.ok(await offline.locator('.node').count()<=12);
    if(!process.env.REPO_GRAPH_UX_OUTPUT)assert.match(await offline.locator('#map-subtitle').innerText(),/Shared index: admitted import bindings.*candidate bindings.*unresolved bindings \(omitted from edges\)/);
    assert.match(await offline.locator('#index-heading').innerText(),/captured export/);
    assert.match(await offline.locator('#index-summary').innerText(),/Live freshness unobserved/);
    if (!process.env.REPO_GRAPH_UX_OUTPUT) assert.match(await offline.locator('#index-summary').innerText(),/captured artifact ready/);
    const payload=await offline.locator('#graph-data').textContent();
    const embedded=JSON.parse(payload);
    const capturedFreshness=['current','stale'].includes(embedded.index_status?.structural?.freshness) ? embedded.index_status.structural.freshness : 'unknown';
    assert.match(await offline.locator('#index-summary').innerText(),new RegExp('Captured freshness: '+capturedFreshness+' · Live freshness unobserved'));
    for(const field of ['definitions','symbols','sites','relationships','source_text','ir']) assert.equal(Object.hasOwn(embedded,field),false);
    assert.equal(Object.hasOwn(embedded.index_status?.structural?.receipt?.coverage || {},'parser_error_samples'),false);
    if(process.env.REPO_GRAPH_UX_REPORT) await offline.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-offline.png'});
    await offline.locator('.node').first().focus(); await offline.keyboard.press('Enter');
    await offline.locator('#inspector-content .detail-section .component-item').first().click();
    assert.equal(await offline.locator('#tab-explore').getAttribute('aria-selected'),'true');
    await offline.click('#tab-calls');
    assert.match(await offline.locator('#data-panel').innerText(),/Calls require the local structural index/);
    await offline.setViewportSize({width:360,height:900});
    assert.ok(await offline.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await offline.locator('.index-status summary').focus(); await offline.keyboard.press('Enter');
    assert.equal(await offline.locator('.index-status details').getAttribute('open'),'');
    assert.ok(await offline.locator('.index-status summary').evaluate(control=>control.getBoundingClientRect().height>=44));
    assert.deepEqual(offlineNetwork,[]);
    checks.push('offline bounded ready overview; source inspection; keyboard coverage; 360px layout; no HTTP requests or symbol graph');
  } finally { await offline.close(); }
  for (const file of ['graph.json','architecture.mmd']) { const response=await page.request.get(new URL(file,url).toString()); assert.equal(response.status(),200); }
  checks.push('JSON and Mermaid downloads');
  for(const mode of ['system','atlas','tree','radial','treemap','table','matrix']) {
    await page.selectOption('#view-mode',mode);
    if(['table','matrix'].includes(mode)) assert.ok(await page.locator('#data-panel').isVisible());
    else assert.ok(await page.locator('#map').isVisible());
    assert.ok(await page.locator('.node').count()<=24); checks.push('view '+mode);
  }
  await page.click('#tab-system');
  assert.ok(await page.locator('.node').count()<=12);
  if(!process.env.REPO_GRAPH_UX_OUTPUT) {
    assert.equal(graph.scan.basis,'shared_structural_index');
    for(const key of ['generation','repository_identity','source_identity','analyzer_identity','config_identity'])assert.equal(graph.scan.identities[key],liveStatus.structural.identities[key]);
    assert.match(await page.locator('#map-subtitle').innerText(),new RegExp(graph.scan.import_certainty_counts.candidate+' candidate bindings'));
    assert.match(await page.locator('#map-subtitle').innerText(),new RegExp(graph.scan.unresolved_import_bindings+' unresolved bindings \\(omitted from edges\\)'));
    impactChecks.push('online/offline System uses shared admitted import projection and visible candidate/unresolved counts without resolving unknown edges');
  }
  const grouped=(graph.system?.edges || []).reduce((sum,edge)=>sum+edge.count,0);
  assert.match(await page.locator('#map-subtitle').innerText(),new RegExp(grouped.toLocaleString('en-US')+' captured imports'));
  await page.evaluate(()=>new Promise(requestAnimationFrame));
  assert.equal(await page.locator('#inspector').isVisible(),false);
  const systemMetrics=await page.evaluate(() => {
    const canvas=document.querySelector('#map').getBoundingClientRect(), heading=document.querySelector('.stage-heading').getBoundingClientRect();
    const cards=[...document.querySelectorAll('.node .card')].map(card=>card.getBoundingClientRect());
    const titleSizes=[...document.querySelectorAll('.node-title')].map(title=>parseFloat(getComputedStyle(title).fontSize)*title.getScreenCTM().a);
    const luminance=color=>color.match(/[\d.]+/g).slice(0,3).map(Number).reduce((sum,value,index)=>{const channel=value/255;return sum+[.2126,.7152,.0722][index]*(channel<=.04045 ? channel/12.92 : ((channel+.055)/1.055)**2.4);},0);
    const pairs=[...document.querySelectorAll('.eyebrow,.legend')].map(label=>[getComputedStyle(label).color,'rgb(248,249,252)']);
    for(const index of document.querySelectorAll('.node-index')) pairs.push([getComputedStyle(index).fill,getComputedStyle(index.parentNode.querySelectorAll('rect')[2]).fill]);
    const minimumLabelContrast=Math.min(...pairs.map(([foreground,background])=>{const [a,b]=[luminance(foreground),luminance(background)].sort((x,y)=>x-y);return (b+.05)/(a+.05);}));
    return {minimumTitlePx:Math.min(...titleSizes),minimumLabelContrast,canvasWidth:canvas.width,cards:cards.length,headingBottom:heading.bottom,cardTop:Math.min(...cards.map(card=>card.top)),cardBottom:Math.max(...cards.map(card=>card.bottom)),canvasBottom:canvas.bottom,allCardsVisible:cards.every(card=>card.left>=canvas.left && card.right<=canvas.right && card.top>=heading.bottom && card.bottom<=canvas.bottom-60)};
  });
  assert.ok(systemMetrics.minimumTitlePx>=14,JSON.stringify(systemMetrics));
  assert.ok(systemMetrics.minimumLabelContrast>=4.5,JSON.stringify(systemMetrics));
  assert.ok(systemMetrics.allCardsVisible,JSON.stringify(systemMetrics)); checks.push('readable System titles; all cards fit; empty inspector collapsed');
  if(process.env.REPO_GRAPH_UX_REPORT) await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'.png'});
  await page.locator('#tab-system').focus(); await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#tab-explore').getAttribute('aria-selected'),'true');
  assert.ok(await page.locator('#tab-explore').evaluate(tab=>tab===document.activeElement));
  assert.equal(await page.locator('.view-tabs button[tabindex="0"]').count(),1); checks.push('arrow navigation and roving tab focus');
  await page.locator('#map').focus();
  const beforePan=await page.locator('#world').getAttribute('transform'); await page.keyboard.press('ArrowRight');
  assert.notEqual(await page.locator('#world').getAttribute('transform'),beforePan);
  await page.keyboard.press('+'); const zoom=await page.locator('#zoom-value').innerText(); await page.keyboard.press('f');
  assert.notEqual(await page.locator('#zoom-value').innerText(),zoom); checks.push('keyboard pan, zoom and fit');
  await page.click('#tab-search');
  await page.getByLabel('Search method').selectOption(process.env.REPO_GRAPH_UX_MODE || 'keyword');
  await page.waitForFunction(() => document.querySelector('[aria-label="Rerank results"]')?.options.length===3);
  assert.equal(await page.getByLabel('Rerank results').inputValue(),'none');
  if(method!=='none') {
    await page.waitForFunction(method => ![...document.querySelector('[aria-label="Rerank results"]').options].find(o=>o.value===method).disabled,method);
    await page.getByLabel('Rerank results').selectOption(method);
    if(method==='jev') assert.ok((await page.locator('.search-status').first().innerText()).includes('TypeSafe'));
  }
  if(!process.env.REPO_GRAPH_UX_OUTPUT) {
    assert.equal(graph.search.kind,'functions');assert.equal(graph.search.documents,null);
    assert.equal(liveStatus.semantic_index.catalog_receipt,null);
    assert.equal(liveStatus.function_evidence.keyword_query_available,true);
    assert.equal(await page.getByLabel('Search method').locator('option[value="semantic"]').evaluate(element=>element.disabled),!liveStatus.function_evidence.semantic_query_available);
    if(!liveStatus.function_evidence.semantic_query_available)assert.match(await page.locator('.search-status[role="status"]').innerText(),/index OUTPUT --kind functions --semantic/);
    const searchFunction=async()=>{await page.getByLabel('Repository search query').fill('view_leaf');await page.getByRole('button',{name:'Search',exact:true}).click();};
    await searchFunction();await page.locator('.search-result').first().waitFor();await Promise.all(responseReads);
    const current=searchResponses.at(-1);assert.equal(current.request.kind,'functions');assert.equal(current.response.kind,'functions');
    for(const key of ['generation','repository_identity','source_identity','analyzer_identity','config_identity'])
      assert.equal(current.response.identities[key==='generation' ? 'structural_generation' : key],graph.scan.identities[key]);
    const hit=current.response.results.find(row=>row.members.some(member=>member.name==='view_leaf'));
    assert.ok(hit);assert.equal(hit.path,'src/component00/main.py');assert.ok(!Object.hasOwn(hit,'evidence'));
    const raw=readFileSync(resolve(repo,hit.path));assert.equal(hit.text,raw.subarray(hit.range.start_byte,hit.range.end_byte).toString('utf8'));
    assert.match(await page.locator('.search-result').allInnerTexts().then(rows=>rows.join(' ')),/view_leaf.*Static syntax|Static syntax.*view_leaf/s);
    searchChecks.push('fresh analyze then captured map without legacy catalogue uses current bounded function-only identifier evidence/ranges and all five identities');
    // Each refusal wraps a real native response; forged transports must publish no excerpts or navigation.
    for(const key of ['structural_generation','repository_identity','source_identity','analyzer_identity','config_identity','missing','legacy']) {
      await page.route('**/api/search',async route=>{const response=await route.fetch();const body=await response.json();
        if(key==='missing')delete body.identities;
        else if(key==='legacy'){delete body.identities;body.kind='files';body.results=[{path:'src/component00/main.py',evidence:'OLD_CATALOG_EVIDENCE'}];body.documents=75;body.seconds=0;}
        else body.identities[key]='f'.repeat(64);
        await route.fulfill({response,body:JSON.stringify(body)});});
      await searchFunction();await page.waitForFunction(()=>document.querySelector('.search-status[role="status"]').textContent.includes('Function evidence snapshot mismatch'));
      assert.equal(await page.locator('.search-result').count(),0);assert.doesNotMatch(await page.locator('.search-panel').innerText(),/OLD_CATALOG_EVIDENCE/);
      await page.unroute('**/api/search');
    }
    searchChecks.push('stale or foreign values for every captured identity, missing identity and transported old file evidence refuse excerpts/current navigation');
    await page.route('**/api/search',async route=>{const request=route.request().postDataJSON();const response=await route.fetch({postData:JSON.stringify({...request,limits:{...request.limits,max_excerpt_bytes:16}})});await route.fulfill({response});});
    await searchFunction();await page.locator('.search-result').first().waitFor();assert.match(await page.locator('.search-status[role="status"]').innerText(),/Partial evidence: excerpt budget exceeded/);
    assert.match(await page.locator('.search-result').first().innerText(),/excerpt truncated/);await page.unroute('**/api/search');
    searchChecks.push('actual native excerpt limit retains bounded partial passages and explicit stop reason');
  }
  await page.getByLabel('Repository search query').fill(process.env.REPO_GRAPH_UX_QUERY || 'access control permissions');
  await page.getByRole('button',{name:'Search',exact:true}).click();
  await page.locator('.search-result').first().waitFor({timeout:30000});
  if(method!=='none') assert.match(await page.locator('.search-status[role="status"]').innerText(),graph.scan?.basis==='shared_structural_index' ? /Rerank work:/ : /Reranker: (used|cached)/);
  const resultPath=await page.locator('.search-result h2').first().innerText();
  const navigateStart=Date.now();
  await page.getByRole('button',{name:'Open in diagram →'}).first().click();
  await page.waitForFunction(path=>document.activeElement?.dataset.id==='file:'+path,resultPath);
  const searchToGraphMs=Date.now()-navigateStart;
  assert.equal(await page.locator('.inspector-path').innerText(),resultPath);
  assert.equal(await page.locator('#selection-location').innerText(),'Selected: '+resultPath);
  const selectedVisible=await page.locator('.node.active .card').evaluate(card=>{const a=card.getBoundingClientRect(),b=document.querySelector('#map').getBoundingClientRect();return a.left>=b.left && a.right<=b.right && a.top>=b.top+140 && a.bottom<=b.bottom-60;});
  assert.ok(selectedVisible); checks.push('search to focused, visible file in diagram');
  const parentPath=resultPath.split('/').slice(0,-1).join('/');
  assert.equal(await page.locator('#breadcrumb [aria-current="page"]').innerText(),parentPath.split('/').pop() || graph.name);
  if(parentPath) { await page.getByRole('button',{name:'Repository root',exact:true}).click(); assert.equal(await page.locator('#breadcrumb [aria-current="page"]').innerText(),graph.name); }
  checks.push('source breadcrumb and root navigation');
  await page.click('#tab-search');
  assert.equal(await page.getByLabel('Repository search query').inputValue(),process.env.REPO_GRAPH_UX_QUERY || 'access control permissions'); checks.push('query preserved');
  assert.equal(await page.getByLabel('Search method').inputValue(),process.env.REPO_GRAPH_UX_MODE || 'keyword');
  await page.waitForFunction(method => document.querySelector('[aria-label="Rerank results"]').value===method,method);
  checks.push('search method and reranker preserved: '+method);
  const prefix=resultPath.split('/').slice(0,-1).join('/') || resultPath;
  await page.getByLabel('Path prefix').fill(prefix);
  await page.getByRole('button',{name:'Search',exact:true}).click();
  await page.locator('.search-result').first().waitFor({timeout:30000});
  const scopedPaths=await page.locator('.search-result h2').allInnerTexts();
  assert.ok(scopedPaths.every(path=>path===prefix || path.startsWith(prefix+'/'))); checks.push('visible path scope restricts results');
  if(graph.scan?.basis==='shared_structural_index')assert.match(await page.locator('.search-status[role="status"]').innerText(),/captured passage count unknown for this scope/);
  await page.setViewportSize({width:800,height:900});
  assert.ok(await page.getByRole('button',{name:'Search',exact:true}).isVisible()); checks.push('narrow viewport search');
  await page.setViewportSize({width:360,height:900});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  const formFits=await page.locator('.search-panel form').evaluate(form=>[...form.querySelectorAll('input,select,button')].every(control=>{const rect=control.getBoundingClientRect();return rect.left>=0 && rect.right<=innerWidth && rect.height>=44;}));
  assert.ok(formFits); checks.push('360px search layout; labelled 44px controls; no horizontal page overflow');
  if(process.env.REPO_GRAPH_UX_REPORT) { await page.getByLabel('Repository search query').scrollIntoViewIfNeeded(); await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-narrow.png',fullPage:true}); }
  await page.getByRole('button',{name:'Open in diagram →'}).first().click();
  await page.getByRole('button',{name:'Close details'}).waitFor();
  await page.waitForFunction(()=>document.activeElement?.id==='close-inspector');
  assert.ok(await page.getByRole('button',{name:'Close details'}).evaluate(button=>button===document.activeElement));
  await page.keyboard.press('Escape'); assert.equal(await page.locator('#inspector').isVisible(),false);
  assert.ok(await page.locator('#map').evaluate(map=>map===document.activeElement)); checks.push('narrow details focus and Escape return');
  if(!process.env.REPO_GRAPH_UX_OUTPUT) {
    page.on('response',response=>{if(/\/api\/(query|source)$/.test(response.url()) && response.status()===200)responseReads.push((async()=>{try{callsResponses.push({endpoint:new URL(response.url()).pathname,request:response.request().postDataJSON(),response:await response.json()});}catch{}})());});
    await page.setViewportSize({width:1440,height:1000});await page.click('#tab-calls');
    const find=async name=>{
      await page.getByLabel('Symbol name prefix').fill(name);await page.getByLabel('Symbol path scope').fill('src/component00');
      await page.getByRole('button',{name:'Find symbols',exact:true}).click();
      await page.locator('.calls-matches button').filter({hasText:name+' ·'}).first().waitFor();
      await page.locator('.calls-matches button').filter({hasText:name+' ·'}).first().click();
      await page.waitForFunction(()=>!document.querySelector('.calls-panel button')?.disabled && document.querySelector('.calls-status[role="status"]').textContent.includes('rows in this page'));
    };
    await find('view_entry');await page.locator('.call-site').nth(1).waitFor();
    assert.match(await page.locator('.call-site').allInnerTexts().then(text=>text.join(' ')),/unresolved.*Unresolved target.*not exhaustive/s);
    assert.match(await page.locator('.call-site').allInnerTexts().then(text=>text.join(' ')),/view_middle/);
    assert.ok(await page.locator('.call-element').count()<=24);callsChecks.push('actual outgoing direct and unresolved calls with nonexhaustive reasons');
    const firstPosition=await page.locator('.call-symbol').first().evaluate(card=>{window.__callFirst=card;return {top:card.offsetTop,left:card.offsetLeft};});
    const middle=page.locator('.call-symbol').filter({hasText:'view_middle'});
    await middle.getByRole('button',{name:'Expand outgoing',exact:true}).click();await page.locator('.call-symbol').filter({hasText:'view_leaf'}).waitFor();
    assert.deepEqual(await page.locator('.call-symbol').first().evaluate(card=>({top:card.offsetTop,left:card.offsetLeft})),firstPosition);
    assert.ok(await page.locator('.call-symbol').first().evaluate(card=>card===window.__callFirst));
    assert.equal(await middle.getByRole('button',{name:'Expand outgoing',exact:true}).evaluate(button=>button===document.activeElement),true);
    callsChecks.push('progressive actual call-chain expansion retains prior DOM positions and focus');
    await middle.getByRole('button',{name:'Inspect declaration',exact:true}).click();await page.locator('.call-evidence pre').waitFor();
    assert.match(await page.locator('.call-evidence pre').innerText(),/def view_middle/);
    assert.match(await page.locator('.call-evidence').innerText(),/excerpt digest verified/);
    await page.keyboard.press('Escape');assert.equal(await middle.getByRole('button',{name:'Inspect declaration',exact:true}).evaluate(button=>button===document.activeElement),true);
    const unknown=page.locator('.call-site').filter({hasText:'unresolved callsite'});
    await unknown.getByRole('button',{name:'Inspect callsite',exact:true}).click();await page.locator('.call-evidence pre').waitFor();
    assert.match(await page.locator('.call-evidence pre').innerText(),/unknown_handler\(\)/);await page.keyboard.press('Escape');
    callsChecks.push('actual declaration/callsite source ranges, digests and Escape focus return');
    await unknown.getByRole('button',{name:'Inspect callsite',exact:true}).click();await page.locator('.call-evidence pre').waitFor();
    await page.route('**/api/source',async route=>{const response=await route.fetch();const body=await response.json();body.raw_digest='0'.repeat(64);await route.fulfill({response,body:JSON.stringify(body)});});
    await middle.getByRole('button',{name:'Inspect declaration',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent.includes('digest mismatch'));
    assert.equal(await page.locator('.call-evidence').isVisible(),false);await page.unroute('**/api/source');
    await page.route('**/api/source',route=>route.fulfill({status:409,contentType:'application/json',body:'{"error":"RAW_PRIVATE_DIAGNOSTIC"}'}));
    await middle.getByRole('button',{name:'Inspect declaration',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent.includes('Stale source'));
    assert.doesNotMatch(await page.locator('.calls-panel').innerText(),/RAW_PRIVATE_DIAGNOSTIC/);await page.unroute('**/api/source');
    callsChecks.push('tampered source digest and stale source refusal display no forged evidence or raw errors');
    await find('view_middle');await page.getByLabel('Call direction').selectOption('callers');
    await page.waitForFunction(()=>document.querySelector('.call-site')?.textContent.includes('view_entry'));
    callsChecks.push('actual incoming callsites use same captured Queries payload');
    await page.getByLabel('Call direction').selectOption('callees');await find('view_callback');
    await page.waitForFunction(()=>document.querySelector('.call-site')?.textContent.includes('unresolved'));
    assert.match(await page.locator('.call-site').innerText(),/not exhaustive/);callsChecks.push('unsupported callback remains unresolved; no invented candidates');
    await find('view_fanout');
    const seen=new Set();let pages=0;
    while(true) {
      await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent.includes('rows in this page'));
      assert.ok(await page.locator('.call-element').count()<=24);
      for(const id of await page.locator('.call-site').evaluateAll(cards=>cards.map(card=>card.dataset.id)))seen.add(id);
      const more=page.getByRole('button',{name:/^(More callsites|Next page \(replace scene\))$/});
      if(await more.isDisabled())break;
      assert.ok(++pages<12,'finite fanout page count');await more.click();
      await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent!=='Reading bounded captured evidence…');
    }
    assert.equal(seen.size,30);callsChecks.push('actual fixed-snapshot fanout pages retain all30 physical sites within24 elements');
    await page.route('**/api/query',async route=>{const response=await route.fetch();const body=await response.json();body.generation='0'.repeat(64);await route.fulfill({response,body:JSON.stringify(body)});});
    await page.getByRole('button',{name:'New view at selected symbol',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent.includes('Index changed'));
    await page.unroute('**/api/query');callsChecks.push('changed captured generation is rejected before scene publication');
    let release,started;const waiting=new Promise(resolve=>{started=resolve;});const barrier=new Promise(resolve=>{release=resolve;});
    await page.route('**/api/query',async route=>{const response=await route.fetch();started();await barrier;try{await route.fulfill({response});}catch{}});
    await page.getByRole('button',{name:'Find symbols',exact:true}).click();await waiting;await page.getByRole('button',{name:'Cancel request',exact:true}).click();release();
    await page.waitForTimeout(100);assert.match(await page.locator('.calls-status[role="status"]').innerText(),/Stopped waiting/);
    assert.equal(await page.locator('.calls-matches button').count(),0);await page.unroute('**/api/query');callsChecks.push('cancelled browser request discards late actual backend response');
    await find('view_entry');await page.setViewportSize({width:360,height:900});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.locator('.call-symbol').first().getByRole('button',{name:'Inspect declaration',exact:true}).focus();await page.keyboard.press('Enter');await page.locator('.call-evidence pre').waitFor();
    await page.keyboard.press('Escape');assert.equal(await page.locator('.call-symbol').first().getByRole('button',{name:'Inspect declaration',exact:true}).evaluate(button=>button===document.activeElement),true);
    callsChecks.push('360px Calls controls and keyboard source inspection');
    if(process.env.REPO_GRAPH_UX_REPORT)await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-calls-narrow.png',fullPage:true});
    await page.setViewportSize({width:1440,height:1000});if(process.env.REPO_GRAPH_UX_REPORT)await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-calls.png'});

    const readSaved=()=>page.evaluate(()=>{const key='repo-graph:view:'+location.pathname;const raw=sessionStorage.getItem(key);return {key,raw,value:raw===null ? null : JSON.parse(raw)};});
    const writeSaved=value=>page.evaluate(value=>sessionStorage.setItem('repo-graph:view:'+location.pathname,JSON.stringify(value)),value);
    const restored=()=>page.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.startsWith('Restored'));
    const scene=()=>page.locator('.call-element').evaluateAll(cards=>cards.map(card=>({id:card.dataset.id,top:card.offsetTop,left:card.offsetLeft})));
    await page.locator('.call-symbol').filter({hasText:'view_middle'}).getByRole('button',{name:'Expand outgoing',exact:true}).click();
    await page.locator('.call-symbol').filter({hasText:'view_leaf'}).waitFor();
    const expandedScene=await scene(),expandedRecord=(await readSaved()).value;
    assert.equal(expandedRecord.calls.intents.length,2);assert.ok(Buffer.byteLength(JSON.stringify(expandedRecord))<=32768);
    for(const word of ['cursor','text','rows','prefix','query','targets'])assert.equal(Object.hasOwn(expandedRecord,word),false);
    savedObservations.push({case:'chain before reload',record:expandedRecord,scene:expandedScene});
    await page.reload();await restored();
    assert.equal(await page.locator('#tab-calls').getAttribute('aria-selected'),'true');
    assert.deepEqual(await scene(),expandedScene);assert.deepEqual((await readSaved()).value.snapshot,expandedRecord.snapshot);
    assert.equal(await page.locator('.call-evidence').isVisible(),false);
    await page.locator('.call-symbol').filter({hasText:'view_middle'}).getByRole('button',{name:'Inspect declaration',exact:true}).click();await page.locator('.call-evidence pre').waitFor();
    assert.match(await page.locator('.call-evidence pre').innerText(),/def view_middle/);await page.keyboard.press('Escape');
    savedChecks.push('actual reload restores symbol, snapshot, ranges and stable call-chain positions without cached facts or excerpts');

    await page.context().grantPermissions(['clipboard-read','clipboard-write'],{origin:new URL(url).origin});
    await page.getByRole('button',{name:'Copy bookmark',exact:true}).click();await page.getByLabel('Bookmark URL').waitFor();
    const bookmark=await page.getByLabel('Bookmark URL').inputValue();assert.ok(Buffer.byteLength(bookmark)<=32768);
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),bookmark);
    assert.equal(new URL(bookmark).search,'');
    const bookmarkRecord=JSON.parse(decodeURIComponent(new URL(bookmark).hash.slice(6)));
    assert.deepEqual(bookmarkRecord.calls,expandedRecord.calls);assert.deepEqual(bookmarkRecord.snapshot,expandedRecord.snapshot);
    const bookmarkPage=await page.context().newPage();await bookmarkPage.setViewportSize({width:1440,height:1000});
    bookmarkPage.on('pageerror',error=>errors.push(error.message));
    bookmarkPage.on('response',response=>{if(/\/api\/(query|source)$/.test(response.url()) && response.status()===200)responseReads.push((async()=>{try{callsResponses.push({page:'bookmark',endpoint:new URL(response.url()).pathname,request:response.request().postDataJSON(),response:await response.json()});}catch{}})());});
    try {
      await bookmarkPage.goto(bookmark);await bookmarkPage.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.startsWith('Restored bookmark'));
      assert.deepEqual(await bookmarkPage.locator('.call-element').evaluateAll(cards=>cards.map(card=>({id:card.dataset.id,top:card.offsetTop,left:card.offsetLeft}))),expandedScene);
      assert.equal(await bookmarkPage.evaluate(()=>sessionStorage.getItem('repo-graph:view:'+location.pathname)),null);
      assert.equal(await bookmarkPage.locator('.call-evidence').isVisible(),false);
      savedObservations.push({case:'actual copied bookmark new-tab roundtrip',url:bookmark,record:bookmarkRecord});
      // A malformed explicit fragment takes precedence over an otherwise valid tab-local record.
      await bookmarkPage.evaluate(record=>sessionStorage.setItem('repo-graph:view:'+location.pathname,JSON.stringify(record)),bookmarkRecord);
      const invalidBookmark=new URL(bookmark);invalidBookmark.hash='#view=%not-json';
      await bookmarkPage.goto(invalidBookmark.href);await bookmarkPage.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.includes('Invalid bookmark'));
      assert.equal(await bookmarkPage.locator('.call-element').count(),0);
      const staleBookmark=new URL(bookmark),staleBookmarkRecord=structuredClone(bookmarkRecord);staleBookmarkRecord.snapshot.generation='0'.repeat(64);
      staleBookmark.hash='#view='+encodeURIComponent(JSON.stringify(staleBookmarkRecord));
      await bookmarkPage.goto(staleBookmark.href);await bookmarkPage.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.includes('Bookmark snapshot is stale'));
      assert.equal(await bookmarkPage.locator('.call-element').count(),0);
      const oversizedBookmark=new URL(bookmark);oversizedBookmark.hash='#view='+'x'.repeat(32769);
      await bookmarkPage.goto(oversizedBookmark.href);await bookmarkPage.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.includes('Bookmark exceeds 32 KiB'));
      assert.equal(await bookmarkPage.locator('.call-element').count(),0);
      await bookmarkPage.getByRole('button',{name:'Clear saved view',exact:true}).click();assert.equal(new URL(bookmarkPage.url()).hash,'');
      savedChecks.push('actual clipboard bookmark new-tab roundtrip reuses bounded snapshot replay; invalid/stale/oversized fragments refuse selection and valid-storage fallback');
    } finally {await bookmarkPage.close();}

    await find('view_fanout');
    const fanoutSeen=new Set();
    for(let step=0;step<3;step++) {
      for(const id of await page.locator('.call-site').evaluateAll(cards=>cards.map(card=>card.dataset.id)))fanoutSeen.add(id);
      await page.getByRole('button',{name:/^(More callsites|Next page \(replace scene\))$/}).click();
      await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent.includes('rows in this page'));
    }
    const replacedScene=await scene(),fanoutRecord=(await readSaved()).value;
    assert.equal(fanoutRecord.calls.intents.length,4);assert.ok(fanoutRecord.calls.intents.some(step=>step.reset && step.continuation));
    await Promise.all(responseReads);
    const fanoutPage=callsResponses.filter(row=>row.endpoint==='/api/query' && row.request.operation==='callees' && row.request.seed===fanoutRecord.calls.root.id).at(-1);
    assert.ok(fanoutPage.response.cursor);assert.doesNotMatch(JSON.stringify(fanoutRecord),new RegExp(fanoutPage.response.cursor));
    savedObservations.push({case:'fanout replaced scene before cursor expiry',record:fanoutRecord,scene:replacedScene});
    // Exercise the actual 60-second server cursor/session expiry, not a fabricated expiry response.
    await page.waitForTimeout(31000);await page.waitForTimeout(31000);
    const expired=await page.request.post(new URL('/api/query',url).toString(),{data:{...fanoutPage.request,cursor:fanoutPage.response.cursor}});
    assert.equal(expired.status(),400);
    await page.reload();await restored();assert.deepEqual(await scene(),replacedScene);
    while(true) {
      assert.ok(await page.locator('.call-element').count()<=24);
      for(const id of await page.locator('.call-site').evaluateAll(cards=>cards.map(card=>card.dataset.id)))fanoutSeen.add(id);
      const more=page.getByRole('button',{name:/^(More callsites|Next page \(replace scene\))$/});if(await more.isDisabled())break;
      await more.click();await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent.includes('rows in this page'));
    }
    assert.equal(fanoutSeen.size,30);savedChecks.push('actual expired cursor is not persisted; fresh bounded replay restores replaced fanout page and complete continuation');

    const stableRecord=(await readSaved()).value;
    const staleRecord=structuredClone(stableRecord);staleRecord.snapshot.generation='0'.repeat(64);
    await writeSaved(staleRecord);await page.reload();await page.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.includes('snapshot is stale'));
    assert.equal(await page.locator('.call-element').count(),0);savedChecks.push('changed snapshot refuses old symbol and expansion before publishing a scene');
    const invalidRecord=structuredClone(stableRecord);invalidRecord.calls.root.range.end_byte=-1;
    await writeSaved(invalidRecord);await page.reload();await page.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.includes('Invalid saved view'));
    assert.equal(await page.locator('.call-element').count(),0);
    const forgedSelection=structuredClone(stableRecord);forgedSelection.calls.root.source_sha256='0'.repeat(64);
    await writeSaved(forgedSelection);await page.reload();await page.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.includes('invalid selection'));
    assert.equal(await page.locator('.call-element').count(),0);
    await page.evaluate(()=>sessionStorage.setItem('repo-graph:view:'+location.pathname,' '.repeat(32769)));await page.reload();
    await page.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.includes('exceeds 32 KiB'));
    await page.getByRole('button',{name:'Clear saved view',exact:true}).click();await page.reload();
    assert.equal((await readSaved()).value,null);assert.equal(await page.locator('#view-mode').inputValue(),'atlas');
    savedChecks.push('malformed handles, forged digest, oversized record and clear controls never claim successful restoration');

    await page.click('#tab-search');const sensitive='synthetic_private_query_9371';
    await page.getByLabel('Repository search query').fill(sensitive);
    await page.click('#tab-calls');await page.getByLabel('Symbol name prefix').fill(sensitive);await page.getByRole('button',{name:'Find symbols',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent.includes('rows in this page'));
    assert.doesNotMatch(page.url(),new RegExp(sensitive));assert.doesNotMatch((await readSaved()).raw,new RegExp(sensitive));
    await page.getByRole('button',{name:'Copy bookmark',exact:true}).click();await page.getByLabel('Bookmark URL').waitFor();
    assert.doesNotMatch(await page.getByLabel('Bookmark URL').inputValue(),new RegExp(sensitive));
    await page.click('#tab-search');await page.reload();await restored();
    assert.equal(await page.getByLabel('Repository search query').inputValue(),'');
    assert.doesNotMatch(page.url(),/[?#]/);savedChecks.push('sensitive queries remain absent from native record and URL; reload never replays Search work');

    await page.click('#tab-explore');if(!await page.locator('#home').isDisabled())await page.click('#home');
    await page.getByRole('button',{name:'Open src',exact:true}).click();await page.getByRole('button',{name:'Open src/component00',exact:true}).click();
    await page.getByRole('button',{name:'Select src/component00/main.py',exact:true}).click();
    const sourceRecord=(await readSaved()).value;await page.reload();await restored();
    assert.equal(await page.locator('#breadcrumb').innerText().then(text=>text.includes('component00')),true);
    assert.match(await page.locator('#selection-location').innerText(),/src\/component00\/main.py/);
    assert.equal((await readSaved()).value.selected,sourceRecord.selected);
    savedChecks.push('actual source area, view, scope and selected file restore after rendering');

    await page.click('#tab-calls');await find('view_entry');
    let resumeRestore,restoreStarted;const restoreWaiting=new Promise(resolve=>{restoreStarted=resolve;});const restoreBarrier=new Promise(resolve=>{resumeRestore=resolve;});
    await page.route('**/api/query',async route=>{const response=await route.fetch();restoreStarted();await restoreBarrier;try{await route.fulfill({response});}catch{}});
    await page.reload();await restoreWaiting;await page.getByRole('button',{name:'Cancel restore',exact:true}).click();resumeRestore();
    await page.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.includes('Restore partial: cancelled'));
    await page.waitForTimeout(100);assert.equal(await page.locator('.call-element').count(),0);await page.unroute('**/api/query');
    savedChecks.push('cancelled restore ignores late actual backend reply and reports partial restoration');
    await page.reload();await restored();await page.setViewportSize({width:360,height:900});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.locator('.call-symbol').first().getByRole('button',{name:'Inspect declaration',exact:true}).focus();await page.keyboard.press('Enter');await page.locator('.call-evidence pre').waitFor();
    await page.keyboard.press('Escape');assert.equal(await page.locator('.call-symbol').first().getByRole('button',{name:'Inspect declaration',exact:true}).evaluate(button=>button===document.activeElement),true);
    await page.getByRole('button',{name:'Clear saved view',exact:true}).focus();assert.ok(await page.getByRole('button',{name:'Clear saved view',exact:true}).evaluate(button=>button.getBoundingClientRect().height>=44));
    if(process.env.REPO_GRAPH_UX_REPORT)await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-saved-narrow.png',fullPage:true});
    savedChecks.push('restored Calls keeps 360px keyboard source inspection, Escape return and labelled 44px storage controls');
    await page.setViewportSize({width:1440,height:1000});
    for(let i=0;i<24;i++) {
      await page.locator('.call-symbol').first().getByRole('button',{name:'Expand outgoing',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent.includes('rows in this page'));
    }
    assert.equal((await readSaved()).value,null);assert.match(await page.locator('#saved-view-status').innerText(),/exceeds 24 Calls intents/);
    savedChecks.push('actual 25th successful Calls intent refuses saving instead of silently truncating expansion');
    await page.getByRole('button',{name:'New view at selected symbol',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.calls-status[role="status"]').textContent.includes('rows in this page'));
    savedObservations.push({case:'new bounded view after overflow',record:(await readSaved()).value});
    if(process.env.REPO_GRAPH_UX_REPORT)await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-saved.png'});

    await page.setViewportSize({width:1440,height:1000});await page.click('#tab-impact');
    const impactStatus=page.locator('.impact-panel .calls-status[role="status"]');
    const findImpact=async(path,relation='all',certainty='all',kind='source_area')=>{
      await page.getByLabel('Impact input',{exact:true}).selectOption(kind);
      if(kind==='source_area')await page.getByLabel('Impact source area or captured base').fill(path);
      await page.getByLabel('Impact relation',{exact:true}).selectOption(relation);await page.getByLabel('Impact certainty',{exact:true}).selectOption(certainty);
      await page.getByRole('button',{name:'Find impact',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('.impact-panel .calls-status[role="status"]').textContent.includes('physical relations in this page'));
    };
    const impactScene=()=>page.locator('.impact-element').evaluateAll(cards=>cards.map(card=>({id:card.dataset.id,type:card.dataset.type,relation:card.dataset.relation,top:card.offsetTop,left:card.offsetLeft})));
    const showImpactBoundaries=async()=>{const details=page.locator('.impact-panel details');if(await details.getAttribute('open')===null)await details.locator('summary').click();};
    const collectImpact=async()=>{
      const physical=new Map();let pages=0;
      while(true) {
        assert.ok(await page.locator('.impact-element').count()<=24);
        for(const card of await impactScene())if(card.type==='site')physical.set(card.id,card.relation);
        const more=page.getByRole('button',{name:/^(More impact|Next impact page \(replace scene\))$/});
        if(await more.isDisabled())break;
        assert.ok(++pages<40,'bounded impact page count');await more.click();
        await page.waitForFunction(()=>document.querySelector('.impact-panel .calls-status[role="status"]').textContent.includes('physical relations in this page'));
      }
      return physical;
    };
    await findImpact('src/component00/main.py');await Promise.all(responseReads);
    const firstImpact=callsResponses.filter(value=>value.endpoint==='/api/query' && value.request.operation==='impact').at(-1);
    assert.ok(firstImpact.response.cursor);assert.equal(firstImpact.response.contracts_available,false);assert.equal(firstImpact.response.runtime_complete,false);
    const changedFilter=await page.request.post(new URL('/api/query',url).toString(),{data:{...firstImpact.request,relations:['import'],cursor:firstImpact.response.cursor}});
    assert.equal(changedFilter.status(),400);
    const firstImpactCard=await page.locator('.impact-element').first().evaluate(card=>{window.__impactFirst=card;return {top:card.offsetTop,left:card.offsetLeft};});
    if(await page.getByRole('button',{name:'More impact',exact:true}).count()) {
      await page.getByRole('button',{name:'More impact',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.impact-panel .calls-status[role="status"]').textContent.includes('physical relations in this page'));
      assert.deepEqual(await page.locator('.impact-element').first().evaluate(card=>({top:card.offsetTop,left:card.offsetLeft})),firstImpactCard);
      assert.ok(await page.locator('.impact-element').first().evaluate(card=>card===window.__impactFirst));
    }
    const physical=await collectImpact();
    await Promise.all(responseReads);
    // Source-owned expected physical call intervals, independent of returned target/rank rows.
    const expectedCalls=new Set();
    for(const path of ['src/component00/main.py','src/component01/main.py']) {
      const raw=readFileSync(resolve(repo,path),'utf8');
      for(const match of raw.matchAll(/(?:return |    )(view_leaf|view_middle|unknown_handler|fn)\(\)/g)) {
        const start=match.index+match[0].lastIndexOf(match[1]+'('),end=start+match[1].length+2;expectedCalls.add(`${path}:${start}:${end}:call`);
      }
    }
    // First page may have been replaced later; include its observed physical evidence.
    for(const exchange of callsResponses.filter(value=>value.request.operation==='impact' && value.request.selector?.paths?.[0]==='src/component00/main.py' && value.request.relations.length===2))
      for(const row of exchange.response.rows)physical.set(row.site.id,row.relation);
    assert.deepEqual(new Set([...physical].filter(([,relation])=>relation==='call').map(([id])=>id)),expectedCalls);
    assert.ok([...physical.values()].includes('import'));assert.match(await impactStatus.innerText(),/Runtime effects unknown/);
    await showImpactBoundaries();
    assert.match(await page.locator('.impact-panel details').innerText(),/unassigned incoming targets not enumerable/);
    impactChecks.push('actual source-area reverse imports/calls retain source-owned physical intervals; <=24 cards, stable append positions and filter-bound continuation');

    await findImpact('src/component00/main.py','import');await collectImpact();
    assert.ok((await impactScene()).filter(value=>value.type==='site').every(value=>value.relation==='import'));
    await findImpact('src/component01/main.py','import','unresolved');await collectImpact();
    assert.match(await page.locator('.impact-element[data-type="site"]').innerText(),/Import · unresolved.*Unresolved target.*not exhaustive/s);
    assert.equal(await page.getByLabel('Impact relation',{exact:true}).locator('option[value="contract"]').evaluate(element=>element.disabled),true);
    assert.match(await page.locator('.impact-panel').innerText(),/Contract filters unavailable/);
    assert.equal(callsResponses.some(value=>value.request.relations?.includes('contract')),false);
    impactChecks.push('relation/certainty filters use actual backend rows; unknown external import explicit; Contracts disabled without requests');

    await findImpact('src/component00/main.py','import');
    await collectImpact();const physicalImport=page.locator('.impact-element[data-relation="import"]').filter({hasText:'src/component01/main.py'}).first();
    await physicalImport.getByRole('button',{name:'Inspect import',exact:true}).click();await page.locator('.impact-panel .call-evidence pre').waitFor();
    assert.match(await page.locator('.impact-panel .call-evidence pre').innerText(),/from \.\.component00\.main import view_leaf/);
    assert.match(await page.locator('.impact-panel .call-evidence').innerText(),/excerpt digest verified/);await page.keyboard.press('Escape');
    assert.equal(await physicalImport.getByRole('button',{name:'Inspect import',exact:true}).evaluate(button=>button===document.activeElement),true);
    await page.route('**/api/source',async route=>{const response=await route.fetch();const body=await response.json();body.impact_identity='0'.repeat(64);await route.fulfill({response,body:JSON.stringify(body)});});
    await physicalImport.getByRole('button',{name:'Inspect import',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.impact-panel .calls-status[role="status"]').textContent.includes('Import source capture mismatch'));
    assert.equal(await page.locator('.impact-panel .call-evidence').isVisible(),false);await page.unroute('**/api/source');
    impactChecks.push('physical import source membership/range/digest and impact identity verified; forged capture refuses excerpt with keyboard focus return');

    await findImpact(null,'call','all','git_change');
    assert.equal(await page.getByLabel('Impact source area or captured base').inputValue(),gitBase);
    assert.match(await page.locator('.impact-panel').innerText(),/worktree byte affinity unobserved.*historical call closure unavailable/s);
    const deletedFile=page.locator('.impact-element[data-type="unavailable"]').filter({hasText:'obsolete.py'});assert.match(await deletedFile.innerText(),/Changed path D.*current closure unavailable/s);
    await deletedFile.getByRole('button',{name:'Open file in Explore',exact:true}).click();assert.match(await impactStatus.innerText(),/File absent from the captured map/);
    assert.equal(await page.locator('.impact-panel .call-evidence').isVisible(),false);
    const changedFile=page.locator('.impact-element[data-type="file"]').filter({hasText:'Changed path M'}).first();
    assert.match(await changedFile.innerText(),/src\/component00\/main.py/);await changedFile.getByRole('button',{name:'Open file in Explore',exact:true}).click();
    await page.getByRole('button',{name:'Close details'}).waitFor();assert.match(await page.locator('#inspector').innerText(),/src\/component00\/main.py/);
    await page.keyboard.press('Escape');impactChecks.push('actual captured Git-base modified file opens the mapped area; deleted source refuses navigation/excerpts and historical/worktree boundaries remain visible');

    await page.click('#tab-impact');await findImpact('missing.py','call');await collectImpact();
    await showImpactBoundaries();
    assert.equal(await page.locator('.impact-element').count(),0);assert.match(await page.locator('.impact-panel details').innerText(),/source area not in admitted inventory/);
    assert.match(await impactStatus.innerText(),/lower bound/);impactChecks.push('nonadmitted source area stays unknown with exhausted captured traversal; no known-empty runtime claim');
    await page.getByLabel('Impact rows per page').selectOption('1');await findImpact('src/component00/main.py','call');
    assert.match(await impactStatus.innerText(),/(edge|entity) budget exceeded/);assert.match(await impactStatus.innerText(),/bounded continuation/);
    await page.getByLabel('Impact rows per page').selectOption('8');assert.equal(await page.getByRole('button',{name:/^(More impact|Next impact page \(replace scene\))$/}).isDisabled(),true);
    impactChecks.push('actual edge/entity exhaustion shown; changed controls retire old continuation before a fresh query');

    await findImpact('src/component01/main.py','import');await collectImpact();
    await page.locator('.impact-element[data-type="site"]').first().getByRole('button',{name:'Select impact item',exact:true}).click();
    const savedImpact=(await readSaved()).value,impactBeforeReload=await impactScene();assert.equal(savedImpact.version,2);assert.equal(savedImpact.view,'impact');
    for(const key of ['cursor','rows','text','query','prefix','response'])assert.equal(JSON.stringify(savedImpact).includes('"'+key+'"'),false);
    await page.reload();await restored();assert.deepEqual(await impactScene(),impactBeforeReload);assert.equal(await page.locator('.impact-element[aria-current="true"]').count(),1);
    assert.equal(await page.locator('.impact-panel .call-evidence').isVisible(),false);
    await page.getByRole('button',{name:'Copy bookmark',exact:true}).click();await page.getByLabel('Bookmark URL').waitFor();const impactBookmark=await page.getByLabel('Bookmark URL').inputValue();
    const impactTab=await context.newPage();
    try{await impactTab.goto(impactBookmark);await impactTab.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.startsWith('Restored bookmark: Impact'));assert.deepEqual(await impactTab.locator('.impact-element').evaluateAll(cards=>cards.map(card=>({id:card.dataset.id,type:card.dataset.type,relation:card.dataset.relation,top:card.offsetTop,left:card.offsetLeft}))),impactBeforeReload);}
    finally{await impactTab.close();}
    impactObservations.push({case:'snapshot-fenced Impact reload/bookmark',record:savedImpact,scene:impactBeforeReload});
    const changedImpact=structuredClone(savedImpact);changedImpact.impact.identity='0'.repeat(64);await writeSaved(changedImpact);await page.reload();await page.waitForFunction(()=>document.querySelector('#saved-view-status').textContent.includes('snapshot is stale'));
    assert.equal(await page.locator('.impact-element').count(),0);await writeSaved(savedImpact);await page.reload();await restored();
    impactChecks.push('Impact reload/bookmark replays typed intents/filters/selection without cached facts or cursors; changed impact identity refuses even on same structural snapshot');

    await page.route('**/api/query',async route=>{const response=await route.fetch();const body=await response.json();body.generation='0'.repeat(64);await route.fulfill({response,body:JSON.stringify(body)});});
    await page.getByRole('button',{name:'Find impact',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.impact-panel .calls-status[role="status"]').textContent.includes('Index changed'));
    assert.equal(await page.locator('.impact-element').count(),0);await page.unroute('**/api/query');
    let impactRelease,impactStarted;const impactWaiting=new Promise(resolve=>{impactStarted=resolve;}),impactBarrier=new Promise(resolve=>{impactRelease=resolve;});
    await page.route('**/api/query',async route=>{const response=await route.fetch();impactStarted();await impactBarrier;try{await route.fulfill({response});}catch{}});
    await page.getByRole('button',{name:'Find impact',exact:true}).click();await impactWaiting;await page.getByRole('button',{name:'Cancel request',exact:true}).click();impactRelease();await page.waitForTimeout(100);
    assert.equal(await page.locator('.impact-element').count(),0);assert.match(await impactStatus.innerText(),/Stopped waiting/);await page.unroute('**/api/query');
    impactChecks.push('stale initial capture refused before scene publication; cancellation discards late actual backend reply');

    await findImpact('src/component00/main.py','import');await collectImpact();await page.setViewportSize({width:360,height:900});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.ok(await page.locator('.impact-panel form').evaluate(form=>[...form.querySelectorAll('input,select,button')].every(control=>{const rect=control.getBoundingClientRect();return rect.width<=innerWidth && rect.height>=44;})));
    const importButton=page.locator('.impact-element[data-relation="import"]').first().getByRole('button',{name:'Inspect import',exact:true});await importButton.focus();await page.keyboard.press('Enter');await page.locator('.impact-panel .call-evidence pre').waitFor();await page.keyboard.press('Escape');
    assert.equal(await importButton.evaluate(button=>button===document.activeElement),true);
    impactChecks.push('360px Impact forms/cards stay bounded; labelled44px controls and Enter/Escape import inspection preserve focus');
    if(process.env.REPO_GRAPH_UX_REPORT)await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-impact-narrow.png',fullPage:true});
    await page.setViewportSize({width:1440,height:1000});if(process.env.REPO_GRAPH_UX_REPORT)await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-impact.png'});
  }
  await Promise.all(responseReads);
  assert.deepEqual(errors,[]); assert.ok(loadMs<5000); checks.push('no browser errors; load under 5 seconds');
  if(process.env.REPO_GRAPH_UX_REPORT) {
    writeFileSync(process.env.REPO_GRAPH_UX_REPORT,JSON.stringify({files:graph.file_count,searchMode:process.env.REPO_GRAPH_UX_MODE || 'keyword',reranker:method,loadMs,searchToGraphMs,systemMetrics,checks,searchChecks,searchResponses,callsChecks,savedChecks,savedObservations,impactChecks,impactObservations,callsResponses,browserErrors:errors},null,2)+'\n');
  }
  console.log(JSON.stringify({files:graph.file_count,searchMode:process.env.REPO_GRAPH_UX_MODE || 'keyword',loadMs,searchToGraphMs,systemMetrics,checks,searchChecks,callsChecks,savedChecks,impactChecks,browserErrors:errors}));
} catch(error) {
  if(process.env.REPO_GRAPH_UX_REPORT) {
    writeFileSync(process.env.REPO_GRAPH_UX_REPORT+'-failure.json',JSON.stringify({checks,searchChecks,searchResponses,callsChecks,savedChecks,savedObservations,impactChecks,impactObservations,callsResponses,browserErrors:errors,failure:{name:error.name,message:error.message,stack:error.stack}},null,2)+'\n');
    await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'-failure.png',fullPage:true}).catch(()=>{});
  }
  throw error;
} finally {
  await browser.close(); server.kill('SIGTERM'); await closed; rmSync(scratch,{recursive:true,force:true});
}
