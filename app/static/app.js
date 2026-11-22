const $ = id => document.getElementById(id);
let source = 'youtube', timer = null, busy = false, shownCount = 0;
const linkTab = $('link-tab'), uploadTab = $('upload-tab'), liveTab = $('live-tab');
function selectTab(mode) {
  source = mode;
  linkTab.classList.toggle('selected', mode === 'youtube');
  uploadTab.classList.toggle('selected', mode === 'upload');
  liveTab.classList.toggle('selected', mode === 'live');
  $('link-pane').hidden = mode !== 'youtube';
  $('upload-pane').hidden = mode !== 'upload';
  $('live-pane').hidden = mode !== 'live';
  $('error').hidden = true;
}
linkTab.onclick = () => selectTab('youtube');
uploadTab.onclick = () => selectTab('upload');
liveTab.onclick = () => selectTab('live');
function error(message) {
  $('status').hidden = true;
  $('error').textContent = message;
  $('error').hidden = false;
  setBusy(false);
}
function setBusy(value) {
  busy = value;
  $('create-btn').disabled = value;
  $('create-btn').textContent = value ? '✦ Processing video…' : '✦ Generate my clips →';
}
function progress(percent, label) {
  $('status').hidden = false;
  $('bar').style.width = Math.max(0, Math.min(100, percent)) + '%';
  $('percent').textContent = percent + '%';
  $('status-text').textContent = label;
}
async function unpack(response) {
  let data;
  try {data = await response.json()} catch {throw new Error('Unexpected server response.')}
  if (!response.ok) throw new Error(data.detail || 'Something went wrong.');
  return data;
}
function display(jobId, clips) {
  const grid = $('clips');
  grid.replaceChildren();
  clips.forEach((clip, index) => {
    const base = '/api/jobs/' + encodeURIComponent(jobId) + '/download/';
    const url = base + encodeURIComponent(clip.name);
    const card = document.createElement('div');card.className = 'clip-card';
    const video = document.createElement('video');video.src = url;video.controls = true;video.playsInline = true;video.preload = 'none';
    const meta = document.createElement('div');meta.className = 'meta';
    const title = document.createElement('strong');title.textContent = 'Highlight ' + String(index + 1).padStart(2, '0');
    const detail = document.createElement('span');
    detail.textContent = Math.floor(clip.start / 60) + ':' + String(Math.floor(clip.start % 60)).padStart(2, '0') + ' • ' + clip.duration + 's';
    meta.append(title, detail);
    const download = document.createElement('a');download.href = url;download.download = clip.name;download.textContent = '↓ Download MP4';
    card.append(video, meta, download);grid.append(card);
  });
  $('download-all').href = '/api/jobs/' + encodeURIComponent(jobId) + '/download/all_clips.zip';
  $('results').hidden = false;
  if (shownCount <= 1) $('results').scrollIntoView({behavior:'smooth'});
}
async function poll(jobId) {
  try {
    const job = await unpack(await fetch('/api/jobs/' + encodeURIComponent(jobId), {cache:'no-store'}));
    const ready = (job.clips || []).length;
    const planned = job.planned_clips || 0;
    const label = job.status === 'done' ? ('All ' + ready + ' available clips ready!') :
      planned && ready ? (ready + ' of ' + planned + ' clips ready — processing continues') : job.message;
    progress(job.progress, label);
    if (job.clips && job.clips.length > shownCount) {
      shownCount = job.clips.length;
      display(jobId, job.clips);
      if (job.status !== 'done') $('download-all').hidden = true;
    }
    if (job.status === 'done') {
      setBusy(false);
      display(jobId,job.clips);
      $('download-all').hidden = false;
      return;
    }
    if (job.status === 'error') throw new Error(job.message);
    timer = setTimeout(() => poll(jobId), 1700);
  } catch (err) {error(err.message);}
}
$('clip-form').onsubmit = async event => {
  event.preventDefault();
  if (busy) return;
  $('error').hidden = true;
  $('results').hidden = true;
  shownCount = 0;
  if (source === 'youtube' && !$('youtube-url').value.trim()) {error('Paste a YouTube link first.');return;}
  if (source === 'live' && !$('live-url').value.trim()) {error('Paste a public live-stream link first.');return;}
  const file = $('video-file').files[0];
  if (source === 'upload' && !file) {error('Choose a video file first.');return;}
  if (source === 'upload' && file.size > 500 * 1024 * 1024) {error('Maximum file size is 500 MB.');return;}
  if (timer) clearTimeout(timer);
  setBusy(true);
  progress(2,source === 'youtube' ? 'Checking YouTube link…' : source === 'live' ? 'Connecting to live stream…' : 'Uploading video…');
  try {
    let response;
    if (source === 'youtube') {
      response = await fetch('/api/jobs/youtube', {
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:$('youtube-url').value.trim(),quality:$('quality').value,analysis_mode:$('analysis-mode').value})
      });
    } else if (source === 'live') {
      response = await fetch('/api/jobs/live', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({url:$('live-url').value.trim(),capture_seconds:Number($('capture-seconds').value),quality:$('quality').value,analysis_mode:$('analysis-mode').value})
      });
    } else {
      const initiated = await unpack(await fetch('/api/jobs/upload/start', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({filename:file.name,size:file.size,quality:$('quality').value,analysis_mode:$('analysis-mode').value})
      }));
      const chunkSize = initiated.chunk_size || 4 * 1024 * 1024;
      let uploaded = 0;
      for (let index = 0; uploaded < file.size; index++) {
        const end = Math.min(file.size, uploaded + chunkSize);
        const body = file.slice(uploaded, end);
        let result;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            result = await unpack(await fetch('/api/jobs/upload/' + encodeURIComponent(initiated.job_id) + '/chunk?index=' + index, {
              method:'PUT', headers:{'Content-Type':'application/octet-stream'}, body
            }));
            break;
          } catch (err) {
            if (attempt === 2) throw new Error('Upload interrupted. Check your connection and retry. ' + err.message);
            await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)));
          }
        }
        if (result.received !== end) throw new Error('Server returned unexpected upload offset. Please retry.');
        uploaded = end;
        progress(Math.round(5 + 20 * uploaded / file.size), 'Uploading video: ' + Math.round(100 * uploaded / file.size) + '%');
      }
      response = await fetch('/api/jobs/upload/' + encodeURIComponent(initiated.job_id) + '/finish', {method:'POST'});
    }
    const job = await unpack(response);
    await poll(job.job_id);
  } catch (err) {error(err.message);}
};