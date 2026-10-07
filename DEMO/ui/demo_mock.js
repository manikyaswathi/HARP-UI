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
    {id:'harp-build-swathi', version:'1.0.0', runtime:'SINGULARITY', image:'docker://ghcr.io/manikyaswathi/harp-build:1.0.0',
     app_args:[], notes:{harp:{label:'HARP build', role:'build'}}},
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
  const HWLABEL = h => h === 'cardinal' ? 'cardinal/gpu · 8 cores · 31 GB · GPU' : `${h}/${QUEUE[h] || 'serial'} · 4 cores · 16 GB`;
  const tLabel = t => [t.system_id + (t.queue ? '/' + t.queue : ''), t.cores_per_node ? t.cores_per_node + ' cores' : '',
    t.memory_mb ? Math.round(t.memory_mb / 1024) + ' GB' : '', t.container_args ? 'GPU' : ''].filter(Boolean).join(' · ');
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
        const row = {campaign:name, system:host, hardware:HWLABEL(host), sys_alloc_cores: host === 'cardinal' ? 8 : 4,
                     sys_alloc_mem_mb: host === 'cardinal' ? 32000 : 16000, run_config:`${job.name}.run-${idx}.iteration-${r}`, run_type:runType, ...HW[host]};
        for (const [k, v] of Object.entries(p)) row['run_' + k] = v;
        row.walltime = +(timeFn(p, host) * jitter()).toFixed(5);
        rows.push(row);
      }
      jobs.push(job); idx++;
    }
    const view = {id:name, name, application:appId, app_ids:[appId], created_at:new Date(created).toISOString(),
      repetitions:reps, run_types:[...new Set(sets.map(x => x[0]))], storage:{system_id:'pitzer'}, campaign_dir:`/fs/scratch/PAS2271/swathi/harp_runs/${name}`,
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
      ['test_data', {model:['yolo11s'], epochs:[3], imgsz:[640], batch:[8], device:['auto']}]], 3, yoloTime, ['pitzer','cardinal'], {runningFrom:19, hoursAgo:2})
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
    for (const p of combos) for (const [ti, t] of spec.targets.entries()) {
      jobs.push({name:`${name}-${spec.run_sets[0].run_type}-${String(i).padStart(4,'0')}`, system_id:t.system_id,
        target:'t' + ti, queue:t.queue, cores_per_node:t.cores_per_node, memory_mb:t.memory_mb, run_type:spec.run_sets[0].run_type, combinations:1, status:'NOT_SUBMITTED',
        uuid:null, error:null, submitted_at:null, ended_at:null});
      plan.push({p, t, at: 1500 + i * 1200, run: 6000 + i * 1500, end: 14000 + i * 2500}); i++;
    }
    const view = {id:name, name, application:spec.application, app_ids:[appId], created_at:new Date(created).toISOString(),
      repetitions:spec.repetitions, run_types:[spec.run_sets[0].run_type], storage:spec.storage, campaign_dir:`${spec.storage.path}/${name}`,
      hardware: spec.targets.map((t, ti) => ({key:'t' + ti, system_id:t.system_id, queue:t.queue, app_id:appId,
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
          const row = {campaign:sw.view.name, system:host, hardware:tLabel(pl.t), sys_alloc_cores:pl.t.cores_per_node,
                       sys_alloc_mem_mb:pl.t.memory_mb, run_config:`${j.name}.run-${k}.iteration-${r}`, run_type:j.run_type, ...HWOF(host)};
          for (const [key, v] of Object.entries(pl.p)) row['run_' + key] = v;
          const base = sw.view.app_ids[0].includes('yolo') ? yoloTime(pl.p, gpu ? 'cardinal' : 'pitzer')
                     : eulerTime({method:'pow', n:1000, precision:64, ...pl.p});
          row.walltime = +(base * Math.pow(48 / Math.max(1, pl.t.cores_per_node || 48), 0.35) * jitter()).toFixed(5); sw.rows.push(row);
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
    const cols = ['campaign', 'system', 'hardware', 'sys_alloc_cores', 'sys_alloc_mem_mb'];
    for (const r of rows) for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k);
    if (cols.includes('walltime')) { cols.splice(cols.indexOf('walltime'), 1); cols.push('walltime'); }
    return {app_id:appId, campaigns:mine.map(s => s.view), columns:cols, rows, total_rows:rows.length, missing:[]};
  }
  // What the runner's harp_progress.json would say: runs done so far in each job.
  function runsOf(sw, j, k){
    const total = (j.combinations || 1) * (sw.view.repetitions || 1), at = new Date().toISOString();
    if (j.status === 'FINISHED') return {runs_total: total, progress: {runs_done: total, runs_failed: 0, runs_total: total, current: null, updated_at: j.ended_at}};
    if (j.status === 'FAILED') { const d = Math.floor(total / 2); return {runs_total: total, progress: {runs_done: d, runs_failed: 0, runs_total: total, current: null, updated_at: j.ended_at}}; }
    if (j.status === 'RUNNING') {
      const pl = sw.live && sw.live.plan[k];
      const frac = pl ? Math.min(0.99, Math.max(0, (Date.now() - sw.live.created - pl.run) / (pl.end - pl.run))) : 0.5;
      const d = Math.floor(total * frac);
      return {runs_total: total, progress: {runs_done: d, runs_failed: 0, runs_total: total, current: `${j.name}.run-${k}.iteration-${d}`, updated_at: at}};
    }
    return {runs_total: total, progress: null};
  }
  // ---- the server's plan rules (harp_server/plan.py), mirrored for the demo ----
  function harpApp(a){
    const h = (a.notes && a.notes.harp) || {};
    let params = Array.isArray(h.params) ? h.params : [];
    if (!params.length && h.command) params = [...h.command.matchAll(/\{(\w+)\}/g)].map(m => ({name:m[1], arg:`{${m[1]}}`}));
    return {id:a.id, version:a.version, label:h.label || a.id, role: h.role === 'build' ? 'build' : 'profile', image:a.image || '', runtime:a.runtime || 'SINGULARITY',
      description:a.description || '', command:h.command || '', workdir:h.workdir || '',
      params: params.map(p => ({name:p.name, arg:p.arg || '', kind:p.kind || 'string', default: p.default == null ? '' : String(p.default)}))};
  }
  const isGpuQ = q => /gpu/i.test(`${q.name || ''} ${q.description || ''} ${q.hpc_queue || ''}`);
  const cap = (v, mx) => mx > 0 ? Math.min(v, mx) : v;
  function resolveTarget(sys, qn, cfg, reps, timeout, onQueue, alloc){
    const where = `${sys.id}/${qn || 'default queue'}`, q = (sys.queues || []).find(x => x.name === qn);
    if (!q) throw `${sys.id} has no queue '${qn}'`;
    if ((q.min_nodes || 1) > 1) throw `${where} needs at least ${q.min_nodes} nodes; profiling jobs use 1 node`;
    const cores = cfg.cores || q.max_cores || 1;
    if (q.max_cores > 0 && cores > q.max_cores) throw `${where} allows at most ${q.max_cores} cores per node; asked for ${cores}`;
    const mem = cfg.memory_mb || cap(Math.max(4096, cores * 4096), q.max_memory_mb);
    if (q.max_memory_mb > 0 && mem > q.max_memory_mb) throw `${where} allows at most ${q.max_memory_mb} MB of memory; asked for ${mem}`;
    const mins = cfg.max_minutes || cap(reps * timeout + 10, q.max_minutes);
    if (q.max_minutes > 0 && mins > q.max_minutes) throw `${where} allows at most ${q.max_minutes} minutes; asked for ${mins}`;
    const lim = q.max_jobs_per_user > 0 ? Math.min(q.max_jobs_per_user, 10) : 4;
    return {system_id:sys.id, queue:qn, node_count:1, cores_per_node:cores, memory_mb:mem, max_minutes:mins,
      container_args: isGpuQ(q) ? '--nv' : null, scheduler_options: (alloc || '').trim() || null,
      max_concurrent_jobs: Math.max(1, Math.floor(lim / onQueue))};
  }
  function buildPlan(plan){
    const st = plan.storage || {}, path = (st.path || '').trim();
    const storage_error = !st.system_id ? 'pick a TAPIS system for the results'
      : !path.startsWith('/') ? 'the results folder must be an absolute path, e.g. /fs/scratch/PAS0000/harp_runs' : null;
    const stamp = new Date().toISOString().slice(2, 16).replace(/[-:T]/g, '');
    const sweeps = plan.sweeps.map(s => {
      const name = `${s.app_id} / ${s.name}`;
      try {
        const app = APPS.map(harpApp).find(a => a.id === s.app_id);
        if (!app) throw `app '${s.app_id}' is not one of your TAPIS apps`;
        if (!app.command) throw `${app.label} does not declare its sweep command in its TAPIS notes`;
        if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,30}$/.test(s.name || '')) throw 'sweep name: letters, digits, - _ . (max 31)';
        const params = {};
        for (const p of app.params) {
          const vals = (s.params[p.name] || []).filter(v => v !== '');
          if (!vals.length) throw `give '${p.name}' a value`;
          if ((p.kind === 'int' || p.kind === 'float') && vals.some(v => isNaN(+v))) throw `'${p.name}' needs numbers`;
          params[p.name] = vals.map(v => p.kind === 'int' || p.kind === 'float' ? Number(v) : v);
        }
        if (!s.targets.length) throw 'tick at least one queue';
        const count = {}; s.targets.forEach(t => count[t.system_id + '|' + t.queue] = (count[t.system_id + '|' + t.queue] || 0) + 1);
        const seen = new Set();
        const targets = s.targets.map(t => { const sys = SYSTEMS.find(x => x.id === t.system_id);
          if (!sys) throw `'${t.system_id}' is not one of your TAPIS execution systems`;
          const r = resolveTarget(sys, t.queue, t, s.repetitions, s.timeout_min, count[t.system_id + '|' + t.queue], (plan.allocations || {})[t.system_id]);
          const sig = [r.system_id, r.queue, r.cores_per_node, r.memory_mb, r.max_minutes].join('|');
          if (seen.has(sig)) throw `${t.system_id}/${t.queue} has two identical configurations`; seen.add(sig);
          return {...r, app_id:app.id, app_version:app.version}; });
        const slug = app.label.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 20) || 'app';
        const spec = {name:`${slug}-${s.name}-${stamp}`.slice(0, 64), application:slug, command:app.command, workdir:app.workdir,
          repetitions:s.repetitions, run_timeout_sec:s.timeout_min * 60, combos_per_job:1, distribution:'replicate',
          run_sets:[{run_type:s.run_type, parameters:params}], targets, storage:{system_id:st.system_id, path}};
        const n = cartesian(params).length;
        return {name, ok:true, targets, spec, sweep_name:spec.name,
                summary:{combinations:n, jobs:n * targets.length, total_runs:n * targets.length * s.repetitions}};
      } catch (e) { return {name, ok:false, error:String(e), targets:[]}; }
    });
    return {ok: !storage_error && sweeps.every(x => x.ok), storage_error, sweeps};
  }
  // ---- build phase (harp_server/build.py), mirrored; the models are made up ----
  const MIN_ROWS = {SD:2, FS:4, test_data:1};
  function standardizeRows(appId, ids){
    const p = profileOf(appId), names = new Set(SWEEPS.filter(x => ids.includes(x.view.id)).map(x => x.view.name));
    const rows = p.rows.filter(r => names.has(r.campaign));
    const rep = {rows_in:rows.length, dropped_rows:{}, dropped_columns:{}, constant_columns:[], by_run_type:{SD:0, FS:0, test_data:0}, problems:[], minimum:MIN_ROWS};
    const kept = rows.filter(r => { const ok = r.run_type in rep.by_run_type && +r.walltime > 0;
      if (!ok) { const why = r.run_type in rep.by_run_type ? 'no walltime' : 'run type is not SD, FS or test_data'; rep.dropped_rows[why] = (rep.dropped_rows[why] || 0) + 1; } return ok; });
    const feats = p.columns.filter(c => !['campaign','system','hardware','run_config','run_type','walltime'].includes(c) && /^(run|sys)_/.test(c));
    const usable = feats.filter(c => { const miss = kept.filter(r => r[c] === undefined || r[c] === '').length;
      if (kept.length && miss) { rep.dropped_columns[c] = miss === kept.length ? "not recorded by these sweeps" : `missing in ${miss} of ${kept.length} rows`; return false; } return true; });
    for (const r of kept) rep.by_run_type[r.run_type]++;
    rep.constant_columns = usable.filter(c => !['sys_name','sys_processor'].includes(c) && new Set(kept.map(r => String(r[c]))).size <= 1);
    rep.features = usable.filter(c => !['sys_name','sys_processor'].includes(c));
    rep.varied_features = rep.features.filter(c => !rep.constant_columns.includes(c));
    for (const [t, n] of Object.entries(MIN_ROWS)) if (rep.by_run_type[t] < n) rep.problems.push(`needs at least ${n} ${t} row${n > 1 ? 's' : ''}; has ${rep.by_run_type[t]}`);
    if (!rep.varied_features.length) rep.problems.push('nothing varies between runs, so there is nothing to learn from');
    rep.ready = !rep.problems.length; rep.rows_out = kept.length; rep.unreadable = [];
    const header = ['run_config', 'run_type', ...usable, 'walltime'];
    return {header, table: kept.map(r => Object.fromEntries(header.map(c => [c, r[c]]))), rep};
  }
  const BUILDS = [];
  function fakeMetrics(seedN){
    let k = seedN; const rr = () => (k = (k * 16807) % 2147483647) / 2147483647, out = [];
    for (const mod of ['LR', 'NN', 'DTR']) for (const ds of ['SD', 'SD+25FS', 'SD+50FS', 'SD+75FS']) {
      const base = {LR:38, NN:22, DTR:14}[mod] * {SD:1.6, 'SD+25FS':1.15, 'SD+50FS':1, 'SD+75FS':0.9}[ds] * (0.85 + rr() * 0.3);
      const adj = 1 + Math.round(base * 0.8) / 100, upp = 35 + rr() * 25;
      out.push({DataSet:ds, RegModel:mod, ADJ_FACTOR:1, MAPE:+base.toFixed(2), UPP:+upp.toFixed(1), MAE:+(base / 30).toFixed(3), MODEL_NAME:`${mod}_${ds}_no`});
      out.push({DataSet:ds, RegModel:mod, ADJ_FACTOR:adj, MAPE:+(base * 1.25).toFixed(2), UPP:+(upp / 4).toFixed(1), MAE:+(base / 24).toFixed(3), MODEL_NAME:`${mod}_${ds}_yes`});
    }
    return out;
  }
  function buildNow(b){
    const t = Date.now() - b.t0, steps = [['preprocess', 2500], ['train', 9000], ['save', 11000]];
    if (b.job) b.job.status = t >= 11000 ? 'FINISHED' : t >= 4000 ? 'RUNNING' : t >= 1500 ? 'QUEUED' : 'PENDING';
    if (b.job && t >= 11000 && !b.job.ended_at) b.job.ended_at = new Date().toISOString();
    let prev = 0;
    for (const [k, end] of steps) { b.steps[k] = t >= end ? 'done' : t >= prev ? 'running' : 'waiting'; if (t >= prev && t < end) b.step = k; prev = end; }
    if (t >= 11000 && b.status !== 'DONE') {
      b.status = 'DONE'; b.ended_at = new Date().toISOString();
      b.metrics = fakeMetrics(b.seed).filter(m => b.models.includes(m.RegModel) && b.training_sets.includes(m.DataSet));
      b.best = [...b.metrics].sort((x, y) => x.MAPE - y.MAPE)[0];
      b.files = [b.dataset_file, 'pipeline_config.json', 'full_dataset_pca.csv', 'model_commons.csv', ...b.metrics.map(m => 'models/' + m.MODEL_NAME + (m.RegModel === 'NN' ? '.h5' : '.pkl'))];
    } else if (b.status !== 'DONE') b.status = 'RUNNING';
    return b;
  }
  function newBuild(appId, body, rep, t0){
      const app = APPS.map(harpApp).find(a => a.id === appId) || {label:appId};
      const name = app.label.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
      const stamp = new Date().toISOString().slice(0, 19).replace(/[-T:]/g, '_');
      const b = {id:'b' + (BUILDS.length + 1), t0, seed:7 + BUILDS.length * 13, app_id:appId, name, created_at:new Date(t0).toISOString(), ended_at:null,
        sweeps:body.sweeps, storage:body.storage, dest:`${body.storage.path.replace(/\/$/, '')}/builds/${name}_${stamp}`, dataset_file:`${name}_${stamp}.csv`,
        status:'QUEUED', step:'preprocess', steps:{pool:'done', standardize:'done', preprocess:'waiting', train:'waiting', save:'waiting'},
        metrics:[], best:null, error:null, report:rep, files:[], log:[],
        models: body.models || ['LR','NN','DTR'], training_sets: body.training_sets || ['SD','SD+25FS','SD+50FS','SD+75FS'], where:{mode:'local'}, job:null};
      const ro = body.run_on || {mode:'local'};
      if (ro.mode === 'tapis') {
        b.where = {mode:'tapis', system_id:ro.system_id, queue:ro.queue, cores_per_node:4, memory_mb:16384, max_minutes:60};
        b.job = {uuid:`${(uuidN++).toString(16)}b1d-5e2f-4a3b-9c00-${String(BUILDS.length).padStart(12,'0')}-007`, name:`harp-build-${name}-${b.id}`,
                 status:'PENDING', system_id:ro.system_id, queue:ro.queue, submitted_at:new Date().toISOString(), ended_at:null};
      }
      BUILDS.unshift(b); return b;
  }
  // one finished build, so HARP Estimate has models to pick from
  { const e = SWEEPS.find(x => x.view.app_ids.includes('harp-sweep-euler-swathi'));
    newBuild('harp-sweep-euler-swathi', {sweeps:[e.view.id], storage:{system_id:'pitzer', path:'/fs/scratch/PAS2271/swathi/harp_models'},
      run_on:{mode:'tapis', system_id:'pitzer', queue:'serial'}}, standardizeRows('harp-sweep-euler-swathi', [e.view.id]).rep, Date.now() - 3 * 3600e3); }
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
    if (path === '/api/tapis/apps') return later(json(200, APPS.map(harpApp)));
    SWEEPS.forEach(advance);
    if (path === '/api/tapis/systems') return later(json(200, [...SYSTEMS.map(({queues, ...x}) => x), ...STORAGE_ONLY]));
    if (path === '/api/tapis/exec-systems') return later(json(200, SYSTEMS.map(x => ({...x, queues: x.queues.map(q => ({...q, gpu: isGpuQ(q)}))}))));
    if (path === '/api/plan/preview' || path === '/api/plan/submit') {
      if (!body.sweeps || !body.sweeps.length) return later(json(400, {detail:'the plan has no sweeps'}));
      const res = buildPlan(body);
      if (path === '/api/plan/preview') return later(json(200, res));
      if (res.storage_error) return later(json(400, {detail:res.storage_error}));
      const bad = res.sweeps.find(x => !x.ok); if (bad) return later(json(400, {detail:`sweep ${bad.name}: ${bad.error}`}));
      const made = res.sweeps.map((x, i) => { const sw = launched(x.spec, Date.now() + i * 4000); SWEEPS.unshift(sw);
        try { const saved = JSON.parse(sessionStorage.getItem(LKEY) || '[]'); saved.push({spec:x.spec, created:sw.live.created});
              sessionStorage.setItem(LKEY, JSON.stringify(saved)); } catch (e) {}
        return {...sw.view}; });
      return later(json(200, made));
    }
    if (path === '/api/jobs') return later(json(200, [...BUILDS.filter(b => b.job).map(b => { buildNow(b); return {kind:'build', name:b.job.name,
        sweep:`build ${b.name}`, sweep_id:null, build_id:b.id, app_id:b.app_id, system_id:b.job.system_id, queue:b.job.queue,
        cores_per_node:4, memory_mb:16384, run_type:'build', combinations:0, runs_total:0, progress:null, status:b.job.status,
        uuid:b.job.uuid, error:null, submitted_at:b.job.submitted_at, ended_at:b.job.ended_at}; }),
      ...SWEEPS.flatMap(sw => sw.jobs.map((j, k) => ({...j, ...runsOf(sw, j, k), kind:'profiling',
      sweep:sw.view.name, sweep_id:sw.view.id, app_id:sw.view.app_ids[0],
      queue: j.queue || (sw.view.hardware.find(h => h.key === j.target) || {}).queue || null,
      cores_per_node: j.cores_per_node || (sw.view.hardware.find(h => h.key === j.target) || {}).cores_per_node || null,
      memory_mb: j.memory_mb || (sw.view.hardware.find(h => h.key === j.target) || {}).memory_mb || null})))]));
    if (path === '/api/campaigns') return Promise.resolve(json(200, SWEEPS.map(s => s.view)));
    let c = path.match(/^\/api\/campaigns\/([^/]+)\/cancel$/);
    if (c) {
      const sw = SWEEPS.find(x => x.view.id === c[1]);
      if (!sw) return Promise.resolve(json(404, {detail:'campaign not found'}));
      const now = new Date().toISOString();
      for (const j of sw.jobs) if (!['FINISHED','FAILED'].includes(j.status)) Object.assign(j, {status:'CANCELLED', ended_at:now});
      sw.view.status = 'CANCELLED'; sw.view.events.push({at:now, message:'cancelled by user'}); recount(sw);
      return later(json(200, sw.view));
    }
    let td = path.match(/^\/api\/apps\/(.+)\/training-data(\.csv\?sweeps=(.*))?$/);
    if (td) {
      const ids = td[2] ? td[3].split(',').filter(Boolean) : (body.sweeps || []);
      if (!ids.length) return later(json(400, {detail:'pick at least one sweep'}));
      const {header, table, rep} = standardizeRows(td[1], ids);
      if (td[2]) return Promise.resolve(new Response(toCSV(header, table), {status:200, headers:{'Content-Type':'text/csv'}}));
      return later(json(200, {columns:header, sample:table.slice(0, 8), ...rep}));
    }
    let bm = path.match(/^\/api\/apps\/(.+)\/builds$/);
    if (bm) {
      const {rep} = standardizeRows(bm[1], body.sweeps || []);
      if (!rep.ready) return later(json(400, {detail:'not enough data to build: ' + rep.problems.join('; ')}));
      return later(json(200, buildNow(newBuild(bm[1], body, rep, Date.now()))));
    }
    if (path === '/api/build-options') return later(json(200, {models:{LR:'Linear regression', NN:'Neural network', DTR:'Decision tree'},
      training_sets:{SD:'SD only', 'SD+25FS':'SD + 25% of FS', 'SD+50FS':'SD + 50% of FS', 'SD+75FS':'SD + 75% of FS'},
      build_app:{id:'harp-build-swathi', version:'1.0.0', image:'docker://ghcr.io/manikyaswathi/harp-build:1.0.0'}}));
    if (path.startsWith('/api/builds')) { const q = new URLSearchParams(path.split('?')[1] || '').get('app_id');
      return later(json(200, BUILDS.filter(b => !q || b.app_id === q).map(buildNow))); }
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
