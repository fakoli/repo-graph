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
  function functionSearch(value,captured) {
    const identities=value?.identities;
    for(const key of snapshotKeys) {
      const actual=identities?.[key==='generation' ? 'structural_generation' : key];
      if(!/^[0-9a-f]{64}$/.test(captured?.[key] || '') || actual!==captured[key])
        throw new Error('Function evidence snapshot mismatch; refresh this map');
    }
    const count=value.counts, budget=value.budgets, storage=value.storage;
    const integer=n=>Number.isSafeInteger(n) && n>=0, seconds=n=>typeof n==='number' && Number.isFinite(n) && n>=0;
    if(value.kind!=='functions' || !/^[0-9a-f]{64}$/.test(identities.evidence_generation || '') ||
        !Array.isArray(value.results) || value.results.length>10 || typeof value.truncated!=='boolean' ||
        value.stop_reason!==null && !/^[a-z_]{1,128}$/.test(value.stop_reason || '') ||
        !['exact','unknown'].includes(count?.documents?.knowledge) ||
        !(integer(count.documents.value) || count.documents.knowledge==='unknown' && count.documents.value===null) ||
        count.returned_passages!==value.results.length || !integer(count.returned_symbol_handles) || !integer(count.examined_candidates) ||
        !integer(budget?.max_entities) || budget.max_entities<1 || budget.max_entities>24 ||
        !integer(budget.max_response_bytes) || budget.max_response_bytes<1 || budget.max_response_bytes>32768 ||
        !integer(budget.max_excerpt_bytes) || budget.max_excerpt_bytes>8192 ||
        !seconds(budget.timeout_seconds) || budget.timeout_seconds<=0 || budget.timeout_seconds>.5 ||
        !seconds(storage?.elapsed_seconds) || !seconds(storage.model_encode_seconds) || !seconds(storage.rerank_seconds) || storage.hard_model_deadline!==false)
      throw new Error('Invalid bounded function evidence');
    const handles=new Set();let bytes=0;
    for(const row of value.results) {
      sourceHandle({id:'passage',path:row.path,source_sha256:row.file_sha256,range:row.range});
      if(row.evidence_kind!=='static_syntax' || !['python','javascript','typescript','go'].includes(row.language) ||
          !['parsed','partial_parse'].includes(row.extraction_state) || typeof row.text!=='string' ||
          !/^[0-9a-f]{64}$/.test(row.raw_digest || '') || typeof row.redacted!=='boolean' || typeof row.excerpt_truncated!=='boolean' ||
          !Array.isArray(row.members) || !row.members.length || row.members.length>24 ||
          row.score!==undefined && !seconds(row.score))throw new Error('Invalid captured function passage');
      bytes+=new TextEncoder().encode(row.text).length;
      for(const member of row.members) {
        sourceHandle({id:member.symbol_id,path:row.path,source_sha256:row.file_sha256,range:member.range});
        if(member.symbol_id!==`${row.path}:${member.range.start_byte}:${member.range.end_byte}` ||
            typeof member.name!=='string' || member.name.length>4096 || !['function','method','function_value'].includes(member.kind))
          throw new Error('Invalid captured function member');
        handles.add(member.symbol_id);
      }
    }
    if(handles.size!==count.returned_symbol_handles || handles.size>budget.max_entities || bytes>budget.max_excerpt_bytes)
      throw new Error('Function evidence budget mismatch');
    return value;
  }
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
    if(/^import:[0-9a-f]{64}$/.test(handle.id) && (value.kind!=='import' || value.role!=='import' || value.impact_schema!=='captured-impact-v1' ||
        value.impact_identity!==captured.impact_identity || value.targets_exhaustive!==false || typeof value.source_candidates_exhaustive!=='boolean' ||
        value.scope!=='admitted_source_candidates_runtime_unqualified'))throw new Error('Import source capture mismatch');
    const span=value.range;
    if(!span || ['start_byte','end_byte','start_line','end_line'].some(key=>!Number.isSafeInteger(span[key])) ||
        span.start_byte<handle.range.start_byte || span.end_byte>handle.range.end_byte || span.end_byte<span.start_byte ||
        span.start_line<handle.range.start_line || span.end_line>handle.range.end_line || span.end_line<span.start_line)
      throw new Error('Source excerpt range mismatch');
    return value;
  }
  const snapshotKeys=['generation','repository_identity','source_identity','analyzer_identity','config_identity'];
  const contractSchema='captured-contract-membership-v1';
  function capturedImpact(status) {
    const snapshot=capturedSnapshot(status),projection=status?.structural?.impact,receipt=projection?.receipt;
    if(!snapshot || projection?.state!=='ready' || projection.query_available!==true || receipt?.schema!=='captured-impact-v1' ||
        !sameSnapshot(snapshot,receipt) || !/^[0-9a-f]{64}$/.test(receipt.identity || '') || typeof receipt.contracts_available!=='boolean')return null;
    const available=receipt.contracts_available===true && receipt.contract_membership_schema===contractSchema;
    return {...snapshot,impact_identity:receipt.identity,contracts_available:available,contract_membership_schema:available ? contractSchema : null};
  }
  function impactFilters(value,relations) {
    const filters={};
    for(const [key,max] of [['services',8],['protocols',3],['namespaces',8]]) {
      const choices=value[key]===undefined ? null : value[key];
      if(choices!==null && (!Array.isArray(choices) || !choices.length || choices.length>max || new Set(choices).size!==choices.length ||
          choices.some(choice=>typeof choice!=='string' || !choice || choice.includes('\0') || new TextEncoder().encode(choice).length>256 || key==='protocols' && !['http','rpc','queue'].includes(choice))))throw new Error('Invalid bounded contract filters');
      if(choices!==null && !relations.includes('contract'))throw new Error('Contract filters require explicit contract relation');
      filters[key]=choices===null ? null : [...choices].sort();
    }
    return filters;
  }
  function contractRow(row) {
    const bounded=(value,max)=>typeof value==='string' && new TextEncoder().encode(value).length<=max;
    const identity=row.contract_identity,fields=['contract_service_id','namespace','protocol',...({http:['method','path','operation','operation_ref','request_schema','response_schema'],rpc:['rpc_service','operation','request_schema','response_schema'],queue:['topic','schema']}[row.protocol] || [])];
    if(!['http','rpc','queue'].includes(row.protocol) || row.relation_kind!=='explicit_'+row.protocol ||
        !['contract','contract_boundary'].includes(row.family) || row.site.role!==row.family ||
        !bounded(row.binding_id,256) || !row.binding_id || !bounded(row.service_id,256) || !row.service_id ||
        !['client','producer'].includes(row.endpoint_role) || row.namespace!==null && !bounded(row.namespace,1024) ||
        !identity || Array.isArray(identity) || Object.keys(identity).length!==fields.length || fields.some(key=>!Object.hasOwn(identity,key) || identity[key]!==null && !bounded(identity[key],1024)) ||
        identity.protocol!==row.protocol || identity.namespace!==row.namespace ||
        typeof row.contract_identity_asserted!=='boolean' || typeof row.partial!=='boolean' || row.runtime_qualified!==false ||
        !['current_profile_row','captured_source_binding'].includes(row.boundary_origin) ||
        !Array.isArray(row.evidence) || row.evidence.length>64 ||
        (row.family==='contract_boundary' || !row.contract_identity_asserted || row.partial) && (row.target!==null || row.targets_exhaustive!==false || row.certainty!=='unresolved') ||
        row.family==='contract' && (!row.target || !row.contract_identity_asserted || row.partial || row.certainty!=='resolved' || row.targets_exhaustive!==true || fields.some(key=>!identity[key])))throw new Error('Invalid imported contract evidence');
    const witnesses=new Map();
    for(const witness of row.evidence) {
      sourceHandle(witness);
      if(!/^[0-9a-f]{64}$/.test(witness.slice_sha256 || '') || !['reviewed_artifact_binding','structural_declaration','explicit_source_binding','original_contract_source'].includes(witness.source_role))throw new Error('Invalid contract source witness');
      if(witnesses.has(witness.id) && JSON.stringify(witnesses.get(witness.id))!==JSON.stringify(witness))throw new Error('Changed contract source witness');
      witnesses.set(witness.id,witness);
    }
    return [...witnesses.values()];
  }
  function impactSelector(value) {
    const exact=(row,keys)=>row && typeof row==='object' && !Array.isArray(row) && Object.keys(row).length===keys.length && keys.every(key=>Object.hasOwn(row,key));
    if(value?.kind==='git_change' && exact(value,['kind','base_revision']) && /^[0-9a-f]{40}$|^[0-9a-f]{64}$/.test(value.base_revision || ''))return {kind:value.kind,base_revision:value.base_revision};
    if(!exact(value,['kind','paths']) || value.kind!=='source_area' || !Array.isArray(value.paths) || !value.paths.length || value.paths.length>50)throw new Error('Invalid impact selection');
    for(const path of value.paths) {
      const parts=typeof path==='string' ? path.replace(/\/$/,'').split('/') : [];
      if(typeof path!=='string' || !path || new TextEncoder().encode(path).length>4096 || /[\\\0]/.test(path) || path!=='.' && parts.some(part=>!part || part==='.' || part==='..'))throw new Error('Invalid impact selection');
    }
    const result={kind:value.kind,paths:[...new Set(value.paths)].sort()};
    if(new TextEncoder().encode(JSON.stringify(result)).length>8192)throw new Error('Impact selection exceeds its budget');
    return result;
  }
  function impactFile(value) {
    if(!/^file:[0-9a-f]{64}$/.test(value?.id || '') || typeof value.path!=='string' || !value.path || value.path.length>4096 ||
        !/^[0-9a-f]{64}$/.test(value.source_sha256 || '') || !Number.isSafeInteger(value.source_bytes) || value.source_bytes<0 ||
        typeof value.admission_status!=='string' || !/^[a-z_]{1,64}$/.test(value.admission_status) || typeof value.kind!=='string' || value.kind.length>64 ||
        value.change_status!==undefined && !['A','D','M','T'].includes(value.change_status))throw new Error('Invalid captured impact file');
    impactSelector({kind:'source_area',paths:[value.path]});return value;
  }
  function impactPage(value,request,captured=null) {
    for(const key of snapshotKeys)if(!/^[0-9a-f]{64}$/.test(value?.[key] || '') || captured && value[key]!==captured[key])throw new Error('Index changed; select impact again');
    if(!/^[0-9a-f]{64}$/.test(value.impact_identity || '') || captured && value.impact_identity!==captured.impact_identity)throw new Error('Impact capture changed; select again');
    const filters=impactFilters(request,request.relations),hasContract=request.relations.includes('contract');
    if(hasContract && (!captured?.contracts_available || captured.contract_membership_schema!==contractSchema || value.contracts_available!==true || value.contract_membership_schema!==contractSchema))throw new Error('Captured contract membership unavailable');
    if(value.impact_schema!=='captured-impact-v1' || typeof value.contracts_available!=='boolean' || value.runtime_complete!==false || value.live_source_observed!==false ||
        value.historical_call_closure!=='unavailable_current_index_only' || value.scope?.claim!=='possible_captured_reachability' || value.scope.evidence_kind!=='static_syntax' ||
        value.selection?.seed!==null || JSON.stringify(impactSelector(value.selection.selector))!==JSON.stringify(impactSelector(request.selector)) ||
        value.scope.depth!==request.depth || value.scope.path_filter!=='' || value.scope.name_prefix!=='' || value.scope.role!=='all' ||
        JSON.stringify(value.scope.relations)!==JSON.stringify([...request.relations].sort()) || JSON.stringify(value.scope.certainties)!==JSON.stringify([...request.certainties].sort()) ||
        Object.keys(filters).some(key=>JSON.stringify(value.scope[key]===undefined && !hasContract ? null : value.scope[key])!==JSON.stringify(filters[key])))throw new Error('Impact filter or capture mismatch');
    if(typeof value.truncated!=='boolean' || value.cursor!==null && !/^[0-9a-f]{64}$/.test(value.cursor || '') ||
        !['exact','lower_bound','unknown'].includes(value.total_count?.kind) || !(Number.isSafeInteger(value.total_count.value) && value.total_count.value>=0 || value.total_count.kind==='unknown' && value.total_count.value===null) ||
        value.stop_reason!=null && !/^[a-z_]{1,128}$/.test(value.stop_reason))throw new Error('Invalid bounded impact page');
    if(!value.unknown_boundaries || typeof value.unknown_boundaries!=='object' || Array.isArray(value.unknown_boundaries) || Object.keys(value.unknown_boundaries).length>32 ||
        Object.entries(value.unknown_boundaries).some(([key,count])=>!/^[a-z_]{1,128}$/.test(key) || !Number.isSafeInteger(count) || count<0))throw new Error('Invalid impact boundaries');
    for(const key of ['rows','selected_files','selected_symbols','unavailable_paths'])if(!Array.isArray(value[key]) || value[key].length>8)throw new Error('Impact page exceeds its budget');
    const symbols=new Set(),files=new Set(),entities=new Set();
    const symbol=handle=>{sourceHandle(handle);if(typeof handle.name!=='string' || handle.name.length>256)throw new Error('Invalid impact declaration');symbols.add(handle.id);entities.add(handle.id);};
    const file=handle=>{impactFile(handle);files.add(handle.id);entities.add(handle.id);};
    value.selected_symbols.forEach(symbol);value.selected_files.forEach(file);
    for(const row of value.unavailable_paths) {
      if(!/^unavailable-source:[0-9a-f]{64}$/.test(row?.id || '') || row.source_sha256!==null || Object.hasOwn(row,'range') || typeof row.reason!=='string' || row.reason.length>256 || row.change_status!==undefined && !['A','D','M','T'].includes(row.change_status))throw new Error('Invalid unavailable impact path');
      impactSelector({kind:'source_area',paths:[row.path]});entities.add(row.id);
    }
    for(const row of value.rows) {
      sourceHandle(row.site);
      if(!['call','import','contract'].includes(row.relation) || !request.relations.includes(row.relation) || row.relation!=='contract' && row.site.role!==row.relation || !['resolved','candidate','unresolved'].includes(row.certainty) ||
          typeof row.targets_exhaustive!=='boolean' || typeof row.reason!=='string' || row.reason.length>256 || row.evidence_kind!=='static_syntax')throw new Error('Invalid impact relation evidence');
      if(row.relation==='call'){if(row.caller)symbol(row.caller);if(row.target)symbol(row.target);}
      else if(row.relation==='import'){file(row.importer);if(row.target)file(row.target);if(row.targets_exhaustive!==false || typeof row.source_candidates_exhaustive!=='boolean')throw new Error('Invalid import certainty');}
      else {
        if(!hasContract)throw new Error('Unexpected contract relation');
        if(!request.certainties.includes(row.certainty) || [['services',row.service_id],['protocols',row.protocol],['namespaces',row.namespace]].some(([key,choice])=>filters[key]!==null && !filters[key].includes(choice)))throw new Error('Contract row filter mismatch');
        if(row.caller)symbol(row.caller);if(row.target)symbol(row.target);
        for(const witness of contractRow(row))if(witness.source_role==='structural_declaration'){entities.add(witness.id);symbols.add(witness.id);}
      }
    }
    if(entities.size>request.limits.max_entities || value.rows.length>request.limits.max_edges || value.returned_entities!==entities.size ||
        value.returned_symbol_handles!==symbols.size || value.returned_file_handles!==files.size || value.returned_edges!==value.rows.length)throw new Error('Impact counters exceed their budget');
    if(request.selector.kind==='git_change' && (value.selection.git_change?.status!=='ready' || value.selection.git_change.base_revision!==request.selector.base_revision ||
        !/^[0-9a-f]{64}$/.test(value.selection.git_change.changes_sha256 || '') || value.selection.git_change.source_byte_affinity!=='unobserved_worktree'))throw new Error('Captured Git base unavailable');
    return value;
  }
  function impactScene(prior,page) {
    const files=new Map((prior?.files || []).map(value=>[value.id,value])),symbols=new Map((prior?.symbols || []).map(value=>[value.id,value]));
    const unavailable=new Map((prior?.unavailable || []).map(value=>[value.id,value])),sites=new Map((prior?.sites || []).map(row=>[row.site.id,{...row,targets:[...row.targets]}]));
    const witnesses=new Map();
    const witness=value=>{const typed={...sourceHandle(value),slice_sha256:value.slice_sha256,source_role:value.source_role};
      if(witnesses.has(value.id) && JSON.stringify(witnesses.get(value.id))!==JSON.stringify(typed))throw new Error('Changed contract source witness');witnesses.set(value.id,typed);};
    for(const row of sites.values())if(row.relation==='contract')row.evidence.forEach(witness);
    const file=value=>{const old=files.get(value.id);if(old && (old.path!==value.path || old.source_sha256!==value.source_sha256 || old.source_bytes!==value.source_bytes || old.admission_status!==value.admission_status || old.change_status && value.change_status && old.change_status!==value.change_status))throw new Error('Changed captured impact file');files.set(value.id,{...old,...value});};
    const symbol=value=>{if(symbols.has(value.id) && !sameHandle(symbols.get(value.id),value))throw new Error('Changed source handle');symbols.set(value.id,value);};
    page.selected_files.forEach(file);page.selected_symbols.forEach(symbol);
    for(const value of page.unavailable_paths){const old=unavailable.get(value.id);if(old && JSON.stringify(old)!==JSON.stringify(value))throw new Error('Changed unavailable impact path');unavailable.set(value.id,value);}
    for(const row of page.rows) {
      if(row.relation==='call' || row.relation==='contract'){
        if(row.caller)symbol(row.caller);if(row.target)symbol(row.target);
        if(row.relation==='contract')for(const value of contractRow(row)){witness(value);if(value.source_role==='structural_declaration')symbol({...value,name:symbols.get(value.id)?.name || 'Contract source witness'});}
      }else{file(row.importer);if(row.target)file(row.target);}
      let site=sites.get(row.site.id);
      if(site) {if(!sameHandle(site.site,row.site) || site.relation!==row.relation || site.certainty!==row.certainty || site.targets_exhaustive!==row.targets_exhaustive || site.reason!==row.reason || site.caller?.id!==row.caller?.id || site.importer?.id!==row.importer?.id || row.relation==='contract' &&
          ((site.target===null)!==(row.target===null) || site.target && !sameHandle(site.target,row.target) || ['binding_id','service_id','endpoint_role','protocol','namespace','contract_identity','contract_identity_asserted','partial','boundary_origin','evidence','relation_kind','family','runtime_qualified'].some(key=>JSON.stringify(site[key])!==JSON.stringify(row[key]))))throw new Error('Changed impact occurrence');}
      else {site={...row,targets:[]};sites.set(row.site.id,site);}
      if(row.target && !site.targets.some(value=>value.id===row.target.id))site.targets.push(row.target);
    }
    if(files.size+symbols.size+sites.size+unavailable.size>24)throw new Error('24 element impact limit reached');
    return {files:[...files.values()],symbols:[...symbols.values()],sites:[...sites.values()],unavailable:[...unavailable.values()]};
  }
  function capturedSnapshot(status) {
    const identities=status?.structural?.identities;
    if(status?.status!=='ok' || status.structural?.artifact_ready!==true ||
        snapshotKeys.some(key=>!/^[0-9a-f]{64}$/.test(identities?.[key] || '')))return null;
    return Object.fromEntries(snapshotKeys.map(key=>[key,identities[key]]));
  }
  function sameSnapshot(a,b) { return !!a && !!b && snapshotKeys.every(key=>a[key]===b[key]); }
  function savedView(value) {
    const exact=(row,keys)=>row && typeof row==='object' && !Array.isArray(row) &&
      Object.keys(row).length===keys.length && keys.every(key=>Object.hasOwn(row,key));
    const text=(s,max)=>typeof s==='string' && s.length<=max;
    const fail=()=>{throw new Error('Invalid saved view; clear it and select again');};
    if(!(value?.version===1 && exact(value,['version','view','scope','sort','kind','page','selected','snapshot','calls']) ||
        [2,3].includes(value?.version) && exact(value,['version','view','scope','sort','kind','page','selected','snapshot','calls','impact'])) ||
        !['system','atlas','tree','radial','treemap','table','matrix','search','calls','impact'].includes(value.view) || value.view==='impact' && ![2,3].includes(value.version) || value.version===3 && (value.view!=='impact' || value.impact===null) ||
        !text(value.scope,4096) || !['name','files','imports'].includes(value.sort) ||
        !['all','directory','file'].includes(value.kind) || !Number.isSafeInteger(value.page) || value.page<0 ||
        value.selected!==null && (!text(value.selected,8192) || !value.selected))fail();
    if(value.snapshot!==null && (!exact(value.snapshot,snapshotKeys) ||
        snapshotKeys.some(key=>!/^[0-9a-f]{64}$/.test(value.snapshot[key] || ''))))fail();
    if(value.calls!==null) {
      const calls=value.calls;
      if(value.view!=='calls' || value.snapshot===null ||
          !exact(calls,['root','direction','size','intents']) ||
          !exact(calls.root,['id','path','range','source_sha256']) ||
          !exact(calls.root.range,['start_byte','end_byte','start_line','end_line']) ||
          !['callees','callers'].includes(calls.direction) || ![1,4,8].includes(calls.size) ||
          !Array.isArray(calls.intents) || calls.intents.length>24)fail();
      sourceHandle(calls.root);
      for(const step of calls.intents) {
        if(!exact(step,['seed','operation','continuation','reset','limits']) || !text(step.seed,8192) || !step.seed ||
            !['callees','callers'].includes(step.operation) || typeof step.continuation!=='boolean' || typeof step.reset!=='boolean' ||
            !exact(step.limits,['max_entities','max_edges','max_response_bytes','max_excerpt_bytes']) ||
            !Number.isSafeInteger(step.limits.max_entities) || step.limits.max_entities<1 || step.limits.max_entities>8 ||
            !Number.isSafeInteger(step.limits.max_edges) || step.limits.max_edges<1 || step.limits.max_edges>8 ||
            step.limits.max_response_bytes!==32768 || step.limits.max_excerpt_bytes!==0)fail();
      }
    }
    if([2,3].includes(value.version) && value.impact!==null) {
      const impact=value.impact;
      if(value.view!=='impact' || value.calls!==null || value.snapshot===null || !exact(impact,['selector','relations','certainties','depth','size','identity','selected','intents',...(value.version===3 ? ['services','protocols','namespaces'] : [])]) ||
          !/^[0-9a-f]{64}$/.test(impact.identity || '') || ![1,2].includes(impact.depth) || ![1,4,8].includes(impact.size) ||
          impact.selected!==null && (!text(impact.selected,8192) || !impact.selected) || !Array.isArray(impact.intents) || impact.intents.length>24)fail();
      impactSelector(impact.selector);
      for(const [key,allowed] of [['relations',value.version===3 ? ['call','import','contract'] : ['call','import']],['certainties',['resolved','candidate','unresolved']]])if(!Array.isArray(impact[key]) || !impact[key].length || impact[key].length>allowed.length || new Set(impact[key]).size!==impact[key].length || impact[key].some(choice=>!allowed.includes(choice)))fail();
      if(value.version===3){if(!impact.relations.includes('contract'))fail();impactFilters(impact,impact.relations);}
      for(const step of impact.intents)if(!exact(step,['continuation','reset','limits']) || typeof step.continuation!=='boolean' || typeof step.reset!=='boolean' ||
          !exact(step.limits,['max_entities','max_edges','max_response_bytes','max_excerpt_bytes']) ||
          !Number.isSafeInteger(step.limits.max_entities) || step.limits.max_entities<1 || step.limits.max_entities>8 ||
          !Number.isSafeInteger(step.limits.max_edges) || step.limits.max_edges<1 || step.limits.max_edges>8 || step.limits.max_response_bytes!==32768 || step.limits.max_excerpt_bytes!==0)fail();
    }
    return value;
  }
  function bookmarkFragment(value) {
    const raw=JSON.stringify(savedView(value));
    if(new TextEncoder().encode(raw).length>32768)throw new Error('bookmark_overflow');
    const fragment='#view='+encodeURIComponent(raw);
    if(new TextEncoder().encode(fragment).length>32768)throw new Error('bookmark_overflow');
    return fragment;
  }
  function bookmarkView(fragment) {
    if(typeof fragment!=='string' || fragment.length>32768 || new TextEncoder().encode(fragment).length>32768)throw new Error('bookmark_overflow');
    if(!fragment.startsWith('#view='))throw new Error('invalid_bookmark');
    return savedView(JSON.parse(decodeURIComponent(fragment.slice(6))));
  }
  return {metrics,ordered,layout,csv,systemOverview,indexStatus,sourceHandle,functionSearch,queryPage,callScene,sourceEvidence,impactSelector,impactFilters,capturedImpact,impactPage,impactScene,capturedSnapshot,sameSnapshot,savedView,bookmarkFragment,bookmarkView};
})();
if (typeof module !== 'undefined') module.exports = RepoViews;
