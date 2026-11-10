let token = sessionStorage.getItem('blackbox_token') || '';
export const origin = location.origin;
export function paired() { return !!token; }
export async function pair(value: string) {
  const result = await api('/pair', {method:'POST',body:JSON.stringify({token:value})});
  token = result.token;
  sessionStorage.setItem('blackbox_token', token);
}
export async function api(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  headers.set('X-BlackBox-Origin', origin);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (typeof init.body === 'string') headers.set('Content-Type', 'application/json');
  const res = await fetch(`/api${path}`, {...init,headers});
  const body = await res.json();
  if (!res.ok) throw new Error(body.error?.message || body.detail || `HTTP ${res.status}`);
  return body;
}
export async function events(sid: string, after: number, onEvent: (event:any)=>void, signal: AbortSignal, onConnection?: (message:string)=>void) {
  let cursor = after;
  let frame=0;
  let queued:any[]=[];
  const flush=()=>{
    if(frame)cancelAnimationFrame(frame);
    frame=0;
    const batch=queued;queued=[];
    if(!signal.aborted)batch.forEach(onEvent);
  };
  const deliver=(event:any)=>{
    if(event.type==='answer_delta'){
      queued.push(event);
      if(!frame)frame=requestAnimationFrame(flush);
    }else{flush();onEvent(event);}
  };
  while (!signal.aborted) {
    let reader:ReadableStreamDefaultReader<Uint8Array>|undefined;
    try {
      const res = await fetch(`/api/sessions/${sid}/events?after=${cursor}`, {headers:{Authorization:`Bearer ${token}`,'X-BlackBox-Origin':origin},signal});
      if (!res.ok) throw new Error(`Event stream HTTP ${res.status}`);
      onConnection?.('');
      reader = res.body!.getReader(); const decoder = new TextDecoder(); let pending='';
      while (!signal.aborted) {
        const {done,value} = await reader.read(); if(done)break;
        pending += decoder.decode(value,{stream:true});
        pending=pending.replace(/\r\n/g,'\n');
        let end;
        while ((end=pending.indexOf('\n\n'))>=0) {
          const frame = pending.slice(0,end); pending=pending.slice(end+2);
          const data=frame.split('\n').find(l=>l.startsWith('data: '));
          if(data){ const e=JSON.parse(data.slice(6)); if(e.event_id>cursor){cursor=e.event_id;deliver(e);} }
        }
      }
    } catch(e) { if(signal.aborted)return; onConnection?.(`Live updates disconnected. Reconnecting… ${String(e)}`); }
    finally {flush();if(reader){await reader.cancel().catch(()=>{});reader.releaseLock();}}
    if(!signal.aborted)onConnection?.('Live updates disconnected. Reconnecting…');
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
}
export async function attachment(sid:string, id:string) {
  const res=await fetch(`/api/sessions/${sid}/attachments/${id}`,{headers:{Authorization:`Bearer ${token}`,'X-BlackBox-Origin':origin}});
  if(!res.ok) throw new Error('Attachment unavailable');
  return URL.createObjectURL(await res.blob());
}
