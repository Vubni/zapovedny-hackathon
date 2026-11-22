// Optional acceptance test for the local app. The production app needs only Python.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const base = 'http://127.0.0.1:' + (process.env.METEO_TEST_PORT || '8765');
const evidenceDir = path.join(root, 'results', 'browser');
const exampleDir = path.join(root, 'results', 'examples');

async function downloaded(locator, page) {
  const [file] = await Promise.all([page.waitForEvent('download'), locator.click()]);
  assert.equal(await file.failure(), null);
  return fs.readFile(await file.path(), 'utf8');
}

async function tableSettled(page, expectedText) {
  await page.waitForFunction(text => {
    const table = document.getElementById('ledger-table');
    return table && table.textContent.includes(text) && !document.querySelector('#journal-view[aria-busy=true]');
  }, expectedText);
}

async function apply(page, fields) {
  for (const [id,value] of Object.entries(fields)) {
    const field = page.locator('#'+id);
    if (['station','param','grain'].includes(id)) await field.selectOption(value);
    else await field.fill(value);
  }
  await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/series' && r.ok()),
    page.locator('#apply').click()
  ]);
  await page.waitForFunction(()=>!document.querySelector('#journal-view[aria-busy=true]'));
}

async function checkWidth(page) {
  const size = await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,width:innerWidth}));
  assert.ok(size.scroll<=size.width+1, JSON.stringify(size));
  return size;
}

async function navigationContrast(page) {
  const checks = await page.locator('.masthead nav .nav-button').evaluateAll(elements=>elements.map(el=>{
    const style=getComputedStyle(el);
    let surface=el;
    while(surface.parentElement && getComputedStyle(surface).backgroundColor==='rgba(0, 0, 0, 0)') surface=surface.parentElement;
    const background=getComputedStyle(surface).backgroundColor;
    function luminance(rgb) {
      const [r,g,b]=rgb.match(/[0-9.]+/g).slice(0,3).map(Number).map(v=>v/255).map(v=>v<=0.04045?v/12.92:((v+.055)/1.055)**2.4);
      return .2126*r+.7152*g+.0722*b;
    }
    const foreground=luminance(style.color),back=luminance(background);
    return {text:el.textContent.trim(),color:style.color,background,ratio:(Math.max(foreground,back)+.05)/(Math.min(foreground,back)+.05)};
  }));
  assert.equal(checks.length,2);
  for (const check of checks) assert.ok(check.ratio>=4.5,JSON.stringify(check));
  return checks;
}

async function runViewport(browser, viewport) {
  const page = await browser.newPage({viewport, locale:'ru-RU',acceptDownloads:true});
  page.setDefaultTimeout(6500);
  const result = {viewport,steps:[],api:[],jsErrors:[]};
  const expectedYear = JSON.parse(await fs.readFile(path.join(exampleDir,'kr1-pressure-2024.json'),'utf8'));
  const expectedRaw = JSON.parse(await fs.readFile(path.join(exampleDir,'kr1-pressure-april-29-incomplete.json'),'utf8'));
  page.on('pageerror', e=>result.jsErrors.push(e.message));
  page.on('response', r=>{
    const url = new URL(r.url());
    if (url.pathname.startsWith('/api/')) result.api.push({path:url.pathname,query:url.search,status:r.status()});
  });

  async function step(label, action) {
    const row = {label,status:'running'};
    result.steps.push(row);
    try {
      row.observed = await action();
      row.status='passed';
    } catch (error) {
      row.status='failed';
      row.message=error.message.split('\n').slice(0,4).join(' ');
      throw error;
    }
  }

  try {
    await step('Загрузка архива и выбор Кр1', async()=>{
      const navigation = await page.goto(base, {waitUntil:'load'});
      assert.equal(navigation.status(),200);
      await page.waitForFunction(()=>document.querySelector('#station')?.value==='kr1' && !document.querySelector('#apply').disabled);
      assert.equal(await page.locator('#param').inputValue(),'pressure_hpa');
      return {station:'kr1',param:'pressure_hpa',navigation:await navigationContrast(page)};
    });
    await step('Год по месяцам, причины пропуска и график',async()=>{
      await tableSettled(page,'Апрель');
      const rows=page.locator('#ledger-table tbody tr');
      assert.equal(await rows.count(),expectedYear.rows.length);
      const april=rows.filter({hasText:/Апрель/});
      assert.equal((await april.locator('td').first().innerText()).trim(),'—');
      const text=await april.innerText();
      assert.match(text,/2\s+из\s+30/);
      assert.match(text,/28/);
      assert.match(await page.locator('#metrics').innerText(),/334\s+из\s+366/);
      await page.locator('#series-chart svg, #series-chart canvas').first().waitFor({state:'visible'});
      await checkWidth(page);
      await page.screenshot({path:path.join(evidenceDir,'year-'+viewport.width+'.png')});
      return {rows:await rows.count(),april:text};
    });
    await step('Разрыв на графике и сверка постоянного давления с клавиатуры',async()=>{
      const chart=page.locator('#series-chart svg');
      const line=await chart.locator('path.chart-line').getAttribute('d');
      assert.equal((line.match(/M/g)||[]).length,2);
      await chart.press('Home');
      for (let i=0;i<3;i++) await chart.press('ArrowRight');
      assert.match(await chart.getAttribute('aria-label'),/апр/i);
      await chart.press('Enter');
      const dialog=page.getByRole('dialog');
      await dialog.locator('[data-day="2024-04-01"]').click();
      await dialog.getByText(/все пригодные значения давления за сутки одинаковы/i).waitFor();
      await dialog.press('Escape');
      return {lineSegments:2,constantDay:'2024-04-01',control:'keyboard'};
    });
    await step('Выгрузка CSV для выбранного периода',async()=>{
      const csv=await downloaded(page.locator('#export'),page);
      const sample=await fs.readFile(path.join(exampleDir,'kr1-pressure-2024.csv'),'utf8');
      assert.equal(csv.replace(/\r\n/g,'\n'),sample.replace(/\r\n/g,'\n'));
      return {periods:12,matchesReference:true};
    });
    await step('Переход с апреля к исходной строке 5644',async()=>{
      const april=page.locator('#ledger-table tbody tr').filter({hasText:/Апрель/});
      await april.getByRole('button').first().click();
      const dialog=page.getByRole('dialog');
      await dialog.getByRole('button',{name:/29\s+апр|2024-04-29|29\.04/}).first().click();
      await dialog.getByText(/09:03/).first().waitFor({state:'visible'});
      const content=(await dialog.innerText()).replace(/(?<=\d)[\u202f\u00a0](?=\d)/g,'');
      for (const value of [/5644/,/1002[,.]27/,/15/,/18/,/31/]) assert.match(content,value);
      await checkWidth(page);
      await page.screenshot({path:path.join(evidenceDir,'raw-'+viewport.width+'.png')});
      const json=await downloaded(dialog.getByRole('link',{name:/JSON/i}),page);
      const raw=JSON.parse(json);
      assert.deepEqual(raw.rows,expectedRaw.rows);
      assert.deepEqual([raw.n,raw.slots,raw.threshold,raw.valid,raw.mean],[31,15,18,false,null]);
      await dialog.getByRole('button',{name:/закрыть/i}).click();
      return {source:raw.source,firstLine:raw.rows[0].line,n:raw.n,hours:raw.slots,required:raw.threshold};
    });
    await step('Смена показателя и точки наблюдений',async()=>{
      await apply(page,{param:'t_air',start:'2024-04-01',end:'2024-04-30'});
      const ledger=page.locator('#ledger-table tbody tr');
      assert.match(await ledger.innerText(),/28\s+из\s+30/);
      await apply(page,{station:'kr2',param:'pressure_hpa',start:'2024-04-01',end:'2024-04-30'});
      assert.match(await ledger.innerText(),/30\s+из\s+30/);
      assert.match(await ledger.innerText(),/980,16/);
      return {kr1TemperatureDays:28,kr2PressureDays:30,kr2MeanHpa:980.16};
    });
    await step('Аварийный ряд АМЕ-60 и восстановление фильтра',async()=>{
      await apply(page,{station:'ame60',param:'wind_dir',start:'2021-01-01',end:'2021-12-31'});
      const rows=page.locator('#ledger-table tbody tr');
      assert.equal(await rows.count(),12);
      const means=await rows.evaluateAll(rr=>rr.map(r=>r.querySelector('td').innerText.trim()));
      assert.ok(means.every(v=>v==='—'));
      await apply(page,{param:'t_air',start:'2016-02-01',end:'2016-02-29',grain:'dekad'});
      assert.equal(await rows.count(),3);
      return {directionMonthsWithoutValue:means.length,leapFebruaryDekads:3};
    });
    await step('Многолетний наклон, отклонения и объяснение метода',async()=>{
      await apply(page,{param:'t_air',start:'1993-02-07',end:'2020-09-20',grain:'month'});
      assert.match(await page.locator('#metrics').innerText(),/0,063/);
      assert.match(await page.locator('#metrics').innerText(),/238/);
      await page.getByRole('button',{name:'Отклонения',exact:true}).click();
      assert.ok(await page.locator('#series-chart path.chart-positive').count());
      assert.ok(await page.locator('#series-chart path.chart-negative').count());
      assert.equal(await page.locator('a[href="/method"]').first().isVisible(),true);
      return {slopeDisplayed:0.063,unit:'°C/год',months:238};
    });
    await step('Тридцать лет в браузере, пропуски и постраничная таблица',async()=>{
      const at=Date.now();
      await apply(page,{start:'1992-01-01',end:'2021-12-31',grain:'day'});
      const elapsedMs=Date.now()-at;
      const ledger=page.locator('#ledger-table tbody tr');
      assert.equal(await ledger.count(),60);
      const counters=(await page.locator('#metrics').innerText()).replace(/(?<=\d)[\s\u202f\u00a0]+(?=\d)/g,'');
      assert.match(counters,/8337 из 10958/);
      await page.getByRole('button',{name:'Далее',exact:true}).click();
      assert.match(await ledger.first().innerText(),/1 марта 1992/);
      return {calendarDays:10958,validDays:8337,tablePageSize:60,elapsedMs};
    });
    await step('Сбой запроса и повторное получение правильной выборки',async()=>{
      const matcher='**/api/series?**';
      const injected=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Проверочный сбой'})});
      await page.route(matcher,injected);
      try {
        await page.locator('#apply').click();
        await page.waitForFunction(()=>document.getElementById('journal-status').textContent.includes('Проверочный сбой'));
        assert.equal(await page.locator('#export').getAttribute('href'),null);
        assert.equal(await page.locator('#journal-data').isVisible(),false);
      } finally { await page.unroute(matcher,injected); }
      await apply(page,{start:'2016-02-01',end:'2016-02-29',grain:'dekad'});
      assert.equal(await page.locator('#ledger-table tbody tr').count(),3);
      assert.ok(await page.locator('#export').getAttribute('href'));
      return {injectedHttpStatus:503,oldExportHidden:true,recoveredPeriods:3};
    });
    await step('Сравнение поляны и поля',async()=>{
      await page.getByRole('button',{name:'Поляна и поле',exact:true}).click();
      const rows=page.locator('#compare-table tbody tr');
      await rows.first().waitFor({state:'visible'});
      assert.equal(await rows.count(),12);
      await page.locator('#compare-modes').getByRole('button',{name:/час|суточ/i}).click();
      await page.waitForFunction(()=>document.querySelectorAll('#compare-table tbody tr').length===24);
      await checkWidth(page);
      assert.equal(await page.locator('#compare-chart path.chart-line').count(),1);
      assert.equal(await page.locator('#compare-chart path.chart-baseline').count(),1);
      return {months:12,hours:24,temperatureCurves:2};
    });
    await step('Загрузка нового JSON и отказ с указанием ошибочной строки',async()=>{
      await page.getByRole('button',{name:'Метеожурнал',exact:true}).click();
      await page.getByRole('link',{name:'Открыть файл станции'}).click();
      const file=page.locator('#import-file');
      await file.setInputFiles(path.join(exampleDir,'kr1-station-2024-04-01.json'));
      await page.locator('#import-station').selectOption('kr1');
      await page.locator('#import-param').selectOption('pressure_hpa');
      await page.locator('#import-grain').selectOption('day');
      await page.getByRole('button',{name:'Рассчитать'}).click();
      const ledger=page.locator('#import-rows tr');
      await ledger.first().waitFor({state:'visible'});
      assert.equal(await ledger.count(),1);
      const text=(await ledger.innerText()).replace(/[\u202f\u00a0]/g,'');
      assert.match(text,/1005,45/);
      assert.match(text,/38/);
      assert.match(text,/19/);
      const csv=await downloaded(page.locator('#import-download'),page);
      assert.match(csv,/2024-04-01/);
      await checkWidth(page);
      await page.screenshot({path:path.join(evidenceDir,'import-'+viewport.width+'.png')});
      const bad=Buffer.from(JSON.stringify([{datetime:'2024-02-30 12:00',qc_flag:'',t_air:2}]));
      await file.setInputFiles({name:'bad-date.json',mimeType:'application/json',buffer:bad});
      await page.getByRole('button',{name:'Рассчитать'}).click();
      await page.waitForFunction(()=>document.getElementById('import-status').textContent.includes('элемент 1'));
      assert.equal(await page.locator('#import-result').isVisible(),false);
      return {inputRows:48,accepted:38,usableHours:19,meanHpa:1005.45,navigation:await navigationContrast(page),invalidInput:await page.locator('#import-status').innerText()};
    });
    await step('Воспроизведение результата по ссылке после перезагрузки',async()=>{
      await page.goto(base+'/?station=kr2&param=pressure_hpa&start=2024-04-01&end=2024-04-30&grain=month');
      await tableSettled(page,'Апрель');
      assert.match(await page.locator('#ledger-table tbody tr').innerText(),/980,16/);
      await apply(page,{param:'t_air',station:'kr1',start:'2024-04-01',end:'2024-04-30'});
      const shared=new URL(page.url());
      assert.equal(shared.searchParams.get('station'),'kr1');
      assert.equal(shared.searchParams.get('param'),'t_air');
      await page.reload();
      await tableSettled(page,'Апрель');
      assert.match(await page.locator('#ledger-table tbody tr').innerText(),/28\s+из\s+30/);
      await page.goto(base+'/?view=compare&group=hours');
      await page.waitForFunction(()=>document.querySelectorAll('#compare-table tbody tr').length===24);
      await page.goto(base+'/?station=missing');
      await page.waitForFunction(()=>document.getElementById('journal-status').dataset.error==='true');
      assert.equal(await page.locator('#export').getAttribute('href'),null);
      return {restoredParam:'t_air',restoredDays:28,hourlyRows:24,invalidLinkRejected:true};
    });
    assert.deepEqual(result.jsErrors,[]);
    result.status='passed';
  } catch(error) {
    result.status='failed';
    result.error=error.message.split('\n')[0];
    result.visibleStatus=await page.locator('#journal-status').textContent().catch(()=>null);
  } finally { await page.close(); }
  return result;
}

async function main() {
  await fs.mkdir(evidenceDir,{recursive:true});
  const browser=await chromium.launch({executablePath:process.env.METEO_CHROMIUM || undefined});
  try {
    const runs=[];
    for (const viewport of [{width:1280,height:900},{width:390,height:844}]) runs.push(await runViewport(browser,viewport));
    const report={browser:browser.version(),runs};
    await fs.writeFile(path.join(evidenceDir,'acceptance.json'),JSON.stringify(report,null,2)+'\n');
    for (const run of runs) {
      console.log(run.viewport.width,run.status,run.steps.map(s=>s.status+' '+s.label).join(' · '));
      if (run.error) console.error(run.error,run.visibleStatus);
    }
    if (runs.some(r=>r.status!=='passed')) process.exitCode=1;
    else {
      const checks = runs.flatMap(r=>r.steps.filter(s=>s.observed?.navigation).map(s=>({
        viewport:r.viewport,stage:s.label,colors:s.observed.navigation
      })));
      await fs.writeFile(path.join(root,'results','contrast-check.json'),JSON.stringify({browser:browser.version(),checks},null,2)+'\n');
    }
  } finally { await browser.close(); }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
