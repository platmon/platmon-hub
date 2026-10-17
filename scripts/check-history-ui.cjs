/* Optional browser verification against an empty local Hub. No agent traffic. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
(async () => {
  const base = process.argv[2];
  assert(base && /^http:\/\/127\.0\.0\.1:\d+$/.test(base), 'provide a loopback test Hub URL');
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const ids = ['00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002'];
    const nodes = ids.map((node_id, i) => ({node_id,name:i ? 'Synthetic Pi' : '<img src=x onerror=alert(1)>',
      headline:'live', data_current:true, sample_sequence:1, data_age_ms:50, overview:{cpu_percent:25}}));
    let fail = false, hold = false, release = null, historyRequests = 0, aborted = 0;
    page.on('requestfailed', request => { if (request.url().includes('/history?')) aborted++; });
    await page.route('**/api/nodes**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/history')) {
        historyRequests++;
        if (hold) { hold = false; await new Promise(resolve => { release = resolve; }); }
        if (fail) { await route.fulfill({status:503,json:{schema_version:1,error:'history_unavailable'}}); return; }
        const start = Number(url.searchParams.get('from_ms')), end = Number(url.searchParams.get('to_ms'));
        const metric = url.searchParams.get('metric');
        const scale = metric.endsWith('_bytes_per_s') ? 1048576 : 1;
        const points = Array.from({length:12}, (_,i) => ({timestamp_ms:start+(end-start)*(i+1)/14,
          instance_id:i<6?'a':'b',segment:i<6?0:1,average:metric==='gpu_percent'?null:(20+i)*scale,
          minimum:metric==='gpu_percent'?null:10*scale,maximum:metric==='gpu_percent'?null:90*scale,count:metric==='gpu_percent'?0:5}));
        await route.fulfill({json:{schema_version:1,node_id:url.pathname.split('/')[3],metric,
          from_ms:start,to_ms:end,points,gaps:[{start_ms:start+(end-start)*.4,end_ms:start+(end-start)*.5,reason:'agent_restart'},
            {start_ms:start+(end-start)*.8,end_ms:start+(end-start)*.85,reason:'offline'}]}}).catch(()=>{});
      } else if (url.pathname === '/api/nodes') await route.fulfill({json:{schema_version:1,nodes}});
      else await route.fulfill({json:{schema_version:1,node:nodes.find(n=>url.pathname.endsWith(n.node_id))}});
    });
    await page.route('**/api/alerts', route => route.fulfill({json:{schema_version:1,enabled:true,ready:true,total:1,
      alerts:[{node_id:ids[0],rule_id:'<script>bad</script>',state:'Active',severity:'critical',known:false,changed_at_ms:Date.now()}]}}));
    await page.goto(base);
    await page.waitForFunction(()=>document.getElementById('alerts-list').children.length === 1);
    assert.equal(await page.locator('#alerts-list script').count(), 0);
    assert.match(await page.locator('#alerts-list').textContent(), /awaiting current evidence/);
    await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('history points'));
    assert.equal(await page.locator('.node-card').count(), 2);
    assert.equal(await page.locator('.node-card img').count(), 0);
    await page.locator('#history-metric').selectOption('gpu_percent');
    await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('history points'));
    await page.locator('#history-chart').focus();
    await page.keyboard.press('ArrowRight');
    assert.match(await page.locator('#history-value').textContent(), /Average N\/A/);
    await page.locator('#history-range').selectOption('168');
    await page.locator('#history-metric').selectOption('hottest_c');
    await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('history points'));
    const metrics = await page.locator('#history-metric option').evaluateAll(options => options.map(o => o.value));
    for (const name of ['psi_cpu_some_avg10','psi_memory_some_avg10','psi_io_some_avg10','network_rx_busiest_bytes_per_s',
                        'network_tx_busiest_bytes_per_s','disk_read_bytes_per_s','disk_write_bytes_per_s']) assert(metrics.includes(name), name);
    await page.locator('#history-metric').selectOption('psi_io_some_avg10');
    await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('history points'));
    await page.locator('#history-chart').focus();
    await page.keyboard.press('ArrowRight');
    assert.match(await page.locator('#history-value').textContent(), /Average \d+\.\d%/);
    // Byte rates are unbounded: values above 100 must be shown in units, not clipped to a percent axis.
    await page.locator('#history-metric').selectOption('disk_read_bytes_per_s');
    await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('history points'));
    await page.locator('#history-chart').focus();
    await page.keyboard.press('ArrowRight');
    assert.match(await page.locator('#history-value').textContent(), /Average \d+\.\d MiB\/s · Min 10\.0 MiB\/s · Max 90\.0 MiB\/s/);
    await page.emulateMedia({colorScheme:'dark'});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path:path.join(os.tmpdir(), 'platmon-history-dark.png'),fullPage:true});
    fail = true;
    await page.locator('#history-refresh').click();
    await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('Live monitoring continues'));
    assert(await page.locator('#connection-error').isHidden());
    assert.equal(await page.locator('.node-card').count(), 2);
    fail = false; hold = true;
    await page.locator('#history-refresh').click();
    await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('Loading'));
    await page.locator('.node-card').nth(1).click();
    await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('history points'));
    if (release) release();
    const before = historyRequests;
    await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
    await page.locator('#history-refresh').click();
    assert.equal(historyRequests, before);
    await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));});
    await page.waitForFunction(()=>document.getElementById('history-status').textContent.includes('history points'));
    await page.emulateMedia({colorScheme:'light'});
    await page.setViewportSize({width:1280,height:1000});
    await page.screenshot({path:path.join(os.tmpdir(), 'platmon-history-light.png'),fullPage:true});
    assert.deepEqual(errors, []);
    assert(aborted > 0, 'selection should abort an outstanding history request');
    console.log('History UI passed: nulls, metrics/ranges, byte-rate units, theme/mobile, failure isolation, cancellation, hidden-tab pause; requests=' + historyRequests);
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
