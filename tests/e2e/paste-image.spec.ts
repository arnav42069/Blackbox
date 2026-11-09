import {test,expect} from './helpers';

test('clipboard image preview sends real bytes through tool loop',async({page})=>{
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
  await expect(page.locator('.message.assistant')).toContainText('Image bytes received');
  await expect(page.getByAltText('Attached screenshot')).toBeVisible();
});

test('ordinary clipboard text remains normal',async({page})=>{
  const prevented=await page.getByLabel('Message',{exact:true}).evaluate(element=>{
    const data=new DataTransfer();data.setData('text/plain','ordinary pasted text');
    const e=new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true});
    element.dispatchEvent(e);return e.defaultPrevented;
  });
  expect(prevented).toBe(false);
});
