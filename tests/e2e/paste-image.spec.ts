import {test,expect} from './helpers';

test('clipboard image preview sends real bytes through tool loop',async({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.getByLabel('Message',{exact:true}).evaluate(element=>{
    const canvas=document.createElement('canvas');canvas.width=64;canvas.height=64;
    const context=canvas.getContext('2d')!;context.fillStyle='red';context.fillRect(0,0,64,64);
    return new Promise<void>(resolve=>canvas.toBlob(blob=>{
      const data=new DataTransfer();data.items.add(new File([blob!],'clipboard.png',{type:'image/png'}));
      element.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));resolve();
    },'image/png'));
  });
  await expect(page.getByAltText('Preview clipboard.png')).toBeVisible();
  await page.getByLabel('Message',{exact:true}).fill('Describe image and check host');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.pane-title').filter({hasText:'TERMINAL / CHAT'})).toContainText('Completed');
  await expect(page.locator('.message.assistant').last()).toContainText('Image bytes received');
  await expect(page.locator('.tool-card.success')).toHaveCount(1);
  await expect(page.locator('.tool-card.success')).toContainText('host.preflight');
  const attachment=page.getByAltText('Attached screenshot');
  await expect(attachment).toBeVisible();
  const storedImage=await attachment.evaluate(async element=>{
    const img=element as HTMLImageElement;
    await img.decode();
    const canvas=document.createElement('canvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;
    const context=canvas.getContext('2d')!;
    context.drawImage(img,0,0);
    return {width:canvas.width,height:canvas.height,pixel:Array.from(context.getImageData(32,32,1,1).data)};
  });
  expect(storedImage).toEqual({width:64,height:64,pixel:[255,0,0,255]});
});

test('ordinary clipboard text remains normal',async({page})=>{
  const prevented=await page.getByLabel('Message',{exact:true}).evaluate(element=>{
    const data=new DataTransfer();data.setData('text/plain','ordinary pasted text');
    const e=new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true});
    element.dispatchEvent(e);return e.defaultPrevented;
  });
  expect(prevented).toBe(false);
});

test('screenshot-only request shows structured observations and inspectable plan',async({page})=>{
  await page.setViewportSize({width:1440,height:900});
  await page.locator('input[type=file]').setInputFiles({name:'scan.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhWQAAAAASUVORK5CYII=','base64')});
  await page.getByLabel('Message',{exact:true}).fill('Read this screenshot');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toContainText('Visual observations (not independently verified');
  await page.getByText('Inspect execution plan',{exact:true}).click();
  await expect(page.locator('.activity-log')).toContainText('"intent": "image"');
  await page.getByText('Inspect image observations',{exact:true}).click();
  await expect(page.locator('.activity-log')).toContainText('"visible_text": "red square"');
  await expect(page.locator('.tool-card')).toHaveCount(0);
});

test('capability probe reports verified vision and structured tool failure',async({page})=>{
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Settings'});
  await dialog.getByRole('button',{name:'Probe capabilities'}).click();
  await expect(dialog.getByRole('status')).toContainText('Vision verified');
  await expect(dialog.getByRole('status')).toContainText('No valid structured echo_test tool call');
  await dialog.getByRole('button',{name:'Cancel'}).click();
  await expect(page.locator('.scope-strip')).toContainText('Vision verified');
  await page.locator('input[type=file]').setInputFiles({name:'scan.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhWQAAAAASUVORK5CYII=','base64')});
  await page.getByLabel('Message',{exact:true}).fill('Read this screenshot');
  await page.getByRole('button',{name:'RUN',exact:true}).click();
  await expect(page.locator('.message.assistant')).toContainText('red square');
});
