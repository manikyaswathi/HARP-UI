/* Offline demo of the HARP profiling pages.
   Stands in for the HARP server (/api/*) so profiling.html
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

  // ---- TAPIS execution systems -------------------------------------------------
  // As TAPIS describes them: system + batchLogicalQueues (LogicalQueue fields).
  const Q = (name, hpc, o) => ({name, hpc_queue:hpc, description:'', default:false, max_jobs:-1, min_nodes:1,
    min_cores:1, min_memory_mb:1, min_minutes:1, ...o});
  const SYSTEMS = [
    {id:'pitzer', host:'pitzer.osc.edu', description:'Pitzer cluster, Ohio Supercomputer Center', system_type:'LINUX',
     enabled:true, can_exec:true, can_run_batch:true, batch_scheduler:'SLURM', runtimes:['SINGULARITY'], default_queue:'serial',
     queues:[Q('serial','serial',{default:true, max_nodes:1, max_cores:40, max_memory_mb:160000, max_minutes:10080, max_jobs_per_user:4}),
             Q('parallel','parallel',{min_nodes:2, max_nodes:81, max_cores:40, max_memory_mb:160000, max_minutes:5760, max_jobs_per_user:2}),
             Q('gpuserial-40core','gpuserial-40core',{description:'2x V100 per node', max_nodes:1, max_cores:40, max_memory_mb:160000, max_minutes:5760, max_jobs_per_user:2})]},
    {id:'cardinal', host:'cardinal.osc.edu', description:'Cardinal cluster, Ohio Supercomputer Center', system_type:'LINUX',
     enabled:true, can_exec:true, can_run_batch:true, batch_scheduler:'SLURM', runtimes:['SINGULARITY'], default_queue:'cpu',
     queues:[Q('cpu','cpu',{default:true, max_nodes:1, max_cores:96, max_memory_mb:515000, max_minutes:10080, max_jobs_per_user:4}),
             Q('gpu','gpu',{description:'4x H100 per node', max_nodes:1, max_cores:96, max_memory_mb:1030000, max_minutes:4320, max_jobs_per_user:2})]},
    {id:'stampede3', host:'stampede3.tacc.utexas.edu', description:'Stampede3, Texas Advanced Computing Center', system_type:'LINUX',
     enabled:true, can_exec:true, can_run_batch:true, batch_scheduler:'SLURM', runtimes:['SINGULARITY'], default_queue:'skx',
     queues:[Q('skx','skx',{default:true, description:'Skylake, 48 cores', max_nodes:256, max_cores:48, max_memory_mb:192000, max_minutes:2880, max_jobs_per_user:20}),
             Q('icx','icx',{description:'Ice Lake, 80 cores', max_nodes:32, max_cores:80, max_memory_mb:256000, max_minutes:2880, max_jobs_per_user:20}),
             Q('h100','h100',{description:'4x H100 per node', max_nodes:4, max_cores:96, max_memory_mb:1000000, max_minutes:2880, max_jobs_per_user:4})]}];
  const STORAGE_ONLY = [{id:'osc-project-storage', host:'sftp.osc.edu', description:'OSC project space', system_type:'LINUX', enabled:true, can_exec:false}];
  HW.stampede3 = {sys_name:'Linux', sys_tot_cores_count:48, sys_phy_mem_bytes:201326592000, sys_gpu_count:0, sys_gpu_name:'none'};
  const HWOF = id => HW[id] || HW.pitzer;

  // A sweep launched from the page: jobs move through TAPIS states over ~a minute.
  function launched(spec, created = Date.now()){
    const name = spec.name, appId = spec.targets[0].app_id;
    const jobs = [], plan = [];
    const combos = cartesian(spec.run_sets[0].parameters);
    let i = 0;
    for (const p of combos) for (const t of spec.targets) {
      jobs.push({name:`${name}-${spec.run_sets[0].run_type}-${String(i).padStart(4,'0')}`, system_id:t.system_id,
        target:`${t.system_id}|${t.queue}`, queue:t.queue, run_type:spec.run_sets[0].run_type, combinations:1, status:'NOT_SUBMITTED',
        uuid:null, error:null, submitted_at:null, ended_at:null});
      plan.push({p, t, at: 1500 + i * 1200, run: 6000 + i * 1500, end: 14000 + i * 2500}); i++;
    }
    const view = {id:name, name, application:spec.application, app_ids:[appId], created_at:new Date(created).toISOString(),
      repetitions:spec.repetitions, storage:spec.storage, campaign_dir:`${spec.storage.path}/${name}`,
      hardware: spec.targets.map(t => ({key:`${t.system_id}|${t.queue}`, system_id:t.system_id, queue:t.queue, app_id:appId,
        cores_per_node:t.cores_per_node, memory_mb:t.memory_mb, gpu:!!t.container_args})),
      events:[{at:new Date(created).toISOString(), message:`created with ${jobs.length} jobs`}]};
    const sw = {view, jobs, rows:[], live:{created, plan, reps:spec.repetitions}};
    recount(sw); return sw;
  }
  function advance(sw){
    if (!sw.live || sw.view.status === 'CANCELLED') return;
    const t = Date.now() - sw.live.created;
    sw.jobs.forEach((j, k) => {
      const pl = sw.live.plan[k];
      if (['FINISHED','CANCELLED','FAILED'].includes(j.status)) return;
      if (t >= pl.end) {
        j.status = 'FINISHED'; j.ended_at = new Date().toISOString();
        const host = pl.t.system_id, gpu = !!pl.t.container_args;
        for (let r = 0; r < sw.live.reps; r++) {
          const row = {campaign:sw.view.name, system:host, run_config:`${j.name}.run-${k}.iteration-${r}`, run_type:j.run_type, ...HWOF(host)};
          for (const [key, v] of Object.entries(pl.p)) row['run_' + key] = v;
          const base = sw.view.app_ids[0].includes('yolo') ? yoloTime(pl.p, gpu ? 'cardinal' : 'pitzer')
                     : eulerTime({method:'pow', n:1000, precision:64, ...pl.p});
          row.walltime = +(base * jitter()).toFixed(5); sw.rows.push(row);
        }
        sw.view.events.push({at:j.ended_at, message:`${j.name} FINISHED on ${host}`});
      } else if (t >= pl.run) j.status = 'RUNNING';
      else if (t >= pl.at) { if (!j.uuid) { j.uuid = `${(uuidN++).toString(16)}a7c-0d1e-4b2f-8c3a-${String(k).padStart(12,'0')}-007`; j.submitted_at = new Date().toISOString(); } j.status = t >= pl.at + 2000 ? 'QUEUED' : 'PENDING'; }
    });
    recount(sw);
    if (sw.view.status !== 'RUNNING' && !sw.live.closed) {
      sw.live.closed = true;
      sw.view.events.push({at:new Date().toISOString(), message:`${sw.view.status}: ${sw.rows.length} profiling rows -> ${sw.view.campaign_dir}`});
    }
  }

  // ---- the fake /api ----------------------------------------------------------
  const KEY = 'harp-demo-user', LKEY = 'harp-demo-launched';
  // Sweeps launched earlier in this browser session (each page has its own copy of this demo).
  try { for (const l of JSON.parse(sessionStorage.getItem(LKEY) || '[]')) SWEEPS.unshift(launched(l.spec, l.created)); } catch (e) {}
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
    SWEEPS.forEach(advance);
    if (path === '/api/tapis/systems') return later(json(200, [...SYSTEMS.map(({queues, ...x}) => x), ...STORAGE_ONLY]));
    if (path === '/api/tapis/exec-systems') return later(json(200, SYSTEMS));
    const check = spec => {
      if (!spec.command) return 'command is required';
      if (!spec.targets || !spec.targets.length) return 'at least one target system (hardware) is required';
      const ph = [...spec.command.matchAll(/\{(\w+)\}/g)].map(m => m[1]);
      const missing = ph.filter(x => !(x in spec.run_sets[0].parameters));
      return missing.length ? `run_sets[0] has no values for command placeholders ${JSON.stringify(missing)}` : '';
    };
    if (path === '/api/campaigns/batch/preview') return later(json(200, (Array.isArray(body) ? body : []).map(sp => {
      const err = check(sp); if (err) return {name:sp.name, ok:false, error:err};
      const n = cartesian(sp.run_sets[0].parameters).length;
      return {name:sp.name, ok:true, summary:{combinations:n, jobs:n * sp.targets.length, total_runs:n * sp.targets.length * sp.repetitions}}; })));
    if (path === '/api/campaigns/batch') {
      for (const sp of body) { const err = check(sp); if (err) return later(json(400, {detail:`sweep '${sp.name}': ${err}`})); }
      const made = body.map((sp, i) => { const sw = launched(sp, Date.now() + i * 4000); SWEEPS.unshift(sw);
        try { const saved = JSON.parse(sessionStorage.getItem(LKEY) || '[]'); saved.push({spec:sp, created:sw.live.created});
              sessionStorage.setItem(LKEY, JSON.stringify(saved)); } catch (e) {}
        return {...sw.view}; });
      return later(json(200, made));
    }
    if (path === '/api/jobs') return later(json(200, SWEEPS.flatMap(sw => sw.jobs.map(j => ({...j,
      sweep:sw.view.name, sweep_id:sw.view.id, app_id:sw.view.app_ids[0],
      queue: j.queue || (sw.view.hardware.find(h => h.key === j.target) || {}).queue || null})))));
    let sy = path.match(/^\/api\/tapis\/systems\/([^/]+)$/);
    if (sy) { const x = SYSTEMS.find(y => y.id === sy[1]); return later(x ? json(200, x) : json(404, {detail:'no such system'})); }
    if (path === '/api/campaigns' && (opts.method || 'GET') === 'POST') {
      if (!body.storage || !body.storage.path) return later(json(400, {detail:'storage.path is required'}));
      const sw = launched(body); SWEEPS.unshift(sw);
      try { const saved = JSON.parse(sessionStorage.getItem(LKEY) || '[]');
            saved.push({spec: body, created: sw.live.created}); sessionStorage.setItem(LKEY, JSON.stringify(saved)); } catch (e) {}
      return later(json(200, {...sw.view, jobs:sw.jobs}));
    }
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
    if (href === 'profiling.html') return;
    if (!/\.html$/.test(href)) return;
    ev.preventDefault();
    let t = document.getElementById('demoToast');
    t.textContent = 'Only the Profiling page is part of this demo.';
    t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2200);
  });
})();
