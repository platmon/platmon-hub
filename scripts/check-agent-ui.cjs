/* Optional browser checks against an empty loopback Hub; all Agent data is synthetic. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

(async () => {
  const base = process.argv[2];
  assert(base && /^http:\/\/127\.0\.0\.1:\d+$/.test(base), 'provide a loopback test Hub URL');
  const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, '../tests/fixtures/agent-v1.2.0.json'), 'utf8'));
  const node = {node_id:'00000000-0000-0000-0000-000000000001', name:'Synthetic 1.2.0 host',
    enabled:true, headline:'live', data_current:true, data_age_ms:100, sample_sequence:42,
    overview:{cpu_percent:25,gpu_percent:12.5,npu_percent:0,memory_percent:25,disk_percent:50,hottest_c:51.2},
    stats:fixture.stats, observations:fixture.observations.groups};
  const battery = node.observations.battery.data.batteries[0];
  battery.name = '<img src=x onerror=alert(1)>';
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/nodes**', route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/history')) return route.fulfill({json:{schema_version:1,node_id:node.node_id,
        metric:url.searchParams.get('metric'), from_ms:Number(url.searchParams.get('from_ms')),
        to_ms:Number(url.searchParams.get('to_ms')), points:[],gaps:[]}});
      return route.fulfill({json:url.pathname === '/api/nodes' ? {schema_version:1,nodes:[node]} : {schema_version:1,node}});
    });
    await page.goto(base);
    const details = page.locator('#detail-body');
    await details.getByRole('heading', {name:'Power & battery', exact:true}).waitFor();
    let text = await details.textContent();
    for (const expected of ['intel_vpu', 'rc6_residency', '0.00 MHz', '89.0%', 'Discharging', '-0.25 A',
                            '3.03 Ah / 3.40 Ah / 4.02 Ah', '84.5% of design', 'ADP1', 'Unknown', '6.50 W']) {
      assert(text.includes(expected), 'missing ' + expected);
    }
    assert.equal(await details.locator('img,script').count(), 0);
    assert.match(await page.locator('.node-card').textContent(), /NPU0\.0%/);
    await page.locator('#history-metric').selectOption('npu_percent');
    await page.waitForFunction(() => document.getElementById('history-status').textContent.includes('history points'));

    async function refresh(expected) {
      await page.locator('#refresh').click();
      await page.waitForFunction(value => document.getElementById('detail-body').textContent.includes(value), expected);
    }
    node.observations.battery.state = 'stale';
    node.observations.battery.observation.data_age_ms = 181000;
    await refresh('Battery · stale · Last-known / not current');
    node.observations.battery.state = 'ok';
    node.observations.battery.observation.data_age_ms = 1000;
    node.optional_error = 'transport';
    await refresh('Battery · ok · Last-known / not current');
    delete node.optional_error;
    battery.percent = 0;
    battery.current_ua = 0;
    battery.power_uw = -1500000;
    battery.charge_now_uah = null;
    battery.charge_full_uah = null;
    battery.charge_full_design_uah = null;
    battery.energy_now_uwh = 0;
    battery.energy_full_uwh = 50000000;
    battery.energy_full_design_uwh = 60000000;
    node.observations.battery.data.external[0].online = false;
    await refresh('0.00 Wh / 50.00 Wh / 60.00 Wh');
    text = await details.textContent();
    assert(text.includes('0.0%') && text.includes('0.00 A') && text.includes('-1.50 W'));
    assert(text.includes('83.3% of design') && text.includes('Offline'));
    assert(!text.includes('Ah'));
    await page.emulateMedia({colorScheme:'dark'});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path:path.join(os.tmpdir(), 'platmon-agent120-dark.png'),fullPage:true});
    await page.emulateMedia({colorScheme:'light'});
    await page.setViewportSize({width:1280,height:1000});
    await page.screenshot({path:path.join(os.tmpdir(), 'platmon-agent120-light.png'),fullPage:true});

    battery.present = false;
    battery.percent = 99;
    await refresh('Not present');
    assert(!(await details.textContent()).includes('99.0%'));
    node.stats.gpu = {driver:'unmeasured_gpu',usage:null,freq:null,max_freq:null};
    node.stats.collectors.gpu = {state:'unavailable',reason:'not_exposed'};
    delete node.stats.npu;
    delete node.stats.collectors.npu;
    node.overview.npu_percent = null;
    node.overview.gpu_percent = null;
    node.observations = {};
    await refresh('No battery observation reported by this agent.');
    text = await details.textContent();
    assert(text.includes('unmeasured_gpu') && text.includes('not_exposed'));
    assert.match(await page.locator('.node-card').textContent(), /NPUN\/A/);
    node.observations.battery = {state:'unavailable',observation:null,collector:{reason:'not_detected'},data:null};
    await refresh('not_detected');
    assert((await details.textContent()).includes('No battery or external supply readings available.'));
    assert.deepEqual(errors, []);
    console.log('Agent 1.2.0 UI passed: NPU, GPU metadata/nulls, battery units/signs/zero, freshness, old agents, escaping, mobile and themes.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
