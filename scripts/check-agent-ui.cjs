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
  battery.temp_c = 31.5;
  // Core groups after platmon v1.8.2's examples (network.md, disk-io.md, pressure.md); values are synthetic.
  Object.assign(node.stats.memory, {used:10918944768, total:32668807168, swap_used:0, swap_total:8589930496});
  node.stats.disk = {used:84053614592, total:194949083136};
  node.stats.network = {scope:{kind:'process_network_namespace'}, provider:'proc_net_dev', interfaces:[
    {name:'eth0', ifindex:2, rx:{bytes:1000, packets:10, errors:3, dropped:0}, tx:{bytes:2000, packets:20, errors:0, dropped:7},
     rates:{rx_bytes_per_s:2097152, tx_bytes_per_s:200, rx_packets_per_s:1500.4, tx_packets_per_s:2}, window_ms:1000, reason:null},
    {name:'wlan0', ifindex:3, rx:{bytes:0, packets:0, errors:0, dropped:0}, tx:{bytes:0, packets:0, errors:0, dropped:0},
     rates:null, window_ms:null, reason:'warmup'}]};
  node.stats.disk_io = {scope:{kind:'host_block_devices'}, provider:'proc_diskstats', disks:[
    {name:'nvme0n1', major:259, minor:0, rates:{read_bytes_per_s:4096, write_bytes_per_s:8192, reads_per_s:1, writes_per_s:2.6,
     io_time_ratio:0.01}, window_ms:1000, reason:null}]};
  node.stats.pressure = {scope:{kind:'host'}, provider:'proc_pressure', resources:{
    cpu:{some:{avg10:1.25, avg60:0.5, avg300:0.1, total_us:123456}, full:null},
    memory:{some:{avg10:0, avg60:0, avg300:0, total_us:10}, full:{avg10:0, avg60:0, avg300:0, total_us:4}},
    io:{some:{avg10:3, avg60:1, avg300:0.2, total_us:99999}, full:{avg10:0.5, avg60:0.1, avg300:0, total_us:4567}}}};
  node.stats.collectors.temperature = {state:'partial', reason:'some_unreadable',
    issues:[{target:'hwmon2.temp1', reason:'invalid_data'}], issues_truncated:2};
  // storage, wifi and probe follow platmon v1.8.2's observation examples; values are synthetic.
  const publication = id => ({id, data_age_ms:1000, stale:false});
  node.observations.storage = {state:'ok', interval_ms:30000, stale_after_ms:90000, observation:publication(4),
    collector:{state:'ok', reason:null}, data:{filesystems:[
      {device:'nvme0n1p1', major:259, minor:1, fstype:'ext4', source:'/dev/nvme0n1p1',
       mount_points:[{path:'/', read_only:false}, {path:'<img src=x onerror=alert(2)>', read_only:true}],
       total_bytes:982806532096, used_bytes:113806532096, available_bytes:819000000000}]}};
  node.observations.wifi = {state:'ok', interval_ms:10000, stale_after_ms:30000, observation:publication(5),
    collector:{state:'ok', reason:null}, data:{interfaces:[
      {name:'wlan0', connected:true, connection_state:'connected', signal_dbm:-64, signal_raw:null, link_quality:46, noise_dbm:null},
      {name:'wlan1', connected:null, connection_state:'permission_denied', signal_dbm:null, signal_raw:null, link_quality:null, noise_dbm:null}]}};
  node.observations.probe = {state:'partial', interval_ms:10000, stale_after_ms:30000, observation:publication(6),
    collector:{state:'partial', reason:'some_unreadable'}, data:{provider:'tcp_connect', timeout_ms:2000, targets:[
      {address:'192.0.2.10', port:443, connect_ms:12.3, reason:null, attempts:10, failures:1, failure_ratio:0.1},
      {address:'2001:db8::1', port:22, connect_ms:null, reason:'timeout', attempts:10, failures:10, failure_ratio:1}]}};
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
                            '3.03 Ah / 3.40 Ah / 4.02 Ah', '84.5% of design', 'ADP1', 'Unknown', '6.50 W', '31.5°C']) {
      assert(text.includes(expected), 'missing ' + expected);
    }
    for (const expected of ['Storage · ok · Current', 'nvme0n1p1', '/, <img src=x onerror=alert(2)> (read-only)', 'ext4',
                            '106.0 GiB / 915.3 GiB', '762.8 GiB',
                            'Wi-Fi · ok · Current', 'wlan0', 'Connected', '-64 dBm', 'Unknown · permission_denied',
                            'Probe · partial · Current · 1.0s ago · some_unreadable', '192.0.2.10:443', '12.3 ms',
                            'connected', '1 / 10', '10.0%', '[2001:db8::1]:22', 'timeout', '10 / 10', '100.0%']) {
      assert(text.includes(expected), 'missing ' + expected);
    }
    for (const expected of ['10.2 GiB / 30.4 GiB', '0 B / 8.0 GiB', '78.3 GiB / 181.6 GiB',
                            'Pressure (PSI avg10)', '1.3%', '3.0%', '0.5%',
                            '2.0 MiB/s', '1500 / 2', '3 / 0', '0 / 7', 'warmup',
                            '4.0 KiB/s', '1 / 3', '1.0%', 'Errors and dropped are counts',
                            'Collectors', 'some_unreadable', 'hwmon2.temp1: invalid_data (+2 more)']) {
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
    node.core_error = 'host_refused';
    await refresh('Core: host_refused: the Agent refused this host name (HTTP 421).');
    assert((await details.textContent()).includes('[http] hosts'));
    delete node.core_error;
    battery.percent = 0;
    battery.temp_c = null;
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
    assert(!text.includes('31.5°C'));
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
    node.observations.wifi.state = 'stale';
    node.observations.wifi.observation.data_age_ms = 31000;
    await refresh('Wi-Fi · stale · Last-known / not current');
    node.observations = {};
    for (const key of ['network', 'disk_io', 'pressure']) delete node.stats[key];  // platmon before 1.3.0 may have none
    node.stats.memory = {used:250, total:1000};
    await refresh('No battery observation reported by this agent.');
    text = await details.textContent();
    for (const noun of ['storage', 'Wi-Fi', 'probe']) assert(text.includes('No ' + noun + ' observation reported by this agent.'));
    assert(text.includes('No pressure (PSI) data reported by this agent.') && text.includes('250 B / 1000 B'));
    assert(text.includes('unmeasured_gpu') && text.includes('not_exposed'));
    assert.match(await page.locator('.node-card').textContent(), /NPUN\/A/);
    node.observations.battery = {state:'unavailable',observation:null,collector:{reason:'not_detected'},data:null};
    await refresh('not_detected');
    assert((await details.textContent()).includes('No battery or external supply readings available.'));
    assert.deepEqual(errors, []);
    console.log('Agent 1.2.0 UI passed: NPU, GPU metadata/nulls, battery units/signs/zero, sizes/PSI/network/disk/collectors, storage/Wi-Fi/probe, freshness, the 421 hint, old agents, escaping, mobile and themes.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
