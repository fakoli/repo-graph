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
if (!process.env.REPO_GRAPH_UX_OUTPUT) {
  for (let i=0;i<75;i++) { const dir=resolve(repo,'src','component'+String(i).padStart(2,'0')); mkdirSync(dir,{recursive:true}); writeFileSync(resolve(dir,'main.py'),'def process():\n    """Apply access control permissions to a request."""\n'); }
  const scan=spawnSync(python,['scripts/repo_graph.py','map',repo,'--output',output],{encoding:'utf8'});
  assert.equal(scan.status,0,scan.stderr);
  const analysis=spawnSync(python,['scripts/repo_graph.py','analyze',repo,'--output',output,'--mode','serial'],{encoding:'utf8'});
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
const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
page.on('pageerror',error=>errors.push(error.message));
const checks=[];
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
  await page.getByLabel('Repository search query').fill(process.env.REPO_GRAPH_UX_QUERY || 'access control permissions');
  await page.getByRole('button',{name:'Search',exact:true}).click();
  await page.locator('.search-result').first().waitFor({timeout:30000});
  if(method!=='none') assert.match(await page.locator('.search-status[role="status"]').innerText(),/Reranker: (used|cached)/);
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
  assert.deepEqual(errors,[]); assert.ok(loadMs<5000); checks.push('no browser errors; load under 5 seconds');
  if(process.env.REPO_GRAPH_UX_REPORT) {
    writeFileSync(process.env.REPO_GRAPH_UX_REPORT,JSON.stringify({files:graph.file_count,searchMode:process.env.REPO_GRAPH_UX_MODE || 'keyword',reranker:method,loadMs,searchToGraphMs,systemMetrics,checks,browserErrors:errors},null,2)+'\n');
  }
  console.log(JSON.stringify({files:graph.file_count,searchMode:process.env.REPO_GRAPH_UX_MODE || 'keyword',loadMs,searchToGraphMs,systemMetrics,checks,browserErrors:errors}));
} finally {
  await browser.close(); server.kill('SIGTERM'); await closed; rmSync(scratch,{recursive:true,force:true});
}
