import {test,expect} from './helpers';

test('restored missing model stays selected and offers installed alternatives',async({page})=>{
  await page.route('**/api/sessions/*',async route=>{
    const response=await route.fetch();
    if(route.request().method()!=='GET')return route.fulfill({response});
    const value=await response.json();
    if(value.model){
      value.model='namespace/removed:4b';
      value.model_availability={status:'missing',message:'Saved model namespace/removed:4b is unavailable. Select an installed alternative explicitly.',alternatives:['fixture:4b']};
      value.messages=[{
        id:'history-only', role:'user', content:'Saved transcript entry',
        created_at:Date.now()/1000, attachment_ids:[], metadata:{},
      }];
    }
    return route.fulfill({json:value});
  });
  await page.reload();
  await expect(page.getByLabel('Model',{exact:true})).toHaveValue('namespace/removed:4b');
  await expect(page.locator('.banner').filter({hasText:'Saved model namespace/removed:4b'})).toContainText('Installed alternatives: fixture:4b');
  await expect(page.getByLabel('Model',{exact:true}).locator('option')).toHaveText(['namespace/removed:4b','fixture:4b']);
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Settings'});
  await dialog.getByLabel('Subtle header texture').check();
  await dialog.getByRole('button',{name:'Save settings'}).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByLabel('Model',{exact:true})).toHaveValue('namespace/removed:4b');

  await page.getByLabel('Model',{exact:true}).selectOption('fixture:4b');
  await expect(page.getByText('Saved model namespace/removed:4b',{exact:false})).toBeHidden();
  await expect(page.getByText('Saved transcript entry',{exact:true})).toBeVisible();
});

test('restored session recovers model availability without a reload',async({page})=>{
  let offline=true;
  await page.route('**/api/preflight',async route=>{
    const response=await route.fetch();
    if(!offline)return route.fulfill({response});
    const value=await response.json();
    value.ollama={connected:false,message:'Ollama connection refused'};
    return route.fulfill({json:value});
  });
  await page.route('**/api/sessions/*',async route=>{
    const response=await route.fetch();
    if(!offline||route.request().method()!=='GET')return route.fulfill({response});
    const value=await response.json();
    value.model_availability={status:'unknown',message:'Could not verify the saved model while Ollama is offline.',alternatives:[]};
    return route.fulfill({json:value});
  });

  await page.reload();
  const model=page.getByLabel('Model',{exact:true});
  const status=page.locator('.scope-strip');
  await expect(model).toHaveValue('fixture:4b');
  await expect(status).toContainText('Model availability unknown');
  await expect(page.getByText('Could not verify the saved model while Ollama is offline.')).toBeVisible();

  const composer=page.getByLabel('Message',{exact:true});
  await composer.fill('Draft survives reconnect');
  offline=false;
  await page.getByRole('button',{name:'Retry connection'}).click();
  await expect(status).toContainText('Vision verified');
  await expect(page.getByText('Could not verify the saved model while Ollama is offline.')).toBeHidden();
  await expect(model).toHaveValue('fixture:4b');
  await expect(composer).toHaveValue('Draft survives reconnect');
});
