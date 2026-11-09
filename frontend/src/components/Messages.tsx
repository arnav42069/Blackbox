import { useEffect, useState } from 'preact/hooks';
import { marked } from 'marked';
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

export function Messages({messages,sid}:{messages:any[],sid:string}) {
  return <>{messages.filter(m=>m.role!=='tool' && !m.metadata?.tool_calls?.length).map(m=><article key={m.id} class={`message ${m.role}`}>
    <div class="role">{m.role==='user'?'USER':'BLACKBOX · MODEL RESPONSE'}<small>{new Date(m.created_at*1000).toLocaleTimeString()}</small></div>
    <div class="markdown" dangerouslySetInnerHTML={{__html:DOMPurify.sanitize(marked.parse(m.content,{async:false}) as string,{FORBID_TAGS:['img','style','iframe','form','input','video','audio'],FORBID_ATTR:['style']})}}/>
    <div class="attachments">{m.attachment_ids.map((id:string)=><Image key={id} sid={sid} id={id}/>)}</div>
  </article>)}</>;
}
