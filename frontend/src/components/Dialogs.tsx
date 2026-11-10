import { useState, useEffect } from 'preact/hooks';
import { api } from '../api';

function useModalFocus(close:()=>void) {
  useEffect(()=>{
    const prior=document.activeElement as HTMLElement;
    const modal=document.querySelector('.modal') as HTMLElement;
    const controls=()=>Array.from(modal.querySelectorAll<HTMLElement>('button:not([disabled]),input,select,textarea,a[href]'));
    controls()[0]?.focus();
    function key(e:KeyboardEvent){
      if(e.key==='Escape'){e.preventDefault();close();}
      if(e.key==='Tab'){
        const list=controls(), first=list[0], last=list.at(-1);
        if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}
        else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
      }
    }
    modal.addEventListener('keydown',key);
    return ()=>{modal.removeEventListener('keydown',key);prior?.focus();};
  },[]);
}

export function ScopeDialog({sid, scope, close, saved, error}:any) {
  useModalFocus(close);
  const [localError,setLocalError]=useState('');
  const [targets,setTargets]=useState((scope?.allowed_targets || []).join(', '));
  const [ports,setPorts]=useState(scope?.allowed_ports || '1-1000,8080,8443');
  const [roots,setRoots]=useState((scope?.allowed_remote_read_roots || ['/home']).join(', '));
  const [active,setActive]=useState(scope?.allow_active_scanning || false);
  const [attested,setAttested]=useState(false);
  const [budget,setBudget]=useState(scope?.max_requests_per_run || 100);
  const [ttl,setTtl]=useState('session_end');
  const [busy,setBusy]=useState(false);
  const split=(v:string)=>v.split(',').map(x=>x.trim()).filter(Boolean);
  async function submit(e:Event) {
    e.preventDefault();setBusy(true);
    try { const result=await api(`/sessions/${sid}/scope`,{method:'PUT',body:JSON.stringify({name:'Authorized lab',authorization_attested:attested,allowed_targets:split(targets),allowed_ports:ports,allowed_remote_read_roots:split(roots),allow_active_scanning:active,expires_at:ttl==='session_end'?ttl:Date.now()/1000+Number(ttl)*60,max_requests_per_run:Number(budget)})});saved(result);close();}
    catch(e){setLocalError(String(e));error(String(e));}finally{setBusy(false);}
  }
  return <div class="modal-shade"><form class="modal" role="dialog" aria-modal="true" aria-label="Authorize session scope" onSubmit={submit}>
    <h2>Authorize session scope</h2><p>Only add systems you own or have explicit permission to test. Scope is revoked when BlackBox restarts.</p>
    <label class="field">Exact IPs, CIDRs or hostnames<input required value={targets} onInput={e=>setTargets(e.currentTarget.value)} placeholder="127.0.0.1/32"/></label>
    <label class="field">Allowed TCP ports<input required value={ports} onInput={e=>setPorts(e.currentTarget.value)}/></label>
    <label class="field">Read-only SSH lab roots<input required value={roots} onInput={e=>setRoots(e.currentTarget.value)}/></label>
    <label class="field">HTTP request budget (1–500)<input type="number" min="1" max="500" value={budget} onInput={e=>setBudget(Number(e.currentTarget.value))}/></label>
    <label class="field">Authorization expires<select value={ttl} onChange={e=>setTtl(e.currentTarget.value)}><option value="session_end">On backend restart / session deletion</option><option value="30">In 30 minutes</option><option value="120">In 2 hours</option></select></label>
    <p><label><input type="checkbox" checked={active} onChange={e=>setActive(e.currentTarget.checked)}/> Allow bounded active scans and public path discovery</label></p>
    <p><label><input type="checkbox" required checked={attested} onChange={e=>setAttested(e.currentTarget.checked)}/> I am authorized to inspect these exact targets and read these lab paths.</label></p>
    <small>Read-only tools run automatically in Auto safe mode. No credential attacks, remote writes or unrestricted shell.</small>
    <div role="alert" class="error">{localError}</div><div class="row"><button type="button" onClick={close}>Cancel</button><button class="primary" disabled={!attested || busy}>Save authorization</button></div>
  </form></div>;
}

export function SettingsDialog({prefs,models,close,saved,error,refresh}:any) {
  useModalFocus(close);
  const [localError,setLocalError]=useState('');
  const [value,setValue]=useState({...prefs});
  const [probing,setProbing]=useState(false);
  const [probe,setProbe]=useState('');
  const [profiles,setProfiles]=useState<any[]>([]);
  const change=(key:string,v:any)=>setValue({...value,[key]:v});
  async function submit(e:Event) {e.preventDefault();try {saved(await api('/config',{method:'PATCH',body:JSON.stringify(value)}));close();}catch(e){setLocalError(String(e));error(String(e));}}
  async function runProbe(){setProbing(true);setProbe('Testing installed model with a small image and echo tool…');try {const r=await api(`/models/${encodeURIComponent(value.model)}/probe`,{method:'POST'});setProbe(`Vision ${r.vision?'PASS':'FAIL'} · Tools ${r.tools?'PASS':'FAIL'}`);await refresh();}catch(e){setProbe(String(e));}finally{setProbing(false);}}
  return <div class="modal-shade"><form class="modal" role="dialog" aria-modal="true" aria-label="Settings" onSubmit={submit}><h2>Settings</h2>
    <label class="field">Default model<select value={value.model} onChange={e=>change('model',e.currentTarget.value)}>{!models.some((m:any)=>m.name===value.model)&&<option>{value.model}</option>}{models.map((m:any)=><option value={m.name}>{m.name}</option>)}</select></label>
    <button type="button" disabled={probing} onClick={runProbe}>Probe vision + tools</button>{probe&&<p role="status">{probe}</p>}
    <label class="field">Vision fallback model<select value={value.vision_model || ''} onChange={e=>change('vision_model',e.currentTarget.value || null)}><option value="">Require selected model vision support</option>{models.filter((m:any)=>m.vision).map((m:any)=><option value={m.name}>{m.name}</option>)}</select></label>
    <label class="field">Scan freshness (seconds)<input type="number" min="0" max="86400" value={value.scan_freshness_seconds} onInput={e=>change('scan_freshness_seconds',Number(e.currentTarget.value))}/></label>
    <label class="field">HTTP requests per second<input type="number" min="1" max="5" value={value.http_rate} onInput={e=>change('http_rate',Number(e.currentTarget.value))}/></label>
    <label class="field">Discovery timeout (seconds)<input type="number" min="1" max="40" value={value.discovery_timeout_seconds} onInput={e=>change('discovery_timeout_seconds',Number(e.currentTarget.value))}/></label>
    <label class="field">Execution mode<select value={value.mode} onChange={e=>change('mode',e.currentTarget.value)}><option value="auto_safe">Auto (safe)</option><option value="manual">Manual — approve each tool</option></select></label>
    <label class="field">Context tokens<input type="number" min="2048" max="16384" value={value.num_ctx} onInput={e=>change('num_ctx',Number(e.currentTarget.value))}/></label>
    <label class="field">Tool budget (1–20)<input type="number" min="1" max="20" value={value.max_tool_calls} onInput={e=>change('max_tool_calls',Number(e.currentTarget.value))}/></label>
    <label class="field">Retention days<input type="number" min="1" max="365" value={value.retention_days} onInput={e=>change('retention_days',Number(e.currentTarget.value))}/></label>
    <p><label><input type="checkbox" checked={value.visual_effects} onChange={e=>change('visual_effects',e.currentTarget.checked)}/> Subtle header texture</label></p>
    <button type="button" onClick={()=>api('/ssh-profiles').then(setProfiles).catch(e=>error(String(e)))}>Check SSH profiles</button>{profiles.map(p=><p><code>{p.name}: {p.host}:{p.port}</code></p>)}
    <small class="field">SSH profiles are configured locally. Keys and credentials never enter model messages. See README.md.</small>
    <div role="alert" class="error">{localError}</div><div class="row"><button type="button" onClick={close}>Cancel</button><button class="primary" disabled={probing}>Save settings</button></div>
  </form></div>;
}
