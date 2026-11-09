import { useEffect, useRef, useState } from 'preact/hooks';
import { api, events as streamEvents, pair, paired } from './api';
import { Activity } from './components/Activity';
import { Messages } from './components/Messages';
import { ScopeDialog, SettingsDialog } from './components/Dialogs';

type DraftImage={file:File,url:string};
export function App() {
  const [authenticated,setAuthenticated]=useState(paired());
  const [error,setError]=useState('');
  const [models,setModels]=useState<any[]>([]);
  const [prefs,setPrefs]=useState<any>(null);
  const [sessions,setSessions]=useState<any[]>([]);
  const [current,setCurrent]=useState<any>(null);
  const [events,setEvents]=useState<any[]>([]);
  const [text,setText]=useState('');
  const [images,setImages]=useState<DraftImage[]>([]);
  const [sending,setSending]=useState(false);
  const [connection,setConnection]=useState('Checking Ollama…');
  const [connected,setConnected]=useState(false);
  const [dialog,setDialog]=useState('');
  const [activityOpen,setActivityOpen]=useState(false);
  const [sessionsOpen,setSessionsOpen]=useState(false);
  const [pairCode,setPairCode]=useState('');
  const transcript=useRef<HTMLDivElement>(null);
  const fileInput=useRef<HTMLInputElement>(null);
  const currentID=useRef('');
  const draft=useRef<Record<string,string>>({});
  const imageRef=useRef<DraftImage[]>([]);
  const busy=current?.status==='running' || sending;
  const lastRun=[...events].reverse().find(e=>e.type==='run_started')?.run_id;
  const usedCalls=events.filter(e=>e.type==='tool_finished'&&e.run_id===lastRun).length;
  const capability=models.find(m=>m.name===current?.model);

  useEffect(()=>{
    const fragment=new URLSearchParams(location.hash.slice(1));
    const code=fragment.get('pair');
    if(code){history.replaceState(null,'',location.pathname);pair(code).then(()=>setAuthenticated(true)).catch(e=>{setError(String(e));setAuthenticated(false);});}
  },[]);
  useEffect(()=>{imageRef.current=images;},[images]);
  useEffect(()=>()=>{imageRef.current.forEach(i=>URL.revokeObjectURL(i.url));},[]);
  async function refreshModels(){
    const p=await api('/preflight');setConnected(p.ollama.connected);setConnection(p.ollama.message);
    if(p.ollama.connected)setModels(await api('/models'));else setModels([]);
  }
  async function refreshSessions(){setSessions(await api('/sessions'));}
  async function select(sid:string){
    try {const value=await api(`/sessions/${sid}`);currentID.current=sid;setCurrent(value);setEvents(value.events);setText(draft.current[sid] || '');setImages(old=>{old.forEach(i=>URL.revokeObjectURL(i.url));return [];});setSessionsOpen(false);}
    catch(e){setError(String(e));}
  }
  async function create(){try {const s=await api('/sessions',{method:'POST',body:JSON.stringify({})});await refreshSessions();await select(s.id);}catch(e){setError(String(e));}}
  useEffect(()=>{
    if(!authenticated)return;
    (async()=>{try {setPrefs(await api('/config'));await refreshModels();const list=await api('/sessions');setSessions(list);if(list.length)await select(list[0].id);else await create();}catch(e){setError(String(e));}})();
  },[authenticated]);
  useEffect(()=>{
    if(!current?.id)return;
    const sid=current.id;
    const controller=new AbortController();
    const after=Math.max(0,...events.map(e=>e.event_id));
    streamEvents(sid,after,e=>{
      setEvents(old=>[...old,e].slice(-2000));
      if(e.type==='approval_required')setActivityOpen(true);
      if(e.type==='run_started')setCurrent((old:any)=>old?.id===sid?{...old,status:'running'}:old);
      if(['run_finished','run_failed','run_cancelled'].includes(e.type)) {
        api(`/sessions/${sid}`).then(s=>{if(currentID.current===sid)setCurrent(s);}).catch(err=>setError(String(err)));
        refreshSessions().catch(err=>setError(String(err)));
      }
    },controller.signal);
    return ()=>controller.abort();
  },[current?.id]);
  useEffect(()=>{transcript.current?.scrollTo({top:transcript.current.scrollHeight});},[current?.messages?.length,events.length]);

  function changeText(value:string){setText(value);if(current)draft.current[current.id]=value;}
  function addFiles(files:File[]) {
    setImages(old=>{
      const result=[...old];
      for(const file of files){
        if(!['image/png','image/jpeg','image/webp'].includes(file.type)){setError('Only PNG, JPEG and WebP images are accepted');continue;}
        if(file.size>8*1024*1024){setError('Each image must be at most 8 MiB');continue;}
        if(result.length>=4){setError('Maximum four images per message');break;}
        result.push({file,url:URL.createObjectURL(file)});
      }
      return result;
    });
  }
  async function send(e?:Event){
    e?.preventDefault();if(!current || busy || (!text.trim()&&!images.length))return;
    if(images.length&&!capability?.vision){setError('Select and probe a vision-capable model before sending images.');return;}
    const sid=current.id;
    setError('');setSending(true);
    const form=new FormData();form.append('text',text);images.forEach(i=>form.append('images',i.file));
    try {await api(`/sessions/${sid}/messages`,{method:'POST',body:form});changeText('');setImages(old=>{old.forEach(i=>URL.revokeObjectURL(i.url));return [];});const s=await api(`/sessions/${sid}`);if(currentID.current===sid)setCurrent(s);await refreshSessions();}
    catch(e){setError(String(e));}finally{setSending(false);}
  }
  async function stop(){try {await api(`/sessions/${current.id}/cancel`,{method:'POST'});const s=await api(`/sessions/${current.id}`);setCurrent(s);}catch(e){setError(String(e));}}
  async function model(value:string){try {setCurrent(await api(`/sessions/${current.id}`,{method:'PATCH',body:JSON.stringify({model:value})}));await refreshSessions();}catch(e){setError(String(e));}}
  async function removeSession(){if(!current)return;try {await api(`/sessions/${current.id}`,{method:'DELETE'});currentID.current='';setCurrent(null);const list=await api('/sessions');setSessions(list);if(list.length)await select(list[0].id);else await create();}catch(e){setError(String(e));}}
  async function exportSession(){try {const result=await api(`/sessions/${current.id}/export`);const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='blackbox-session.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){setError(String(e));}}
  async function retry(){
    if(!current||busy)return;
    setSending(true);setError('');
    try {await api(`/sessions/${current.id}/retry`,{method:'POST'});setCurrent(await api(`/sessions/${current.id}`));await refreshSessions();}
    catch(e){setError(String(e));}finally{setSending(false);}
  }
  if(!authenticated)return <main class="pair-screen"><h1 class="wordmark">&gt; BLACKBOX_</h1><p>Open the one-use pairing link printed in the server terminal.</p><p>Expired link? Restart <code>blackbox</code> to generate a new one.</p>{error&&<p role="alert" class="error">{error}</p>}<form onSubmit={e=>{e.preventDefault();pair(pairCode).then(()=>setAuthenticated(true)).catch(e=>setError(String(e)));}}><label class="field">Pairing code<input value={pairCode} onInput={e=>setPairCode(e.currentTarget.value)} autoComplete="off"/></label><button class="primary">Pair tab</button></form></main>;
  return <main class={`workbench ${prefs?.visual_effects?'effects':''}`}>
    <header class="topbar"><button class="drawer-toggle" onClick={()=>setSessionsOpen(!sessionsOpen)}>Sessions</button><span class="wordmark">&gt; BLACKBOX_</span><span class="connection"><span class={`led ${connected?'connected':''}`}/>{connected?'OLLAMA CONNECTED':'OLLAMA UNAVAILABLE'}</span><label class="sr-only" for="model">Model</label><select id="model" disabled={busy||!current} value={current?.model} onChange={e=>model(e.currentTarget.value)}>{current&&!models.some(m=>m.name===current.model)&&<option>{current.model}</option>}{models.map(m=><option value={m.name}>{m.name}</option>)}</select><span class="mono mode-label">{prefs?.mode==='manual'?'MANUAL':'AUTO SAFE'}</span><button class="drawer-toggle activity-toggle" onClick={()=>setActivityOpen(!activityOpen)}>Activity</button><button disabled={busy} onClick={()=>setDialog('settings')}>Settings</button></header>
    <div class="panes"><aside class={`sessions ${sessionsOpen?'open':''}`} aria-label="Sessions"><div class="pane-title">[ SESSIONS ]</div><button class="new-session" disabled={sending} onClick={create}>+ New session</button>{sessions.map(s=><button class={`session-row ${current?.id===s.id?'active':''}`} onClick={()=>select(s.id)}><span>{s.title}</span><small>{s.status} · {s.model}</small></button>)}<footer>ROOTLESS // LOCAL FIRST<br/>Tools execute on this host.</footer></aside>
      <section class="chat" aria-label="Chat"><div class="pane-title">[ TERMINAL / CHAT ] <span class={`caret ${busy?'active':''}`}>{busy?'RUNNING _':'IDLE'}</span></div><div class="scope-strip"><div class="row"><span>TARGET {current?.scope?.allowed_targets.join(', ') || 'NONE — authorization required for network tools'}</span><button disabled={!current||busy} onClick={()=>setDialog('scope')}>Scope</button></div><span>{current?.scope?'ATTESTED':'NO SCOPE'} · {usedCalls}/{prefs?.max_tool_calls || 20} tool calls/run · {prefs?.mode==='manual'?'MANUAL':'AUTO SAFE'}</span><div>{capability?`${capability.vision?'Vision':'No verified vision'} · ${capability.tools?'Tools':'No verified tools'} · ${capability.source}`:'Model not installed — select an installed model'}</div></div>
        <div class="transcript" ref={transcript}>
          {(!connected||!models.length)&&<div class="banner">{connection}<p><code>ollama serve</code> · <code>ollama pull qwen3.5:4b</code></p><button onClick={()=>refreshModels().catch(e=>setError(String(e)))}>Retry connection</button></div>}
          {current?.messages?.length?<Messages sid={current.id} messages={current.messages}/>:<div class="empty"><div class="mark">[ &gt;_ ]</div><h1>READY // WAITING FOR INPUT</h1><p>Chat with a local model, paste a screenshot, or configure an authorized lab scope. Every executed tool leaves a visible evidence record.</p></div>}
        </div>
        <div class="composer-area">{error&&<div class="banner error" role="alert">{error}<button class="dismiss" onClick={()=>setError('')} aria-label="Dismiss error">×</button></div>}
          <form class="composer" onSubmit={send} onDragOver={e=>e.preventDefault()} onDrop={e=>{e.preventDefault();if(!busy)addFiles(Array.from(e.dataTransfer?.files || []));}}>
            <div class="attachments">{images.map((image,i)=><div class="attachment"><img src={image.url} alt={`Preview ${image.file.name}`}/><small>{image.file.name}</small><small>{Math.ceil(image.file.size/1024)} KiB</small><button type="button" aria-label={`Remove ${image.file.name}`} onClick={()=>setImages(old=>{URL.revokeObjectURL(old[i].url);return old.filter((_,n)=>i!==n);})}>×</button></div>)}</div>
            <label class="sr-only" for="composer">Message</label><textarea id="composer" placeholder="$ Ask BlackBox…" value={text} maxLength={16000} disabled={!current||sending} onInput={e=>changeText(e.currentTarget.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send();}}} onPaste={e=>{const files=Array.from(e.clipboardData?.items || []).filter(i=>i.kind==='file'&&i.type.startsWith('image/')).map(i=>i.getAsFile()).filter((f):f is File=>!!f);if(files.length&&!busy){e.preventDefault();addFiles(files);const pasted=e.clipboardData?.getData('text/plain');if(pasted){const input=e.currentTarget;changeText(text.slice(0,input.selectionStart)+pasted+text.slice(input.selectionEnd));}}}}/>
            <div class="actions"><input ref={fileInput} class="sr-only" type="file" multiple accept="image/png,image/jpeg,image/webp" onChange={e=>{addFiles(Array.from(e.currentTarget.files || []));e.currentTarget.value='';}}/><button type="button" disabled={busy} onClick={()=>fileInput.current?.click()}>+ Image</button><small>{images.length}/4 · {busy?'RUNNING':'READY'}</small>{busy?<button class="stop" type="button" onClick={stop}>STOP</button>:<button class="primary" disabled={!current||!connected||(!text.trim()&&!images.length)}>RUN</button>}</div>
          </form><div class="composer-footer"><span>Enter run · Shift+Enter newline · Ctrl+V image</span><span>Untrusted content stays data.</span></div><div class="session-actions"><button disabled={busy||!current} onClick={retry}>Retry last message</button><button disabled={!current} onClick={exportSession}>Export</button><button disabled={sending||!current} onClick={removeSession}>Delete session</button></div>
        </div>
      </section><aside class={`activity ${activityOpen?'open':''}`} aria-label="Live activity"><div class="pane-title">[ LIVE ACTIVITY ]<button class="drawer-toggle" onClick={()=>setActivityOpen(false)}>Close</button></div><Activity events={events} sid={current?.id || ''} onError={setError}/></aside>
    </div>
    {dialog==='scope'&&current&&<ScopeDialog sid={current.id} scope={current.scope} close={()=>setDialog('')} saved={setCurrent} error={setError}/>}
    {dialog==='settings'&&prefs&&<SettingsDialog prefs={prefs} models={models} close={()=>setDialog('')} saved={(v:any)=>{setPrefs(v);if(current && v.model!==current.model)model(v.model);}} error={setError} refresh={refreshModels}/>}
  </main>;
}
