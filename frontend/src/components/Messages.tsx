import { useEffect, useState } from 'preact/hooks';
import { marked, Renderer } from 'marked';
import DOMPurify from 'dompurify';
import { attachment } from '../api';

function Image({sid,id}:{sid:string,id:string}) {
  const [url,setUrl] = useState('');
  useEffect(()=>{
    let disposed=false, objectURL='';
    attachment(sid,id).then(value=>{objectURL=value;if(disposed)URL.revokeObjectURL(value);else setUrl(value);}).catch(()=>{});
    return ()=>{disposed=true;if(objectURL)URL.revokeObjectURL(objectURL);};
  },[sid,id]);
  return url ? <img src={url} alt="Attached screenshot" loading="lazy"/> : <small>Loading attachment…</small>;
}

const escape=(text:string)=>text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const renderer=new Renderer();
renderer.code=({text,lang})=>{
  // Highlight only escaped lexical tokens. No model HTML is trusted.
  const tokens=text.match(/(\/\/[^\n]*|#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:const|let|var|def|class|import|from|return|if|else|for|while|async|await|true|false|null|None|True|False|function|select|SELECT|WHERE|FROM)\b|\b\d+(?:\.\d+)?\b|[\s\S])/g)||[];
  const code=tokens.map(t=>{
    const kind=/^(?:\/\/|#)/.test(t)?'comment':/^["']/.test(t)?'string':/^\d/.test(t)?'number':t.length>1&&/^\w+$/.test(t)?'keyword':'';
    return kind?`<span class="syntax-${kind}">${escape(t)}</span>`:escape(t);
  }).join('');
  return `<div class="code-block"><button type="button" class="copy-code" aria-label="Copy code">Copy code</button><small>${escape(lang||'text')}</small><pre><code>${code}</code></pre></div>`;
};

function Markdown({content}:{content:string}) {
  const [notice,setNotice]=useState('');
  async function copy(text:string){try{await navigator.clipboard.writeText(text);setNotice('Copied');}catch{setNotice('Copy failed');}}
  return <><div class="markdown" onClick={e=>{
    const button=(e.target as HTMLElement).closest('.copy-code');
    if(button)copy(button.parentElement?.querySelector('code')?.textContent || '');
  }} dangerouslySetInnerHTML={{__html:DOMPurify.sanitize(marked.parse(content,{async:false,renderer}) as string,{FORBID_TAGS:['img','style','iframe','form','input','video','audio'],FORBID_ATTR:['style']})}}/>
    <button class="copy-message" onClick={()=>copy(content)}>Copy message</button><small role="status">{notice}</small></>;
}

function ToolSummaries({ids,events}:{ids:string[],events:any[]}) {
  const rows=events.filter(e=>e.type==='tool_finished'&&ids.includes(e.tool_id));
  return rows.length?<details class="conversation-tools"><summary>Tool executions ({rows.length})</summary>{rows.map(e=><p key={e.tool_id}><strong>{e.name}</strong> · {e.status} · {e.target || e.evidence?.[0]?.authorized_target || 'local'}<br/>{e.summary}<br/><small>Evidence {e.tool_id} · {new Date(e.ts*1000).toLocaleString()}</small></p>)}</details>:null;
}

export function Messages({messages,sid,events=[]}:{messages:any[],sid:string,events?:any[]}) {
  return <>{messages.filter(m=>m.role!=='tool' && m.metadata?.conversation_kind!=='tool_call' && (m.content || m.attachment_ids?.length)).map(m=>
    m.metadata?.conversation_kind==='progress' ? <details class="conversation-progress" key={m.id}><summary>{m.metadata?.target&&`[${m.metadata.target}] `}{m.content.split('\n')[0].slice(0,160)}</summary><Markdown content={m.content}/><ToolSummaries ids={m.metadata.tool_ids || []} events={events}/></details> :
    <article key={m.id} class={`message ${m.role}`}>
      <div class="role">{m.role==='user'?'USER':'BLACKBOX'}<small>{new Date(m.created_at*1000).toLocaleTimeString()}</small></div>
      {m.metadata?.target&&<small>Target {m.metadata.target} · Run {m.metadata.run_id}</small>}
      <Markdown content={m.content}/>
      <ToolSummaries ids={m.metadata?.tool_ids || []} events={events}/>
      {m.metadata?.state==='streaming'&&<span class="stream-cursor" aria-label="Generating response">▍</span>}
      <div class="attachments">{(m.attachment_ids || []).map((id:string)=><Image key={id} sid={sid} id={id}/>)}</div>
    </article>)}</>;
}
