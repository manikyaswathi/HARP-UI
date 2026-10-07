/* Offline demo of the HARP profiling pages.
   Stands in for the HARP server (/api/*) so profiling.html, runs.html and profile-data.html
   can be opened straight from disk. The real pages talk to the HARP server,
   which talks to TAPIS. Built into the demo pages by build_demo.py. */
(function(){
  // ---- sample apps (as TAPIS would list them) ------------------------------
  const harp = (label, entrypoint, command, params) => ({harp:{label, entrypoint, command, params}});
  const APPS = [
    {id:'harp-sweep-euler-swathi', version:'1.0.0', runtime:'SINGULARITY',
     image:'docker://ghcr.io/manikyaswathi/harp-sweep-eulernumber:1.0.0', app_args:[],
     notes: harp('Euler number','calc_e.py','python3 calc_e.py {method} {n} {precision}',[
       {name:'method',arg:'{method}',kind:'string',default:'pow'},
       {name:'n',arg:'{n}',kind:'int',default:'1000'},
       {name:'precision',arg:'{precision}',kind:'int',default:'64'}])},
    {id:'harp-sweep-yolo-swathi', version:'1.0.0', runtime:'SINGULARITY',
     image:'docker://ghcr.io/manikyaswathi/harp-sweep-yolo:1.0.0', app_args:[],
     notes: harp('Ultralytics YOLO','train_yolo.py',
       'python3 train_yolo.py --model {model} --epochs {epochs} --imgsz {imgsz} --batch {batch} --device {device}',[
       {name:'model',arg:'--model',kind:'string',default:'yolo11n'},
       {name:'epochs',arg:'--epochs',kind:'int',default:'1'},
       {name:'imgsz',arg:'--imgsz',kind:'int',default:'640'},
       {name:'batch',arg:'--batch',kind:'int',default:'8'},
       {name:'device',arg:'--device',kind:'string',default:'auto'}])},
    {id:'megadetector-batch', version:'5.0', runtime:'DOCKER',
     image:'docker.io/microsoft/megadetector:5.0', notes:{},
     app_args:[{name:'batch',arg:'--batch'},{name:'imgsz',arg:'--imgsz'},{name:'threshold',arg:'--threshold'}]}
  ];

  // ---- sample profiling data (deterministic) --------------------------------
  let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const jitter = () => 0.92 + rnd() * 0.16;
  const HW = {
    pitzer:   {sys_name:'Linux', sys_tot_cores_count:40, sys_phy_mem_bytes:201326592000, sys_gpu_count:0, sys_gpu_name:'none'},
    cardinal: {sys_name:'Linux', sys_tot_cores_count:96, sys_phy_mem_bytes:1080033280000, sys_gpu_count:4, sys_gpu_name:'NVIDIA H100'}};
  function cartesian(params){
    return Object.entries(params).reduce((acc, [k, vs]) => acc.flatMap(a => vs.map(v => ({...a, [k]: v}))), [{}]);
  }
  // A sweep with its TAPIS jobs (one combination per job), spread over hardware.
  const QUEUE = {pitzer:'serial', cardinal:'gpu'};
  let uuidN = 4100;
  function sweep(name, appId, sets, reps, timeFn, hosts, opts = {}){
    const created = Date.now() - (opts.hoursAgo || 1) * 3600e3;
    const rows = [], jobs = []; let idx = 0;
    for (const [runType, params] of sets) for (const p of cartesian(params)) {
      const host = hosts[idx % hosts.length];
      const job = {name:`${name}-${runType}-${String(idx).padStart(4,'0')}`, system_id:host, target:host, run_type:runType,
                   combinations:1, uuid:null, error:null, submitted_at:null, ended_at:null, status:'FINISHED'};
      const at = mins => new Date(created + mins * 60e3).toISOString();
      if (opts.failEvery && idx % opts.failEvery === opts.failEvery - 1) {
        Object.assign(job, {status:'FAILED', error:'job exceeded maxMinutes (30)'});
      } else if (opts.runningFrom != null && idx >= opts.runningFrom) {
        const k = idx - opts.runningFrom;
        job.status = k < 2 ? 'RUNNING' : k < 3 ? 'QUEUED' : 'NOT_SUBMITTED';
      }
      if (job.status !== 'NOT_SUBMITTED') {
        job.uuid = `${(uuidN++).toString(16)}e1c-7a2b-4c1d-9f00-${String(idx).padStart(12,'0')}-007`;
        job.submitted_at = at(2 + idx * 3);
      }
      if (job.status === 'FINISHED' || job.status === 'FAILED') job.ended_at = at(8 + idx * 3);
      if (job.status === 'FINISHED') for (let r = 0; r < reps; r++) {
        const row = {campaign:name, system:host, run_config:`${job.name}.run-${idx}.iteration-${r}`, run_type:runType, ...HW[host]};
        for (const [k, v] of Object.entries(p)) row['run_' + k] = v;
        row.walltime = +(timeFn(p, host) * jitter()).toFixed(5);
        rows.push(row);
      }
      jobs.push(job); idx++;
    }
    const view = {id:name, name, application:appId, app_ids:[appId], created_at:new Date(created).toISOString(),
      repetitions:reps, storage:{system_id:'pitzer'}, campaign_dir:`/fs/scratch/PAS2271/swathi/harp_runs/${name}`,
      hardware: hosts.map(h => ({key:h, system_id:h, queue:QUEUE[h], app_id:appId,
        cores_per_node: h === 'cardinal' ? 8 : 4, memory_mb: h === 'cardinal' ? 32000 : 16000, gpu: h === 'cardinal'})),
      events:[{at:new Date(created).toISOString(), message:`created with ${jobs.length} jobs`}]};
    const sw = {view, jobs, rows};
    recount(sw);
    return sw;
  }
  function recount(sw){
    const v = sw.view, counts = {};
    for (const j of sw.jobs) counts[j.status] = (counts[j.status] || 0) + 1;
    const ended = s => ['FINISHED','FAILED','CANCELLED','SUBMIT_FAILED'].includes(s);
    v.job_counts = counts; v.jobs_total = sw.jobs.length;
    v.jobs_done = sw.jobs.filter(j => ended(j.status)).length;
    if (v.status !== 'CANCELLED')
      v.status = v.jobs_done < v.jobs_total ? 'RUNNING' : !sw.rows.length ? 'FAILED' : counts.FINISHED === v.jobs_total ? 'DONE' : 'DONE_WITH_ERRORS';
    v.result = v.status === 'RUNNING' ? null : {rows: sw.rows.length};
  }
  const eulerTime = p => (p.method === 'pow' ? 0.002 : 0.02) * Math.pow(p.n / 1000, p.method === 'factorial' ? 1.25 : 0.4) * (1 + p.precision / 512);
  const yoloTime = (p, host) => {
    const size = {yolo11n:1, yolo11s:2.4, yolo11m:5.8}[p.model];
    const gpu = host === 'cardinal' ? 0.08 : 1;
    return 6 + p.epochs * size * Math.pow(p.imgsz / 320, 2) * (16 / p.batch) ** 0.15 * 22 * gpu;
  };
  const SWEEPS = [
    sweep('euler-sd-fs', 'harp-sweep-euler-swathi', [
      ['SD', {method:['pow','factorial'], n:[10,100,1000], precision:[64]}],
      ['FS', {method:['pow','factorial'], n:[10000,50000], precision:[64,256]}],
      ['test_data', {method:['factorial'], n:[20000], precision:[128]}]], 3, eulerTime, ['pitzer'], {failEvery:9, hoursAgo:30}),
    sweep('euler-precision', 'harp-sweep-euler-swathi', [
      ['SD', {method:['pow','factorial'], n:[1000], precision:[32,64,128,256,512]}]], 2, eulerTime, ['pitzer'], {hoursAgo:5}),
    sweep('yolo-cpu-gpu', 'harp-sweep-yolo-swathi', [
      ['SD', {model:['yolo11n','yolo11s'], epochs:[1,2], imgsz:[320,480], batch:[4,8], device:['auto']}],
      ['FS', {model:['yolo11n','yolo11s','yolo11m'], epochs:[5], imgsz:[640], batch:[8,16], device:['auto']}],
      ['test_data', {model:['yolo11s'], epochs:[3], imgsz:[640], batch:[8], device:['auto']}]], 1, yoloTime, ['pitzer','cardinal'], {runningFrom:19, hoursAgo:2})
  ];

  // ---- the fake /api ----------------------------------------------------------
  const KEY = 'harp-demo-user';
  const getMe = () => { try { return JSON.parse(sessionStorage.getItem(KEY)); } catch (e) { return window.__harpMe || null; } };
  const setMe = v => { window.__harpMe = v; try { v ? sessionStorage.setItem(KEY, JSON.stringify(v)) : sessionStorage.removeItem(KEY); } catch (e) {} };
  const json = (status, body) => new Response(JSON.stringify(body), {status, headers:{'Content-Type':'application/json'}});
  const later = v => new Promise(r => setTimeout(() => r(v), 300));
  const toCSV = (cols, rows) => [cols.join(','), ...rows.map(r => cols.map(c => r[c] ?? '').join(','))].join('\n') + '\n';
  function profileOf(appId){
    const mine = SWEEPS.filter(s => s.view.app_ids.includes(appId));
    const rows = mine.flatMap(s => s.rows);  // running sweeps show their finished jobs
    const cols = ['campaign', 'system'];
    for (const r of rows) for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k);
    if (cols.includes('walltime')) { cols.splice(cols.indexOf('walltime'), 1); cols.push('walltime'); }
    return {app_id:appId, campaigns:mine.map(s => s.view), columns:cols, rows, total_rows:rows.length, missing:[]};
  }
  const realFetch = window.fetch.bind(window);
  window.fetch = function(url, opts = {}){
    const path = decodeURIComponent(String(url));
    if (path === 'hardware_targets.csv') return Promise.reject(new Error('demo uses embedded data'));
    if (!path.startsWith('/api/')) return realFetch(url, opts);
    let body = {}; try { body = opts.body ? JSON.parse(opts.body) : {}; } catch (e) {}
    const me = getMe();
    if (path === '/api/login') {
      if (!body.username || !body.password) return later(json(400, {detail:'Enter your TAPIS username and password.'}));
      const u = {username:body.username, base_url:body.base_url || 'https://icicle.tapis.io', expires_at:null};
      setMe(u); return later(json(200, u));
    }
    if (path === '/api/logout') { setMe(null); return Promise.resolve(json(200, {ok:true})); }
    if (!me) return Promise.resolve(json(401, {detail:'log in to TAPIS first'}));
    if (path === '/api/me') return Promise.resolve(json(200, me));
    if (path === '/api/tapis/apps') return later(json(200, APPS));
    if (path === '/api/campaigns') return Promise.resolve(json(200, SWEEPS.map(s => s.view)));
    let c = path.match(/^\/api\/campaigns\/([^/]+)(\/(cancel|resubmit))?$/);
    if (c) {
      const sw = SWEEPS.find(x => x.view.id === c[1]);
      if (!sw) return Promise.resolve(json(404, {detail:'campaign not found'}));
      const now = new Date().toISOString();
      if (c[3] === 'cancel') {
        for (const j of sw.jobs) if (!['FINISHED','FAILED'].includes(j.status)) Object.assign(j, {status:'CANCELLED', ended_at:now});
        sw.view.status = 'CANCELLED'; sw.view.events.push({at:now, message:'cancelled by user'}); recount(sw);
      } else if (c[3] === 'resubmit') {
        let n = 0;
        for (const j of sw.jobs) if (['FAILED','CANCELLED'].includes(j.status)) { Object.assign(j, {status:'NOT_SUBMITTED', uuid:null, error:null, ended_at:null}); n++; }
        sw.view.status = 'RUNNING'; sw.view.events.push({at:now, message:`resubmitting ${n} jobs`}); recount(sw);
        return later(json(200, {resubmitted:n}));
      }
      return later(json(200, {...sw.view, jobs:sw.jobs}));
    }
    let m = path.match(/^\/api\/apps\/(.+)\/profile\.csv$/);
    if (m) { const p = profileOf(m[1]);
      return Promise.resolve(p.rows.length ? new Response(toCSV(p.columns, p.rows), {status:200, headers:{'Content-Type':'text/csv'}})
                                           : json(404, {detail:'no profiling data for this app yet'})); }
    m = path.match(/^\/api\/apps\/(.+)\/profile$/);
    if (m) return later(json(200, profileOf(m[1])));
    return Promise.resolve(json(404, {detail:'not part of the demo'}));
  };

  // Only these two pages exist in the demo.
  document.addEventListener('click', ev => {
    const a = ev.target.closest('a[href]');
    if (!a) return;
    const href = a.getAttribute('href');
    if (['profiling.html', 'runs.html', 'profile-data.html'].includes(href)) return;
    if (!/\.html$/.test(href)) return;
    ev.preventDefault();
    let t = document.getElementById('demoToast');
    t.textContent = 'Only Profiling, Runs and Profile data are part of this demo.';
    t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2200);
  });
})();
