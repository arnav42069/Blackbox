import { useState } from 'preact/hooks';
import { api } from '../api';

export function Activity({events, sid, onError}: {events:any[],sid:string,onError:(v:string)=>void}) {
  const [expanded, setExpanded] = useState<Record<string,boolean>>({});
  const tools = new Map<string,any>();
  for (const event of events) {
    if (!event.tool_id) continue;
    const tool = tools.get(event.tool_id) || {id:event.tool_id,name:event.name,status:'running',chunks:'',commands:[],ts:event.ts};
    if (event.type==='approval_required') {tool.status='approval';tool.args=event.args;}
    if (event.type==='tool_started') {tool.status='running';tool.args=event.args;}
    if (event.type==='tool_command') {tool.command=event.argv;tool.commands.push(event.argv);}
    if (event.type==='tool_output') tool.chunks += event.chunk;
    if (event.type==='tool_finished') Object.assign(tool,event);
    if (event.type==='tool_denied') {tool.status='denied';tool.summary=event.reason;}
    tools.set(event.tool_id,tool);
  }
  async function approve(id:string, value:boolean) {
    try {await api(`/sessions/${sid}/approvals/${id}`,{method:'POST',body:JSON.stringify({approve:value})});}
    catch(e) {onError(String(e));}
  }
  return <div class="activity-log" aria-live="polite">
    {!events.length && <small>Idle // no tools executed</small>}
    {Array.from(tools.values()).map(tool=><article key={tool.id} class={`tool-card ${tool.status}`}>
      <time>{new Date(tool.ts*1000).toLocaleTimeString()}</time>
      <header>{tool.status.toUpperCase()} · {tool.name}</header>
      <small>Evidence {tool.id.slice(0,12)}</small>
      <pre>{JSON.stringify(tool.args || {},null,2)}</pre>
      {tool.command && <details><summary>Executed argv</summary><pre>{JSON.stringify(tool.commands,null,2)}</pre></details>}
      {tool.status==='approval' && <div class="row"><button onClick={()=>approve(tool.id,true)}>Approve once</button><button onClick={()=>approve(tool.id,false)}>Deny</button></div>}
      <p>{tool.summary || (tool.status==='running' ? 'Executing real tool…' : '')}</p>
      <small>Exit {tool.exit_code ?? '—'} · {tool.elapsed_ms ?? 0} ms{tool.truncated ? ' · TRUNCATED' : ''}</small>
      {(tool.stdout || tool.stderr || tool.chunks || tool.evidence?.length>0) && <>
        <pre class={expanded[tool.id] ? '' : 'collapsed'}>{[tool.stdout || tool.chunks,tool.stderr,tool.evidence?.length ? JSON.stringify(tool.evidence,null,2) : ''].filter(Boolean).join('\n')}</pre>
        <div class="row"><button class="copy" onClick={()=>setExpanded({...expanded,[tool.id]:!expanded[tool.id]})}>{expanded[tool.id]?'Collapse':'Expand'}</button><button class="copy" onClick={()=>navigator.clipboard.writeText(JSON.stringify(tool,null,2)).catch(e=>onError(String(e)))}>Copy evidence</button></div>
      </>}
    </article>)}
    {events.filter(e=>['run_started','assistant_status','assistant_progress','run_finished','run_failed','run_cancelled','scope_required','completion_review','metrics'].includes(e.type)).slice(-30).map(e=><div key={e.event_id} class={`log-entry ${e.type==='run_failed'?'error':''}`}><time>{new Date(e.ts*1000).toLocaleTimeString()}</time> {e.type.replaceAll('_',' ')}<div>{e.message || e.text || e.value || e.reason || (e.type==='metrics'?`${e.tool_calls} tools · ${e.http_requests} HTTP requests`:'')}</div></div>)}
  </div>;
}
