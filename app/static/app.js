const $ = id => document.getElementById(id);
let source = 'youtube', timer = null, busy = false, shownCount = 0;
const linkTab = $('link-tab'), uploadTab = $('upload-tab');
function selectTab(mode) {
  source = mode;
  linkTab.classList.toggle('selected', mode === 'youtube');
  uploadTab.classList.toggle('selected', mode === 'upload');
  $('link-pane').hidden = mode !== 'youtube';
  $('upload-pane').hidden = mode !== 'upload';
  $('error').hidden = true;
}
linkTab.onclick = () => selectTab('youtube');
uploadTab.onclick = () => selectTab('upload');
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
  if (!$('permission').checked) {error('Confirm that you have permission to process the video.');return;}
  if (source === 'youtube' && !$('youtube-url').value.trim()) {error('Paste a YouTube link first.');return;}
  const file = $('video-file').files[0];
  if (source === 'upload' && !file) {error('Choose a video file first.');return;}
  if (source === 'upload' && file.size > 200 * 1024 * 1024) {error('Maximum file size is 200 MB.');return;}
  if (timer) clearTimeout(timer);
  setBusy(true);
  progress(2,source === 'youtube' ? 'Checking YouTube link…' : 'Uploading video…');
  try {
    let response;
    if (source === 'youtube') {
      response = await fetch('/api/jobs/youtube', {
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:$('youtube-url').value.trim(),authorized:true,quality:$('quality').value})
      });
    } else {
      const form = new FormData();form.append('file',file);form.append('authorized','true');form.append('quality',$('quality').value);
      response = await fetch('/api/jobs/upload',{method:'POST',body:form});
    }
    const job = await unpack(response);
    await poll(job.job_id);
  } catch (err) {error(err.message);}
};