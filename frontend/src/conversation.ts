// Snapshots make journal replay idempotent, including after a mid-stream reload.
export function conversationMessages(messages: any[], events: any[]) {
  const result = messages.map(m=>({...m}));
  const index = new Map(result.map((m,i)=>[m.id,i]));
  for (const e of events) {
    if (!e.message_id || !['user_message','assistant_message_started','answer_delta','assistant_message'].includes(e.type)) continue;
    const i=index.get(e.message_id);
    const old=i===undefined?undefined:result[i];
    if(e.type==='assistant_message_started' && old)continue;
    if(old?.metadata?.conversation_kind==='final' && e.metadata?.conversation_kind==='progress')continue;
    if(e.type==='answer_delta' && old?.metadata?.state==='complete') continue;
    const revision=e.revision ?? e.metadata?.revision;
    if(revision!==undefined && old?.metadata?.revision!==undefined && revision<old.metadata.revision)continue;
    const content=e.content ?? ((old?.content || '') + (e.text || ''));
    // Backwards compatibility for journals written before versioned snapshots.
    if(e.type==='answer_delta' && revision===undefined && old && old.content.length>content.length) continue;
    const message={id:e.message_id,role:e.type==='user_message'?'user':'assistant',created_at:e.ts,
      attachment_ids:e.attachment_ids || [],...old,content,
      metadata:e.metadata || {run_id:e.run_id,conversation_kind:'draft',state:'streaming',...(revision!==undefined?{revision}:{})}};
    if(i===undefined){index.set(message.id,result.length);result.push(message);} else result[i]=message;
  }
  // Fail visibly even when connected to an older or defective backend which
  // publishes a terminal event without publishing a corresponding answer.
  for(const e of events){
    if(!e.run_id || !['run_finished','run_failed','run_cancelled'].includes(e.type))continue;
    if(result.some(m=>m.metadata?.run_id===e.run_id && m.metadata?.conversation_kind==='final'))continue;
    const id=`missing-answer:${e.run_id}`;
    const message={id,role:'assistant',created_at:e.ts,attachment_ids:[],
      content:`${e.message || e.reason || 'Run ended'}. No final assistant response was delivered. Review the recorded activity, restart BlackBox if it is running older code, and retry.`,
      metadata:{run_id:e.run_id,conversation_kind:'final',state:'complete'}};
    const i=index.get(id);
    if(i===undefined){index.set(id,result.length);result.push(message);}else result[i]=message;
  }
  return result.filter(m=>!m.id.startsWith('missing-answer:') || !result.some(other=>other.id!==m.id && other.metadata?.run_id===m.metadata?.run_id && other.metadata?.conversation_kind==='final'));
}

export function mergeJournal(previous:any[], incoming:any[]) {
  return Array.from(new Map([...previous,...incoming].map(event=>[event.event_id,event])).values())
    .sort((a,b)=>a.event_id-b.event_id).slice(-2000);
}

export function executionState(status: string, events: any[]) {
  const names:Record<string,string>={completed:'Completed',failed:'Failed',cancelled:'Cancelled',denied:'Blocked',needs_access:'Blocked',checkpointed:'Checkpoint saved',interrupted:'Interrupted'};
  if(status!=='running')return names[status] || 'Ready';
  const start=events.findLastIndex(e=>e.type==='run_started');
  const latest=events.slice(Math.max(0,start)).findLast(e=>e.type==='assistant_status' || e.type==='tool_started');
  if(latest?.type==='tool_started' || ['running tools','running tool'].includes(latest?.value))return 'Running tools';
  if(latest?.value==='generating response')return 'Generating response';
  return 'Thinking';
}

export function reconcileSession(snapshot:any, transition?:{sid:string,event_id:number,status:string}) {
  const cursor=Math.max(0,...(snapshot.events || []).map((e:any)=>e.event_id));
  return transition && transition.sid===snapshot.id && cursor<transition.event_id ? {...snapshot,status:transition.status} : snapshot;
}
