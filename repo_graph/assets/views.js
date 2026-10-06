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
      const rows = Math.max(1,...columns.map(column => column.length));
      columns.forEach((column,ci) => column.forEach((node,ri) => put(node,60+ci*(W+130),90+(rows-column.length)*(H+65)/2+ri*(H+65))));
      width = 120+Math.max(0,columns.length-1)*(W+130)+W; height = 180+rows*(H+65);
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
  return {metrics,ordered,layout,csv};
})();
if (typeof module !== 'undefined') module.exports = RepoViews;
