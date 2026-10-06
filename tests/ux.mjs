import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { once } from 'node:events';

const scratch = mkdtempSync(resolve(tmpdir(),'repo-graph-ux-'));
const repo = resolve(scratch,'source'), output = process.env.REPO_GRAPH_UX_OUTPUT || resolve(scratch,'output');
const python = process.env.REPO_GRAPH_PYTHON || 'python3';
if (!process.env.REPO_GRAPH_UX_OUTPUT) {
  for (let i=0;i<75;i++) { const dir=resolve(repo,'src','component'+String(i).padStart(2,'0')); mkdirSync(dir,{recursive:true}); writeFileSync(resolve(dir,'main.py'),'def process():\n    """Apply access control permissions to a request."""\n'); }
  const scan=spawnSync(python,['scripts/repo_graph.py','map',repo,'--output',output],{encoding:'utf8'});
  assert.equal(scan.status,0,scan.stderr);
}
const server=spawn(python,['scripts/repo_graph.py','serve',output,'--offline'],{stdio:['ignore','pipe','pipe']});
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
  for(const mode of ['system','atlas','tree','radial','treemap','table','matrix']) {
    await page.selectOption('#view-mode',mode);
    if(['table','matrix'].includes(mode)) assert.ok(await page.locator('#data-panel').isVisible());
    else assert.ok(await page.locator('#map').isVisible());
    assert.ok(await page.locator('.node').count()<=24); checks.push('view '+mode);
  }
  await page.click('#tab-search');
  await page.getByLabel('Search method').selectOption('keyword');
  await page.getByLabel('Repository search query').fill(process.env.REPO_GRAPH_UX_QUERY || 'access control permissions');
  await page.getByRole('button',{name:'Search',exact:true}).click();
  await page.locator('.search-result').first().waitFor({timeout:30000});
  const resultPath=await page.locator('.search-result h2').first().innerText();
  await page.getByRole('button',{name:'Open in diagram →'}).first().click();
  assert.equal(await page.locator('.inspector-path').innerText(),resultPath); checks.push('search to source diagram');
  await page.click('#tab-search');
  assert.equal(await page.getByLabel('Repository search query').inputValue(),process.env.REPO_GRAPH_UX_QUERY || 'access control permissions'); checks.push('query preserved');
  await page.setViewportSize({width:800,height:900});
  assert.ok(await page.getByRole('button',{name:'Search',exact:true}).isVisible()); checks.push('narrow viewport search');
  assert.deepEqual(errors,[]); assert.ok(loadMs<5000); checks.push('no browser errors; load under 5 seconds');
  if(process.env.REPO_GRAPH_UX_REPORT) {
    await page.setViewportSize({width:1440,height:1000}); await page.click('#tab-system');
    await page.screenshot({path:process.env.REPO_GRAPH_UX_REPORT+'.png'});
    writeFileSync(process.env.REPO_GRAPH_UX_REPORT,JSON.stringify({files:graph.file_count,loadMs,checks,browserErrors:errors},null,2)+'\n');
  }
  console.log(JSON.stringify({files:graph.file_count,loadMs,checks,browserErrors:errors}));
} finally {
  await browser.close(); server.kill('SIGTERM'); await closed; rmSync(scratch,{recursive:true,force:true});
}
