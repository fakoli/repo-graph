/* Pure view calculations, shared by the offline viewer and its Node checks. */
const RepoViews = (() => {
  function metrics(edges) {
    const result = new Map();
    for (const edge of edges) {
      for (const id of [edge.source,edge.target]) if (!result.has(id)) result.set(id,{incoming:0,outgoing:0});
      result.get(edge.source).outgoing += edge.count;
      result.get(edge.target).incoming += edge.count;
    }
    return result;
  }
  function ordered(nodes, sort, counts) {
    return [...nodes].sort((a,b) => {
      const score = node => sort === 'files' ? node.count : (counts.get(node.id)?.incoming || 0) + (counts.get(node.id)?.outgoing || 0);
      return (sort === 'name' ? 0 : score(b) - score(a)) || a.name.localeCompare(b.name);
    });
  }
  function layout(nodes, mode, edges=[]) {
    const boxes = new Map(), W = 236, H = 108;
    const root = nodes.find(node => node.kind === 'repository');
    const rest = nodes.filter(node => node !== root);
    const put = (node,x,y,width=W,height=H) => boxes.set(node.id,{x,y,width,height});
    let width = 900, height = 600;
    if (mode === 'treemap') {
      width = 1200; height = 760;
      const split = (items,x,y,w,h) => {
        if (!items.length) return;
        if (items.length === 1) { put(items[0],x,y,w,h); return; }
        const total = items.reduce((sum,node) => sum + Math.max(1,node.count),0);
        let cut = 1, weight = Math.max(1,items[0].count);
        while (cut < items.length - 1 && weight < total / 2) weight += Math.max(1,items[cut++].count);
        const ratio = weight / total;
        if (w >= h) { split(items.slice(0,cut),x,y,w*ratio,h); split(items.slice(cut),x+w*ratio,y,w*(1-ratio),h); }
        else { split(items.slice(0,cut),x,y,w,h*ratio); split(items.slice(cut),x,y+h*ratio,w,h*(1-ratio)); }
      };
      split(rest,40,40,1120,680);
    } else if (mode === 'radial') {
      const radius = Math.max(350,rest.length * Math.hypot(W,H) / (2*Math.PI) * 1.25);
      const cx = radius + W, cy = radius + H;
      width = cx*2; height = cy*2;
      if (root) put(root,cx-W/2,cy-H/2);
      rest.forEach((node,i) => { const angle = -Math.PI/2 + i*2*Math.PI/Math.max(1,rest.length); put(node,cx+radius*Math.cos(angle)-W/2,cy+radius*Math.sin(angle)-H/2); });
    } else if (mode === 'system') {
      let levels = new Map(nodes.map(node => [node.id,node.layer]));
      if (edges.length) {
        // ponytail: cubic reachability is bounded to 12 system components; collapse cycles before layering.
        const reach = new Map(nodes.map(node => [node.id,new Set([node.id])]));
        for (const edge of edges) if (reach.has(edge.source) && reach.has(edge.target)) reach.get(edge.source).add(edge.target);
        for (const middle of nodes) for (const source of nodes) if (reach.get(source.id).has(middle.id)) for (const target of reach.get(middle.id)) reach.get(source.id).add(target);
        const component = new Map();
        let group = 0;
        for (const node of nodes) if (!component.has(node.id)) {
          for (const other of nodes) if (reach.get(node.id).has(other.id) && reach.get(other.id).has(node.id)) component.set(other.id,group);
          group++;
        }
        const ranks = Array(group).fill(0), connected = new Set();
        for (let pass=0;pass<group;pass++) for (const edge of edges) {
          const a=component.get(edge.source),b=component.get(edge.target);
          if (a === undefined || b === undefined) continue;
          connected.add(edge.source); connected.add(edge.target);
          if (a !== b) ranks[b]=Math.max(ranks[b],ranks[a]+1);
        }
        const last = Math.max(0,...ranks)+1;
        levels = new Map(nodes.map(node => [node.id,connected.has(node.id) ? ranks[component.get(node.id)] : node.layer === 0 ? 0 : last]));
      }
      const layers = [...new Set(levels.values())].sort((a,b) => a-b);
      const columns = layers.map(layer => nodes.filter(node => levels.get(node.id) === layer));
      const rows = Math.max(1,...columns.map(column => Math.ceil(column.length/(column.length>6 ? 2 : 1))));
      let x = 60;
      for (const column of columns) {
        const columnRows = Math.ceil(column.length/(column.length>6 ? 2 : 1));
        column.forEach((node,index) => put(node,x+Math.floor(index/columnRows)*(W+56),90+(rows-columnRows)*(H+24)/2+(index%columnRows)*(H+24)));
        x += (column.length>6 ? 2*W+56 : W)+130;
      }
      width = Math.max(W+120,x-70); height = 180+rows*(H+24);
    } else if (mode === 'tree') {
      const columns = Math.min(4,Math.max(1,rest.length));
      width = 160 + columns*(W+60);
      if (root) put(root,(width-W)/2,40);
      rest.forEach((node,i) => put(node,80+(i%columns)*(W+60),root ? 260+Math.floor(i/columns)*(H+70) : 80+Math.floor(i/columns)*(H+70)));
      height = (root ? 300 : 120) + Math.ceil(rest.length/columns)*(H+70);
    } else {
      const columns = root ? [[root]] : [];
      for (let i=0;i<rest.length;i+=6) columns.push(rest.slice(i,i+6));
      const rows = Math.max(1,...columns.map(column => column.length));
      columns.forEach((column,ci) => column.forEach((node,ri) => put(node,80+ci*(W+164),110+(rows-column.length)*(H+52)/2+ri*(H+52))));
      width = 160+Math.max(0,columns.length-1)*(W+164)+W; height = 220+rows*(H+52);
    }
    return {boxes,width,height};
  }
  function csv(nodes, counts) {
    const cell = value => {
      let text = String(value ?? '');
      if (/^[=+@\-\t\r]/.test(text)) text = "'" + text;
      return '"' + text.replaceAll('"','""') + '"';
    };
    const rows = [['Path','Type','Files','Incoming imports','Outgoing imports','Role','Source paths']];
    for (const node of nodes) rows.push([node.name,node.kind,node.count,counts.get(node.id)?.incoming || 0,counts.get(node.id)?.outgoing || 0,node.role || '',JSON.stringify(node.paths || [node.name])]);
    return rows.map(row => row.map(cell).join(',')).join('\r\n') + '\r\n';
  }
  function systemOverview(system, query='') {
    const all = system?.nodes || [], nodes = all.slice(0,12).filter(node =>
      (node.name + ' ' + (node.summary || '')).toLowerCase().includes(query));
    const ids = new Set(nodes.map(node => node.id));
    const edges = (system?.edges || []).filter(edge => ids.has(edge.source) && ids.has(edge.target));
    return {nodes,edges:edges.slice(0,40),relations:edges.length,
      imports:edges.reduce((sum,edge) => sum + edge.count,0),omittedAreas:Math.max(0,all.length-12)};
  }
  function indexStatus(raw, offline=false) {
    // Presentation consumes captured status only. Counts never establish readiness or live freshness.
    const count = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : 'unknown';
    const text = value => typeof value === 'string' ? value.slice(0,128) : 'unknown';
    const states = new Set(['ready','not_scanned','not_indexed','updating','interrupted','failed','stale','publication_uncertain','unavailable','unknown_legacy']);
    const state = part => states.has(part?.state) ? part.state.replaceAll('_',' ') : 'unknown';
    const structural = raw?.structural, receipt = structural?.receipt, coverage = receipt?.coverage;
    const valid = raw?.status === 'ok';
    const ready = valid && structural?.artifact_ready === true;
    const freshness = offline ? `Captured freshness: ${['current','stale'].includes(structural?.freshness) ? structural.freshness : 'unknown'} · Live freshness unobserved` :
      structural?.freshness === 'current' ? 'Freshness: current against supplied source identity' :
      structural?.freshness === 'stale' ? 'Freshness: stale' : 'Live freshness unobserved';
    const summary = [`Structural: ${valid ? state(structural) : 'unavailable'}${ready ? ' · captured artifact ready' : ''}`,freshness];
    if (coverage) {
      const files = coverage.file_status || coverage.status_counts || {};
      summary.push(`${count(coverage.files_total)} admitted files · ${count(files.parsed ?? 0)} parsed · ${count(files.partial_parse ?? 0)} partial · ${count(coverage.files_unsupported)} unsupported`);
      const sites = coverage.sites_by_role_certainty || {};
      summary.push(`Unknown sites: ${count(sites.call?.unresolved ?? 0)} calls · ${count(sites.reference?.unresolved ?? 0)} references`);
    } else summary.push('Structural coverage unavailable');
    const details = [], errors = [];
    if (!raw) errors.push('No captured structural status. Serve the index to read its status.');
    else if (!valid) errors.push(raw.status === 'bounded_stop' ? 'Status read stopped at its storage deadline.' : 'Index status unavailable.');
    if (coverage) {
      const labels = {parsed:'Parsed',partial_parse:'Partial parse',unsupported_language:'Unsupported',excluded_size:'Excluded by size',configuration:'Configuration',go_filename_excluded:'Go filename excluded',source_error:'Source errors',pending:'Pending'};
      for (const [key,value] of Object.entries(coverage.file_status || coverage.status_counts || {}).slice(0,32))
        details.push(`${labels[key] || 'Other file status'}: ${count(value)}`);
      details.push(`Parser errors: ${count(coverage.parser_error_count)}`);
      if (coverage.parser_error_count > 0) errors.push(`${count(coverage.parser_error_count)} parser errors; partial files remain visible.`);
      if (coverage.file_status?.excluded_size > 0) errors.push(`${count(coverage.file_status.excluded_size)} files excluded by size.`);
      details.push('Coverage is the admitted inventory. Discovery skips outside it were not measured.');
      for (const [language,group] of Object.entries(coverage.by_language || {}).slice(0,32)) {
        const values = group.file_status || {};
        details.push(`${text(language)}: ${count(group.files_total)} files · ${count(values.parsed ?? 0)} parsed · ${count(values.partial_parse ?? 0)} partial · ${count(values.unsupported_language ?? 0)} unsupported`);
      }
      if (coverage.language_overflow?.languages > 0) details.push(`${count(coverage.language_overflow.languages)} additional languages · ${count(coverage.language_overflow.files_total)} files`);
      for (const role of ['call','reference']) {
        const sites = coverage.sites_by_role_certainty?.[role] || {};
        details.push(`${role === 'call' ? 'Call' : 'Reference'} sites: ${count(sites.resolved ?? 0)} resolved · ${count(sites.candidate ?? 0)} candidate · ${count(sites.unresolved ?? 0)} unresolved`);
      }
    }
    const identities = structural?.identities || {};
    for (const key of ['generation','repository_identity','source_identity','analyzer_identity','config_identity'])
      if (/^[0-9a-f]{64}$/.test(identities[key] || '')) details.push(`${key.replaceAll('_',' ')}: ${identities[key]}`);
    const versions = receipt?.versions;
    if (versions) {
      details.push(`Schema: ${text(versions.schema)} · rules: ${text(versions.rules)}`);
      for (const [language,version] of Object.entries(versions.grammars || {}).slice(0,32)) details.push(`Grammar ${text(language)}: ${text(version)}`);
    }
    const revision = receipt?.revision_dirty;
    details.push(`Revision: ${/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(revision?.revision || '') ? revision.revision : 'unknown'} · dirty: ${revision?.dirty === true ? 'yes at capture' : revision?.dirty === false ? 'no at capture' : 'unobserved'}`);
    const reasons = {source_changed_before_publication:'source changed before publication',repository_replaced_before_publication:'repository replaced before publication',cancelled:'cancelled',deadline_exceeded:'deadline exceeded'};
    for (const [label,part] of [['Structural',structural],['File semantic',raw?.semantic_index],['Function evidence',raw?.function_evidence]]) {
      details.push(`${label}: ${state(part)} · artifact ${valid && part?.artifact_ready === true ? 'ready' : 'unavailable'} · query ${valid && part?.query_available === true && !offline ? 'available' : 'unavailable offline or backend absent'}`);
      const attempt = part?.last_attempt;
      if (attempt && ['failed','interrupted','updating','publication_uncertain'].includes(attempt.status)) {
        const attribution = ['captured_repository','unpublished_repository'].includes(part.attempt_attribution) ? '' : 'unrelated or unverified ';
        errors.push(`${label}: ${attribution}${attempt.status.replaceAll('_',' ')}${reasons[attempt.reason] ? ' · '+reasons[attempt.reason] : ''}.`);
      }
    }
    const semantic = raw?.semantic_index, functions = raw?.function_evidence;
    details.push(`File semantic model: ${text(semantic?.model)} · backend ${semantic?.backend_available === true && !offline ? 'available' : semantic?.backend_available === false ? 'absent' : 'unobserved'}`);
    if (functions) details.push(`Function semantic: ${state({state:functions.semantic_state})} · model ${text(functions.model)} · query ${valid && functions.semantic_query_available === true && !offline ? 'available' : 'unavailable'}`);
    const catalog = raw?.semantic_index?.catalog_receipt;
    if (catalog) {
      details.push(`File keyword catalogue: ${count(catalog.documents)} documents · ${count(catalog.truncated)} truncated · ${count(catalog.failed)} failed`);
      if (catalog.truncated > 0 || catalog.failed > 0) errors.push(`File keyword catalogue: ${count(catalog.truncated)} truncated · ${count(catalog.failed)} failed.`);
    }
    return {summary,details,errors,ready};
  }
  function sourceHandle(value) {
    const span=value?.range;
    if (typeof value?.id !== 'string' || !value.id || value.id.length>8192 ||
        typeof value.path !== 'string' || !value.path || value.path.length>4096 ||
        !/^[0-9a-f]{64}$/.test(value.source_sha256 || '') || !span ||
        ['start_byte','end_byte','start_line','end_line'].some(key=>!Number.isSafeInteger(span[key])) ||
        span.start_byte<0 || span.end_byte<span.start_byte || span.start_line<1 || span.end_line<span.start_line)
      throw new Error('Invalid captured source handle');
    return {id:value.id,path:value.path,range:{start_byte:span.start_byte,end_byte:span.end_byte,start_line:span.start_line,end_line:span.end_line},source_sha256:value.source_sha256};
  }
  function sameHandle(a,b) { return JSON.stringify(sourceHandle(a))===JSON.stringify(sourceHandle(b)); }
  function queryPage(value,operation,seed=null,captured=null) {
    for(const key of ['generation','repository_identity','source_identity','analyzer_identity']) {
      if (!/^[0-9a-f]{64}$/.test(value?.[key] || '')) throw new Error('Query capture unavailable');
      if (captured && value[key]!==captured[key]) throw new Error('Index changed; select a symbol again');
    }
    if (!Array.isArray(value.rows) || value.rows.length>8 || typeof value.truncated!=='boolean' ||
        value.cursor!==null && !/^[0-9a-f]{64}$/.test(value.cursor || '') ||
        !['exact','lower_bound','unknown'].includes(value.total_count?.kind) ||
        !(Number.isSafeInteger(value.total_count.value) && value.total_count.value>=0 || value.total_count.kind==='unknown' && value.total_count.value===null) ||
        value.stop_reason!=null && (typeof value.stop_reason!=='string' || !/^[a-z_]{1,128}$/.test(value.stop_reason))) throw new Error('Invalid bounded query page');
    for(const row of value.rows) {
      if(operation==='symbol') {
        sourceHandle(row);
        if(typeof row.name!=='string' || row.name.length>256) throw new Error('Invalid declaration');
      } else {
        sourceHandle(row.site);
        if(row.site.role!=='call' || !['resolved','candidate','unresolved'].includes(row.certainty) ||
            typeof row.targets_exhaustive!=='boolean' || typeof row.reason!=='string' || row.reason.length>256)
          throw new Error('Invalid call occurrence');
        for(const handle of [row.caller,row.target]) if(handle) sourceHandle(handle);
        if((operation==='callers' ? row.target?.id : row.caller?.id)!==seed) throw new Error('Call seed mismatch');
      }
    }
    return value;
  }
  function callScene(prior,page,seed) {
    const handles=new Map((prior?.handles || [seed]).map(handle=>[handle.id,handle]));
    const sites=new Map((prior?.sites || []).map(row=>[row.site.id,{...row,targets:[...row.targets]}]));
    for(const row of page.rows) {
      for(const handle of [row.caller,row.target]) if(handle) {
        if(handles.has(handle.id) && !sameHandle(handles.get(handle.id),handle)) throw new Error('Changed source handle');
        handles.set(handle.id,handle);
      }
      let site=sites.get(row.site.id);
      if(site) {
        if(!sameHandle(site.site,row.site) || site.certainty!==row.certainty || site.targets_exhaustive!==row.targets_exhaustive ||
            site.reason!==row.reason || site.caller?.id!==row.caller?.id) throw new Error('Changed occurrence evidence');
      } else {
        site={site:row.site,caller:row.caller,targets:[],certainty:row.certainty,targets_exhaustive:row.targets_exhaustive,reason:row.reason,reason_truncated:row.reason_truncated};
        sites.set(row.site.id,site);
      }
      if(row.target && !site.targets.some(target=>target.id===row.target.id)) site.targets.push(row.target);
    }
    if(handles.size+sites.size>24) throw new Error('24 element scene limit reached');
    return {handles:[...handles.values()],sites:[...sites.values()]};
  }
  function sourceEvidence(value,handle,captured) {
    if(value?.schema!=='captured-source-v1' || value.status!=='ok' || value.generation!==captured.generation ||
        value.evidence_kind!=='static_syntax' || !sameHandle(value.handle,handle) ||
        value.identities?.structural_generation!==captured.generation ||
        ['repository_identity','source_identity','analyzer_identity'].some(key=>value.identities?.[key]!==captured[key]) ||
        value.provenance?.source_sha256!==handle.source_sha256 || !/^[0-9a-f]{64}$/.test(value.raw_digest || '') ||
        typeof value.text!=='string' || typeof value.redacted!=='boolean' || typeof value.truncated!=='boolean')
      throw new Error('Source evidence identity mismatch');
    const span=value.range;
    if(!span || ['start_byte','end_byte','start_line','end_line'].some(key=>!Number.isSafeInteger(span[key])) ||
        span.start_byte<handle.range.start_byte || span.end_byte>handle.range.end_byte || span.end_byte<span.start_byte ||
        span.start_line<handle.range.start_line || span.end_line>handle.range.end_line || span.end_line<span.start_line)
      throw new Error('Source excerpt range mismatch');
    return value;
  }
  return {metrics,ordered,layout,csv,systemOverview,indexStatus,sourceHandle,queryPage,callScene,sourceEvidence};
})();
if (typeof module !== 'undefined') module.exports = RepoViews;
