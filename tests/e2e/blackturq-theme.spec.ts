import {test,expect} from './helpers';
import {mkdirSync} from 'node:fs';
for(const [width,height] of [[1440,900],[1024,768],[390,844]]){
  test(`BlackTurq layout ${width}`,async({page})=>{
    await page.setViewportSize({width,height});
    const colors=await page.evaluate(()=>{
      const s=getComputedStyle(document.documentElement);
      return ['--bt-bg','--bt-fg','--bt-accent','--bt-cursor','--bt-select-bg','--bt-select-fg'].map(k=>s.getPropertyValue(k).trim());
    });
    const contrasts=await page.evaluate(()=>{
      const css=getComputedStyle(document.documentElement);
      const luminance=(key:string)=>{
        const hex=css.getPropertyValue(key).trim();
        const rgb=[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);
        return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
      };
      return [['--bt-fg','--surface-0'],['--text-secondary','--surface-1'],['--text-disabled','--surface-1'],['--bt-red','--surface-1'],['--bt-bg','--bt-accent']].map(([a,b])=>{
        const values=[luminance(a),luminance(b)].sort((x,y)=>x-y);return (values[1]+.05)/(values[0]+.05);
      });
    });
    for(const ratio of contrasts)expect(ratio).toBeGreaterThanOrEqual(4.5);
    expect(colors).toEqual(['#0a0a0a','#c8dcdc','#adf0e9','#75dddd','#c8dcdc','#0a0a0a']);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
    await expect(page.getByLabel('Message',{exact:true})).toBeInViewport();
    if(width<1100)await expect(page.getByLabel('Live activity',{exact:true})).toBeHidden();
    if(width<720)await expect(page.getByLabel('Sessions',{exact:true})).toBeHidden();
    await page.emulateMedia({reducedMotion:'reduce'});
    expect(await page.evaluate(()=>{
      const caret=document.createElement('span');caret.className='caret active';document.body.append(caret);
      const animation=getComputedStyle(caret).animationName;caret.remove();return animation;
    })).toBe('none');
    await page.getByLabel('Message',{exact:true}).focus();
    expect(await page.getByLabel('Message',{exact:true}).evaluate(e=>getComputedStyle(e.closest('.composer')!).borderColor)).toBe('rgb(117, 221, 221)');
    expect(await page.locator('.workbench').getAttribute('class')).not.toContain('effects');
    mkdirSync('../docs/screenshots',{recursive:true});
    await page.screenshot({path:`../docs/screenshots/blackbox-${width}.png`});
  });
}

test('scope dialog supports keyboard focus and Escape on phone',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.getByRole('button',{name:'Scope',exact:true}).click();
  await expect(page.getByRole('dialog')).toBeInViewport();
  await expect(page.getByLabel('Exact IPs, CIDRs or hostnames')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toBeHidden();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
});
