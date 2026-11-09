import {test as base,expect} from '@playwright/test';
import {readFileSync,writeFileSync,existsSync} from 'node:fs';
import {resolve} from 'node:path';
import {mkdirSync} from 'node:fs';
const runtime=process.env.BLACKBOX_E2E_RUNTIME || resolve('../.runtime');
const port=Number(process.env.BLACKBOX_E2E_PORT || 8876);
const origin=`http://127.0.0.1:${port}`;
let bearer='';
export const test=base.extend({page:async({page,request},use,testInfo)=>{
  if(!bearer){
    const saved=resolve(runtime,'e2e-bearer');
    if(existsSync(saved))bearer=readFileSync(saved,'utf8');
    else {
    const code=readFileSync(resolve(runtime,'e2e-pair'),'utf8');
    const response=await request.post('/api/pair',{data:{token:code},headers:{Origin:origin}});
    expect(response.ok()).toBeTruthy();bearer=(await response.json()).token;writeFileSync(saved,bearer,{mode:0o600});
    }
  }
  const report=process.env.BLACKBOX_QA_REPORT;
  const evidence:{kind:string;[key:string]:unknown}[]=[];
  const pending:Promise<void>[]=[];
  const clean=(text:string)=>text.replace(/Bearer [\w.~+/=-]+/gi,'Bearer [REDACTED]').replace(/([?#](?:pair|token)=)[^\s]+/gi,'$1[REDACTED]');
  const redact=(value:any):any=>{
    if(Array.isArray(value))return value.map(redact);
    if(value && typeof value==='object')return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,/^(token|bearer|pairing|authorization|cookie|password|secret|images)$/i.test(key)?'[REDACTED]':redact(item)]));
    return typeof value==='string'?clean(value):value;
  };
  const url=(raw:string)=>{const value=new URL(raw);return value.origin+value.pathname;};
  if(report){
    page.on('console',message=>evidence.push({kind:'console',level:message.type(),text:clean(message.text()).slice(0,1000)}));
    page.on('pageerror',error=>evidence.push({kind:'pageerror',text:clean(error.message)}));
    page.on('requestfailed',request=>evidence.push({kind:'network_failure',url:url(request.url()),error:request.failure()?.errorText}));
    page.on('response',response=>{
      const path=new URL(response.url()).pathname;
      if(!path.startsWith('/api/') || path==='/api/pair' || path.endsWith('/events') || path.includes('/attachments/'))return;
      pending.push((async()=>{
        const content=await response.json().catch(()=>null);
        // Never retain headers, pairing responses, request bodies or image bytes.
        evidence.push({kind:'api_response',url:url(response.url()),status:response.status(),body:content});
      })());
    });
    await page.context().route('**/*',route=>{
      const target=new URL(route.request().url());
      return ['data:','blob:'].includes(target.protocol) || target.origin===origin ? route.continue() : route.abort('blockedbyclient');
    });
  }
  await page.addInitScript(token=>sessionStorage.setItem('blackbox_token',token),bearer);
  await page.goto('/');
  await expect(page.getByLabel('Message',{exact:true})).toBeVisible();
  await expect(page.locator('.session-row.active')).toHaveCount(1);
  await Promise.all([page.waitForResponse(r=>/\/api\/sessions\/[^/]+$/.test(new URL(r.url()).pathname) && r.request().method()==='GET'),page.getByRole('button',{name:'+ New session'}).click()]);
  await expect(page.locator('.session-row').first()).toHaveClass(/active/);
  await expect(page.getByRole('heading',{name:'READY // WAITING FOR INPUT'})).toBeVisible();
  try { await use(page); }
  finally {
    if(report){
      await Promise.race([Promise.allSettled(pending),new Promise(resolve=>setTimeout(resolve,2000))]);
      const name=testInfo.title.replace(/[^a-zA-Z0-9_.-]/g,'_');
      mkdirSync(resolve(report,'screenshots'),{recursive:true});
      await page.screenshot({path:resolve(report,'screenshots',name+'.png'),fullPage:true});
      // Python controller applies recursive secret redaction to this JSON after browser shutdown.
      writeFileSync(resolve(report,name+'.browser.json'),JSON.stringify(redact(evidence),null,2),{mode:0o600});
      const errors=evidence.filter(e=>e.kind==='pageerror');
      expect(errors,'Uncaught browser errors').toEqual([]);
    }
  }
}});
export {expect};
