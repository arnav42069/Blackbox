import {test,expect} from './helpers';

test('chat executes host tool with evidence and persists history',async({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.getByLabel('Message',{exact:true}).fill('Run host preflight');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toContainText('Verified host preflight complete');
  await expect(page.locator('.tool-card.success')).toContainText('host.preflight');
  await page.reload();
  await expect(page.locator('.message.assistant')).toContainText('Verified host preflight complete');
});

test('scope denial is visible and execution stops',async({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.getByRole('button',{name:'Scope',exact:true}).click();
  await page.getByLabel('Exact IPs, CIDRs or hostnames').fill('127.0.0.1');
  await page.getByLabel('I am authorized').check();
  await page.getByRole('button',{name:'Save authorization'}).click();
  await page.getByLabel('Message',{exact:true}).fill('Try outside scope');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toContainText('Action denied');
  await expect(page.locator('.tool-card.denied')).toContainText('outside authorized scope');
});

test('Stop cancels a pending model request',async({page})=>{
  await page.getByLabel('Message',{exact:true}).fill('slow request');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await page.getByRole('button',{name:'STOP',exact:true}).click();
  await expect(page.getByRole('button',{name:'RUN',exact:true})).toBeVisible();
  const toggle=page.getByRole('button',{name:'Activity',exact:true});
  if(await toggle.isVisible())await toggle.click();
  await expect(page.locator('.activity-log')).toContainText('run cancelled');
});

test('Retry starts a new bounded run',async({page})=>{
  await page.getByLabel('Message',{exact:true}).fill('Run host preflight');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toHaveCount(1);
  await page.getByRole('button',{name:'Retry last message'}).click();
  await expect(page.locator('.message.assistant')).toHaveCount(2);
});

test('Ollama unavailable has actionable diagnostics',async({page})=>{
  await page.route('**/api/preflight',route=>route.fulfill({json:{ollama:{connected:false,message:'Ollama unavailable. Run ollama serve'}}}));
  await page.reload();
  await expect(page.locator('.banner')).toContainText('Ollama unavailable');
  await expect(page.getByRole('button',{name:'RUN',exact:true})).toBeDisabled();
});

test('journal reconnect does not rerun a finished tool',async({page,context})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.getByLabel('Message',{exact:true}).fill('Run host preflight');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.tool-card.success')).toHaveCount(1);
  await context.setOffline(true);
  await page.waitForTimeout(100);
  await context.setOffline(false);
  await page.waitForTimeout(1200);
  await expect(page.locator('.tool-card.success')).toHaveCount(1);
  await expect(page.locator('.message.assistant')).toHaveCount(1);
});
