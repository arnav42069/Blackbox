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
  await expect(page.getByRole('dialog',{name:'Authorize session scope'})).toBeHidden();
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
  await page.route('**/api/preflight',route=>route.fulfill({json:{conversation_protocol:1,ollama:{connected:false,message:'Ollama unavailable. Run ollama serve'}}}));
  await page.reload();
  await expect(page.locator('.banner')).toContainText('Ollama unavailable');
  await expect(page.getByRole('button',{name:'RUN',exact:true})).toBeDisabled();
});

test('old backend conversation protocol has an actionable restart warning',async({page})=>{
  await page.route('**/api/preflight',async route=>{
    const response=await route.fetch();
    const body=await response.json();
    delete body.conversation_protocol;
    await route.fulfill({json:body});
  });
  await page.reload();
  await expect(page.locator('.banner')).toContainText('blackbox restart');
});

test('room goal checkpoints and continues with linked tool evidence',async({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.getByRole('button',{name:'Scope',exact:true}).click();
  await page.getByLabel('Exact IPs, CIDRs or hostnames').fill('127.0.0.1');
  await page.getByLabel('I am authorized').check();
  await page.getByLabel('HTTP request budget (1–500)').fill('1');
  await page.getByRole('button',{name:'Save authorization'}).click();
  await expect(page.getByRole('dialog',{name:'Authorize session scope'})).toBeHidden();
  await page.getByLabel('Message',{exact:true}).fill('Solve the authorized lab at http://127.0.0.1:8080/ and retrieve flag.txt');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toContainText('Goal checkpoint saved');
  await expect(page.getByRole('button',{name:'Continue goal'})).toBeVisible();
  await page.getByRole('button',{name:'Continue goal'}).click();
  await expect(page.locator('.message.assistant').last()).toContainText('THM{browser_goal_evidence}');
  await expect(page.locator('.message.assistant').last().locator('.conversation-tools')).toContainText('web.inspect');
  await expect(page.locator('.goal-status')).toContainText('completed');
  await page.reload();
  await expect(page.locator('.message.assistant').last()).toContainText('THM{browser_goal_evidence}');
});

test('invalid workspace tool arguments recover without repeating execution',async({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.getByLabel('Message',{exact:true}).fill('Run workspace recovery and read the actual notes file');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant').last()).toContainText('controlled workspace evidence');
  await expect(page.locator('.message.assistant').last()).toContainText('No remote file access');
  await expect(page.locator('.pane-title').filter({hasText:'TERMINAL / CHAT'})).toContainText('Completed');
  await expect(page.locator('.tool-recovery')).toContainText(['validation','IDENTICAL_FAILURE_SUPPRESSED']);
  await expect(page.locator('.tool-card.success').filter({hasText:'workspace.read'})).toHaveCount(1);
  await page.reload();
  await expect(page.locator('.message.assistant').last()).toContainText('controlled workspace evidence');
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

test('authorized lab inspection streams an evidence-grounded Markdown answer',async({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.getByRole('button',{name:'Scope',exact:true}).click();
  await page.getByLabel('Exact IPs, CIDRs or hostnames').fill('127.0.0.1');
  await page.getByLabel('I am authorized').check();
  await page.getByRole('button',{name:'Save authorization'}).click();
  await expect(page.getByRole('dialog',{name:'Authorize session scope'})).toBeHidden();
  await page.getByLabel('Message',{exact:true}).fill('Perform an authorized lab inspection and explain the evidence');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toContainText('Inspected the authorized lab');
  await expect(page.locator('.message.assistant .stream-cursor')).toBeVisible();
  await expect(page.getByRole('button',{name:'STOP',exact:true})).toBeVisible();
  await expect(page.locator('.tool-card.success')).toContainText('web.inspect');
  await expect(page.locator('.message.assistant')).toContainText('Next step: review the application within scope');
  await expect(page.locator('.message.assistant table').first()).toBeVisible();
  const pythonBlock=page.locator('.message.assistant .code-block').filter({has:page.locator('small',{hasText:/^python$/})});
  await expect(pythonBlock.locator('.syntax-string')).toHaveCount(1);
  await expect(pythonBlock.getByRole('button',{name:'Copy code',exact:true})).toBeVisible();
  await expect(page.locator('.pane-title').filter({hasText:'TERMINAL / CHAT'})).toContainText('Completed');
  const evidence=await page.locator('.tool-card.success small').first().textContent();
  await expect(page.locator('.message.assistant')).toContainText(evidence!.replace('Evidence ',''));
  await page.reload();
  await expect(page.locator('.message.assistant')).toContainText('No vulnerability is verified');
  await expect(page.locator('.conversation-progress')).not.toHaveCount(0);
});

test('assistant stays visible when session snapshots fail during streaming',async({page})=>{
  await page.route('**/api/sessions/*',async route=>{
    const request=route.request();
    if(request.method()==='GET' && /^\/api\/sessions\/[^/]+$/.test(new URL(request.url()).pathname)){
      await route.fulfill({status:503,json:{error:{message:'Fixture snapshot temporarily unavailable'}}});
    }else await route.continue();
  });
  await page.getByLabel('Message',{exact:true}).fill('Hello without tools');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toContainText('Fixture model answer');
  await expect(page.locator('.message.user')).toContainText('Hello without tools');
  await expect(page.getByRole('heading',{name:'READY // WAITING FOR INPUT'})).toHaveCount(0);
});

test('streaming respects a reader who scrolls upward and follows again at the bottom',async({page})=>{
  await page.setViewportSize({width:1440,height:600});
  await page.getByLabel('Message',{exact:true}).fill('Context '.repeat(1000));
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.pane-title').filter({hasText:'TERMINAL / CHAT'})).toContainText('Completed');
  const transcript=page.locator('.transcript');
  await transcript.evaluate(el=>{el.scrollTop=0;el.dispatchEvent(new Event('scroll'));});
  await page.getByLabel('Message',{exact:true}).fill('Hello again');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toHaveCount(2);
  await expect(page.locator('.pane-title').filter({hasText:'TERMINAL / CHAT'})).toContainText('Completed');
  expect(await transcript.evaluate(el=>el.scrollTop)).toBeLessThan(80);
  await transcript.evaluate(el=>{el.scrollTop=el.scrollHeight;el.dispatchEvent(new Event('scroll'));});
  await page.getByLabel('Message',{exact:true}).fill('One more greeting');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toHaveCount(3);
  await expect(page.locator('.pane-title').filter({hasText:'TERMINAL / CHAT'})).toContainText('Completed');
  await expect.poll(()=>transcript.evaluate(el=>el.scrollHeight-el.scrollTop-el.clientHeight)).toBeLessThan(80);
});

test('Stop during assistant tokens preserves the partial response after reload',async({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.getByRole('button',{name:'Scope',exact:true}).click();
  await page.getByLabel('Exact IPs, CIDRs or hostnames').fill('127.0.0.1');
  await page.getByLabel('I am authorized').check();
  await page.getByRole('button',{name:'Save authorization'}).click();
  await page.getByLabel('Message',{exact:true}).fill('Perform an authorized lab inspection and explain the evidence');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toContainText('Inspected the authorized');
  await expect(page.locator('.message.assistant .stream-cursor')).toBeVisible();
  await page.getByRole('button',{name:'STOP',exact:true}).click();
  await expect(page.locator('.message.assistant')).toContainText('Stopped by user');
  await expect(page.locator('.message.assistant')).toContainText('Inspected the authorized');
  await expect(page.locator('.pane-title').filter({hasText:'TERMINAL / CHAT'})).toContainText('Cancelled');
  await page.reload();
  await expect(page.locator('.message.assistant')).toContainText('Inspected the authorized');
  await expect(page.locator('.message.assistant')).toContainText('Stopped by user');
});
