import {test as base,expect} from '@playwright/test';
import {readFileSync,writeFileSync,existsSync} from 'node:fs';
import {resolve} from 'node:path';
let bearer='';
export const test=base.extend({page:async({page,request},use)=>{
  if(!bearer){
    const saved=resolve('../.runtime/e2e-bearer');
    if(existsSync(saved))bearer=readFileSync(saved,'utf8');
    else {
    const code=readFileSync(resolve('../.runtime/e2e-pair'),'utf8');
    const response=await request.post('/api/pair',{data:{token:code},headers:{Origin:'http://127.0.0.1:8876'}});
    expect(response.ok()).toBeTruthy();bearer=(await response.json()).token;writeFileSync(saved,bearer,{mode:0o600});
    }
  }
  await page.addInitScript(token=>sessionStorage.setItem('blackbox_token',token),bearer);
  await page.goto('/');
  await expect(page.getByLabel('Message',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'+ New session'}).click();
  await expect(page.getByRole('heading',{name:'READY // WAITING FOR INPUT'})).toBeVisible();
  await use(page);
}});
export {expect};
