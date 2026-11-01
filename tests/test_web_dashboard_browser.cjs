// Optional browser smoke check. See docs/LOCAL_CHECK.md for setup.
const {chromium} = require('playwright');
const {createServer} = require('node:http');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const assert = require('node:assert/strict');
const root = resolve(__dirname, '..');
const expenses = Array.from({length: 31}, (_, i) => ({
  id:i+1,
  name:['Кофейня у дома','Продукты на неделю','Подписка на музыку','Такси до работы','Покупка книги'][i%5],
  category:['Еда','Продукты','Подписки','Транспорт','Досуг'][i%5],
  subcategory:'Разное',
  operation_date:'2026-10-'+String(30-Math.floor(i/3)).padStart(2,'0'),
  amount:String(350+i*137),
  returned_amount:i===0?'150':'0',
  personal_amount:String(350+i*137-(i===0?150:0))
}));
const data = {
  start:'2026-10-01',
  end:'2026-10-31',
  metrics:{expenses:'48570.50',
  income:'95000',
  cash_flow:'45179.50',
  paid:'49820.50',
  reimbursements:'0',
  adjustment:'1250',
  excluded:'0'},
  expenses:expenses.slice(0,25),
  count:31,
  page:0,
  series:Array.from({length:15},(_,i)=>({date:'2026-10-'+String(i*2+1).padStart(2,'0'),
  category:['Еда','Транспорт','Подписки'][i%3],
  amount:String(300+i*311)})),
  colors:{'Еда':'#dca36c','Транспорт':'#9e8bd1','Подписки':'#66a992','Продукты':'#80a790','Досуг':'#9e8bd1'},
  categories:['Еда','Транспорт','Подписки','Продукты','Досуг'].map(name=>({kind:'expense',name,subcategories:[{name:'Разное'},{name:'Другая'}]})),
  members:[{user_id:'telegram:123'}],
  planning:{start:'2026-10-15',
  end:'2026-11-14',
  available:'28170',
  daily:'2012.14',
  days_left:14,
  reserve:'15000',
  opening:'25000',
  income:'95000',
  paid:'76830',
  reimbursements:'0',
  all_expenses:'76830',
  upcoming:[{due_date:'2026-11-05',
  name:'Оплата квартиры',
  amount:'12000',
  operation_type:'expense'},{due_date:'2026-11-10',
  name:'Спортзал',
  amount:'3000',
  operation_type:'expense'}]},
  limits:{'':'60000','Еда':'15000'}
};
(async()=>{
 const server=createServer((request,response)=>{
   const assets={'/dashboard':'index.html','/dashboard/app.js':'app.js','/dashboard/style.css':'style.css'};
   const asset=assets[new URL(request.url,'http://localhost').pathname];
   if(!asset){response.writeHead(404);response.end();return;}
   response.setHeader('Content-Type',asset.endsWith('.js')?'application/javascript':asset.endsWith('.css')?'text/css':'text/html');
   response.end(readFileSync(resolve(root,'src/budget_bot/web',asset)));
 });
 await new Promise(done=>server.listen(0,'127.0.0.1',done));
 const browser=await chromium.launch({executablePath:process.env.BROWSER_EXECUTABLE||undefined,headless:true});
 try {
 const page=await browser.newPage({viewport:{width:390,height:844},deviceScaleFactor:1});
 const errors=[]; const requests=[]; let failNext=false; let unauth=false; let empty=false;
 let saveError=0; let editable=true; let delaySave=false; const saveBodies=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('https://telegram.org/**',r=>r.fulfill({contentType:'application/javascript',body:''}));
 await page.route('**/api/**',async route=>{
   const url=new URL(route.request().url()); requests.push(url);
   if(url.pathname==='/api/budgets') return route.fulfill({json:[{id:'telegram:123',name:'Личный бюджет'},{id:'shared:test',name:'Семейный бюджет'}]});
   if(url.pathname==='/api/dashboard') {
     if(unauth) return route.fulfill({status:401,json:{detail:'Откройте приложение заново.'}});
     if(failNext){failNext=false;return route.fulfill({status:503,json:{detail:'Временная ошибка. Повторите запрос.'}});}
     const copy=structuredClone(data); copy.page=Number(url.searchParams.get('page'));
     copy.expenses=expenses.slice(copy.page*25,copy.page*25+25);
     copy.start=url.searchParams.get('start')||data.start;copy.end=url.searchParams.get('end')||data.end;
     if(empty){copy.expenses=[];copy.count=0;copy.series=[];}
     return route.fulfill({json:copy});
   }
   if(url.pathname.startsWith('/api/purchases/')) {
     const index=expenses.findIndex(row=>row.id===Number(url.pathname.split('/').at(-1)));
     if(route.request().method()==='PUT') {
       assert.equal(route.request().headers()['x-budget-edit'],'1');
       const body=route.request().postDataJSON();saveBodies.push(body);
       if(saveError){const status=saveError;saveError=0;return route.fulfill({status,json:{detail:status===409?'Запись уже изменилась.':'Сумма покупки меньше уже возвращённой.'}});}
       if(delaySave) await new Promise(done=>setTimeout(done,500));
       expenses[index]={...expenses[index],...body,personal_amount:String(Number(body.amount)-Number(expenses[index].returned_amount))};
     }
     return route.fulfill({json:{purchase:expenses[index],can_edit:editable,version:'a'.repeat(64),categories:data.categories,reimbursements:[{id:80,name:'Возврат за кофе',operation_date:'2026-10-31',amount:'150'}]}});
   }
   return route.fulfill({json:{ok:true}});
 });
 await page.goto('http://127.0.0.1:'+server.address().port+'/dashboard');
 await page.locator('.hero-value').first().filter({hasText:'48'}).waitFor();
 assert.equal(await page.locator('#recent .transaction').count(),4);
 if(process.env.DASHBOARD_SCREENSHOTS) await page.screenshot({path:root+'/docs/screenshots/dashboard-mobile.jpg',type:'jpeg',quality:90,fullPage:false});
 for(const width of [320,360,390,768,1440]){
   await page.setViewportSize({width,height:width>760?1024:844});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), 'Page overflow at '+width);
   await page.waitForTimeout(200);
   if(width===1440 && process.env.DASHBOARD_SCREENSHOTS) await page.screenshot({path:root+'/docs/screenshots/dashboard-desktop.jpg',type:'jpeg',quality:90,fullPage:true});
 }
 await page.setViewportSize({width:390,height:844});
 await page.locator('#recent .transaction').filter({hasText:'Кофейня'}).first().click();
 await page.locator('#purchase-content h2').waitFor();
 assert((await page.locator('#purchase .hero-value').textContent()).replace(/\s/g,'')==='200,00₽');
 await page.locator('#edit-purchase').click();
 assert.equal(await page.locator('#edit-amount').inputValue(),'350');
 await page.locator('#edit-name').fill('Отменённая правка');
 await page.locator('#edit-cancel').click();
 assert.equal(saveBodies.length,0);
 assert.equal(await page.locator('#purchase-content h2').textContent(),'Кофейня у дома');
 await page.locator('#edit-purchase').click();
 await page.locator('#edit-name').fill('Новая кофейня');
 await page.locator('#edit-amount').fill('500,25');
 await page.locator('#edit-date').fill('2026-10-29');
 await page.locator('#edit-category').selectOption('Транспорт');
 await page.locator('#edit-subcategory').selectOption('Другая');
 if(process.env.DASHBOARD_SCREENSHOTS) await page.screenshot({path:root+'/docs/screenshots/dashboard-edit.jpg',type:'jpeg',quality:90,fullPage:false});
 saveError=422;
 await page.locator('#edit-save').click();
 await page.locator('#edit-error').waitFor();
 assert.equal(await page.locator('#edit-name').inputValue(),'Новая кофейня');
 assert(await page.locator('#purchase-edit').isVisible());
 await page.locator('#edit-amount').fill('550,25');
 delaySave=true;
 await page.locator('#edit-save').click();
 assert(await page.locator('#edit-save').isDisabled());
 assert(await page.locator('[data-close="purchase"]').isDisabled());
 await page.evaluate(()=>document.querySelector('#edit-save').click());
 await page.keyboard.press('Escape');
 assert(await page.locator('#purchase').isVisible());
 await page.locator('#purchase-feedback').waitFor();
 assert.equal(saveBodies.length,2);
 assert.equal(saveBodies[1].amount,'550.25');
 assert.equal(saveBodies[1].category,'Транспорт');
 assert.equal(saveBodies[1].subcategory,'Другая');
 assert.equal(saveBodies[1].version,'a'.repeat(64));
 assert.equal(await page.locator('#purchase-content h2').textContent(),'Новая кофейня');
 assert((await page.locator('#purchase-content .hero-value').textContent()).replace(/\s/g,'')==='400,25₽');
 await page.waitForFunction(()=>!document.querySelector('#edit-save').disabled);
 await page.locator('#edit-purchase').click();
 saveError=409;
 await page.locator('#edit-name').fill('Устаревшая форма');
 await page.locator('#edit-save').click();
 await page.locator('#edit-reload').waitFor();
 await page.locator('#edit-reload').click();
 await page.locator('#edit-purchase').waitFor();
 assert.equal(await page.locator('#purchase-content h2').textContent(),'Новая кофейня');
 await page.locator('[data-close="purchase"]').click();
 editable=false;
 await page.locator('#recent .transaction').first().click();
 await page.locator('#purchase-content h2').waitFor();
 assert.equal(await page.locator('#edit-purchase').count(),0);
 await page.locator('[data-close="purchase"]').click();
 editable=true;
 await page.locator('[data-tab="expenses"]').click();
 assert.equal(await page.locator('#entries .transaction').count(),25);
 await page.locator('#next').click();
 await page.locator('#page').filter({hasText:'2 из 2'}).waitFor();
 assert.equal(await page.locator('#entries .transaction').count(),6);
 await page.locator('[data-tab="overview"]').click();
 await page.waitForFunction(()=>document.querySelector('#page').textContent.includes('1 из 2'));
 assert.equal(await page.locator('#recent .transaction').count(),4);
 await page.locator('#filter-button').click();
 await page.locator('#search').fill('кофе');
 await page.locator('#category').selectOption('Еда');
 await page.locator('[data-close="filter-dialog"]').click();
 assert(await page.locator('#active-filters').isHidden());
 await page.locator('#filter-button').click();
 assert.equal(await page.locator('#search').inputValue(),'');
 await page.locator('#search').fill('кофе');
 await page.locator('#category').selectOption('Еда');
 await page.locator('#mandatory').check();
 await page.locator('#filters [type="submit"]').click();
 await page.locator('#filter-count').filter({hasText:'3'}).waitFor();
 assert(requests.at(-1).searchParams.get('category')==='Еда');
 await page.locator('#clear-filters').click();
 await page.waitForFunction(()=>document.querySelector('#active-filters').hidden);
 await page.locator('#period-button').click();
 await page.locator('[data-period="budget"]').click();
 await page.locator('#filters [type="submit"]').click();
 await page.waitForFunction(()=>!document.querySelector('#filter-dialog').open);
 await page.locator('#refresh').waitFor({state:'visible'});
 await page.waitForFunction(()=>!document.querySelector('#refresh').disabled);
 assert.equal(requests.at(-1).searchParams.has('start'),false);
 await page.locator('[data-tab="trends"]').click();
 await page.locator('#chart svg').waitFor();
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Chart overflow');
 await page.locator('#chart circle').first().click();
 assert((await page.locator('#chart-detail').textContent()).includes('₽'));
 if(process.env.DASHBOARD_SCREENSHOTS) await page.screenshot({path:root+'/docs/screenshots/dashboard-trends.jpg',type:'jpeg',quality:90,fullPage:false});
 await page.locator('[data-tab="planning"]').click();
 assert(await page.locator('#period-toolbar').isHidden());
 assert.equal(await page.locator('#payments .payment').count(),2);
 await page.locator('#budget').selectOption('shared:test');
 await page.waitForFunction(()=>!document.querySelector('#refresh').disabled);
 assert.equal(requests.at(-1).searchParams.get('budget'),'shared:test');
 failNext=true; await page.locator('#refresh').click();
 await page.locator('#retry').waitFor();
 assert((await page.locator('#status').textContent()).includes('Временная'));
 assert(await page.locator('#status-wrap').evaluate(node=>node.getBoundingClientRect().bottom<=innerHeight));
 await page.locator('#retry').click();
 await page.waitForFunction(()=>document.querySelector('#status-wrap').hidden);
 empty=true; await page.locator('#refresh').click();
 await page.waitForFunction(()=>document.querySelector('#entries .empty'));
 await page.locator('[data-tab="trends"]').click();
 assert.equal(await page.locator('#chart .empty').count(),1);
 unauth=true; await page.locator('#refresh').click();
 await page.locator('#login').waitFor();
 assert(await page.locator('#workspace').isHidden());
 assert(await page.locator('#account').isHidden());

 // Telegram authentication stays in memory; the native Back button closes sheets.
 const mini = await browser.newPage({viewport: {width: 390, height: 844}});
 mini.on('pageerror', error => errors.push(error.message));
 await mini.route('https://telegram.org/**', route => route.fulfill({
   contentType: 'application/javascript',
   body: `window.nativeCalls = [];
     window.Telegram = {WebApp: {
       initData: 'synthetic-init-data', isVersionAtLeast: () => true,
       ready: () => nativeCalls.push('ready'), expand: () => nativeCalls.push('expand'),
       setHeaderColor: () => {}, setBackgroundColor: () => {}, setBottomBarColor: () => {},
       BackButton: {onClick: handler => window.nativeBack = handler,
         show: () => nativeCalls.push('show'), hide: () => nativeCalls.push('hide')}
     }};`
 }));
 await mini.route('**/api/**', route => {
   const request = route.request(), url = new URL(request.url());
   if (url.pathname === '/api/login') {
     assert.equal(request.postDataJSON().init_data, 'synthetic-init-data');
     return route.fulfill({json: {session_token: 'synthetic-memory-token'}});
   }
   assert.equal(request.headers().authorization, 'Bearer synthetic-memory-token');
   return route.fulfill({json: url.pathname === '/api/budgets' ? [{id: 'telegram:123', name: 'Личный бюджет'}] : data});
 });
 await mini.goto('http://127.0.0.1:' + server.address().port + '/dashboard');
 await mini.locator('.hero-value').first().filter({hasText: '48'}).waitFor();
 assert(await mini.evaluate(() => nativeCalls.includes('ready') && nativeCalls.includes('expand')));
 assert.equal(await mini.evaluate(() => localStorage.length + sessionStorage.length), 0);
 await mini.locator('[data-tab="planning"]').click();
 assert.equal(await mini.evaluate(() => nativeCalls.at(-1)), 'show');
 await mini.evaluate(() => nativeBack());
 assert(await mini.locator('[data-view="overview"]').isVisible());
 await mini.locator('#filter-button').click();
 await mini.evaluate(() => nativeBack());
 await mini.waitForFunction(() => !document.querySelector('#filter-dialog').open);
 await mini.close();
 assert.deepEqual(errors,[]);
 console.log('PASS: mobile/desktop overflow, cards, pagination, filters/cancel/reset, period presets, chart, planning, budget switch, retry, empty state, expired session, edit/cancel/validation/conflict/permissions and saving lock; no JS errors.');
 } finally {
   await browser.close();
   await new Promise(done=>server.close(done));
 }
})().catch(error=>{console.error(error);process.exit(1);});
