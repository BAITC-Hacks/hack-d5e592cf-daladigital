const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {view:'overview', source:'upload', provider:'meet', meetings:[], current:null, poll:null, detailRequest:0,
  stream:null, displayStream:null, audioContext:null, meterTimer:null, recorder:null, startedAt:0,
  uploadChain:Promise.resolve(), uploadError:null, recordingId:null, gaps:[], silentSince:null, lastSignal:0,
  starting:false, stopPromise:null, recorderStopped:null, localChunks:[], recordingMeta:null, recordingUrl:null, captureClosed:false};
const labels = {recording:'Идёт запись',queued:'В очереди',processing:'Обработка',review:'Нужна проверка',approved:'Утверждён',error:'Ошибка'};
const kinds = {action:'Поручение',decision:'Решение',initiative:'Инициатива',question:'Открытый вопрос',risk:'Риск'};
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = n => `${String(Math.floor(n/60)).padStart(2,'0')}:${String(Math.floor(n%60)).padStart(2,'0')}`;
function flash(message) { const box=$('#flash'); box.textContent=message; box.hidden=false; clearTimeout(flash.timer); flash.timer=setTimeout(()=>box.hidden=true,5500); }
async function api(path, options={}) { const response=await fetch(path,options); if(!response.ok) {let message=`Ошибка ${response.status}`;try {const body=await response.json();message=typeof body.detail==='string'?body.detail:message;}catch{} const error=new Error(message);error.status=response.status;throw error;} return response.json(); }
function json(body) {return {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};}
function show(view) {state.view=view;if(view!=='detail'){clearInterval(state.poll);state.detailRequest++;} $$('.view').forEach(el=>el.hidden=el.id!==view); $$('.nav').forEach(el=>el.classList.toggle('active',el.dataset.view===view)); $('#page-title').textContent={overview:'Обзор',new:'Новое совещание',meetings:'Протоколы',dashboard:'Поручения',detail:'Протокол'}[view];if(view==='dashboard') loadDashboard();if(view==='meetings'||view==='overview') loadMeetings();window.scrollTo(0,0);}
function setSource(source) {if(state.stream||state.recordingId){show('new');flash('Сначала завершите текущую запись или проверку звука');return;}state.source=source;$$('.source-tabs button').forEach(el=>el.classList.toggle('selected',el.dataset.tab===source));$$('.mode-fields').forEach(el=>el.hidden=el.id!==`${source}-fields`);$('#submit-meeting').textContent=source==='upload'?'Загрузить и обработать →':'Проверить звук →';show('new');}
function validateUrl(url,provider) {try {const u=new URL(url);if(u.protocol!=='https:')return false;const h=u.hostname.toLowerCase();return provider==='meet'?h==='meet.google.com':provider==='zoom'?(h==='zoom.us'||h.endsWith('.zoom.us')):['teams.microsoft.com','teams.live.com'].includes(h);}catch{return false;}}
async function loadHealth() {const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),8000);try {const h=await api('/api/health',{signal:controller.signal});const ready=h.ffmpeg&&h.asr_package&&h.asr_model&&h.diarize_package&&h.diarize_model&&h.ollama_executable;$('#system-pill').textContent=ready?'Локальные компоненты установлены':'Нужна настройка моделей';$('#system-pill').classList.toggle('warn',!ready);}catch(e){$('#system-pill').textContent=e.name==='AbortError'?'Проверка заняла больше времени':'Сервер недоступен';$('#system-pill').classList.add('warn');}finally{clearTimeout(timer);}}
async function loadMeetings() {try {state.meetings=await api('/api/meetings');renderList('#recent-list',state.meetings.slice(0,5));renderList('#all-list',state.meetings);}catch(e){flash(e.message);}}
function renderList(selector,meetings) {$(selector).innerHTML=meetings.length?meetings.map(m=>`<div class="list-row"><div><b>${esc(m.title)}</b><small>${esc(m.meeting_date)} · ${m.source==='platform'?esc((m.provider||'').toUpperCase()):m.source==='room'?'Переговорная':'Загрузка'} · ${m.items.length} элементов</small></div><div><span class="state ${esc(m.state)}">${labels[m.state]||esc(m.state)}</span> <button class="link" data-open="${esc(m.id)}">Открыть →</button></div></div>`).join(''):'<div class="empty">Совещаний пока нет. Создайте первый протокол.</div>';}
async function loadDashboard() {try {const data=await api('/api/dashboard');const names={overdue:'Просрочено',soon:'Скоро срок',on_track:'В работе',no_date:'Без срока',done:'Выполнено'};$('#stats').innerHTML=Object.entries(names).map(([key,label])=>`<div class="stat"><strong>${data.counts[key]||0}</strong><span>${label}</span></div>`).join('');$('#action-list').innerHTML=data.actions.length?data.actions.map(item=>`<div class="list-row"><div><b>${esc(item.title)}</b><small>${esc(item.meeting_title)} · ${esc(item.owner||'Ответственный не указан')} · ${esc(item.due_date||item.due_text||'Без срока')}</small></div><div><span class="state ${item.deadline_state==='overdue'?'error':''}">${esc(names[item.deadline_state]||item.deadline_state)}</span> <button class="link" data-open="${esc(item.meeting_id)}">Открыть →</button></div></div>`).join(''):'<div class="empty">Утверждённых поручений пока нет.</div>';}catch(e){flash(e.message);}}
async function openDetail(id) {
  clearInterval(state.poll);const request=++state.detailRequest;let busy=false,polling=true;
  show('detail');$('#detail-content').hidden=true;$('#detail-stage').textContent='Загрузка протокола…';
  if(state.current?.id!==id){state.current=null;$('#detail-title').textContent='Протокол';$('#detail-meta').textContent='';$('#detail-sub').textContent='СОВЕЩАНИЕ';}
  $('#detail-state').textContent='Загрузка';$('#detail-state').className='state';
  const refresh=async()=>{
    if(busy||request!==state.detailRequest||state.view!=='detail')return;
    busy=true;const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),10000);
    try{
      const meeting=await api(`/api/meetings/${id}`,{signal:controller.signal});
      if(request!==state.detailRequest||state.view!=='detail')return;
      state.current=meeting;renderDetail(meeting);polling=['queued','processing','recording'].includes(meeting.state);
      if(!polling)clearInterval(state.poll);
    }catch(error){
      if(request!==state.detailRequest||state.view!=='detail')return;
      const missing=error.status===404;
      if(missing){polling=false;clearInterval(state.poll);state.current=null;}
      $('#detail-content').hidden=true;$('#detail-state').textContent=missing?'Не найдено':'Нет связи';$('#detail-state').className='state error';
      $('#detail-stage').innerHTML=missing
        ?'<b>Совещание не найдено в текущем хранилище. Вернитесь к списку и обновите страницу.</b><p><button class="link" data-go="meetings">← К списку протоколов</button></p>'
        :`<b>Связь с сервером потеряна.</b><p>Пробуем восстановить соединение. Данные протокола появятся после подключения.</p><button class="link" data-open="${esc(id)}">Обновить сейчас</button>`;
    }finally{clearTimeout(timeout);busy=false;}
  };
  await refresh();
  if(polling&&request===state.detailRequest&&state.view==='detail')state.poll=setInterval(refresh,3000);
}
function sourceLink(id,segments){const segment=segments.find(s=>String(s.id)===String(id));const start=Number(segment?.start);return segment&&Number.isFinite(start)?`<button type="button" class="time-link" data-seek="${start}" title="Прослушать источник №${esc(id)}">№${esc(id)} · ${fmtTime(start)}</button>`:`№${esc(id)}`;}
function renderDetail(m) {const canExport=['review','approved'].includes(m.state)||(m.state==='error'&&Boolean(m.summary?.trim())&&m.segments.length>0);$('#detail-title').textContent=m.title;$('#detail-sub').textContent=m.provider?`ОНЛАЙН · ${m.provider.toUpperCase()}`:m.source==='room'?'ПЕРЕГОВОРНАЯ':'ЗАГРУЖЕННАЯ ЗАПИСЬ';$('#detail-meta').textContent=`${m.meeting_date} · ${m.participants.join(', ')||'Участники не указаны'}`;$('#detail-state').textContent=labels[m.state]||m.state;$('#detail-state').className=`state ${m.state}`;$('#detail-stage').innerHTML=`<b>${esc(m.stage)}</b>${m.error?`<p>${esc(m.error)}</p>`:''}`;$('#detail-content').hidden=!['review','approved','error'].includes(m.state);$('#approve').hidden=m.state!=='review';$('#export-pdf').hidden=$('#export-docx').hidden=!canExport;$('#retry').hidden=m.state!=='error';$('#export-pdf').href=`/api/meetings/${m.id}/export?format=pdf`;$('#export-docx').href=`/api/meetings/${m.id}/export?format=docx`;$('#detail-summary').textContent=m.summary||'Саммари ещё не сформировано.';$('#item-count').textContent=`(${m.items.length})`;$('#detail-items').innerHTML=m.items.length?m.items.map(item=>`<div class="item"><div class="item-top"><span class="tag ${esc(item.kind)}">${kinds[item.kind]||esc(item.kind)}</span><span class="state">${esc(item.status)}</span></div><h3>${esc(item.title)}</h3><p>${esc(item.description||'')}</p><small>Ответственный: ${esc(item.owner||'требует уточнения')} · Срок: ${esc(item.due_text||'требует уточнения')}</small><small>Источники: ${item.source_segment_ids.map(id=>sourceLink(id,m.segments)).join(' ')}${item.review_note?' · Проверить: '+esc(item.review_note):''}</small>${m.state==='review'?`<div class="item-controls"><button data-edit-item="${esc(item.id)}">Изменить данные</button></div>`:m.state==='approved'&&item.kind==='action'?`<div class="item-controls"><button data-done="${esc(item.id)}">${item.status==='done'?'Вернуть в работу':'Отметить выполненным'}</button></div>`:''}</div>`).join(''):'<div class="empty">Элементов пока нет. При проверке добавьте пропущенное вручную через API.</div>';
  $('#protocol-download').hidden=!canExport;
  $('#protocol-download-note').textContent=m.state==='approved'?'Утверждённая версия протокола.':'Документ доступен до утверждения и будет помечен «Черновик».';
  for(const format of ['pdf','docx']){
    $('#download-'+format).href=`/api/meetings/${m.id}/export?format=${format}`;
    $('#download-full-'+format).href=`/api/meetings/${m.id}/export?format=${format}&include_transcript=true`;
  }
  $('#source-audio').src=`/api/meetings/${m.id}/media`;$('#detail-gaps').innerHTML=m.gaps.map(g=>`<div class="gap">Проверьте ${fmtTime(g.start)}–${fmtTime(g.end)}: ${esc(g.reason)}</div>`).join('');$('#detail-segments').innerHTML=m.segments.length?m.segments.map(s=>`<div class="turn"><div class="turn-meta"><button class="time-link" data-seek="${s.start}">${fmtTime(s.start)}</button><b>${esc(m.speakers[s.speaker_id]||s.speaker_id)}</b>${m.state==='review'?`<button class="speaker-edit" data-speaker="${esc(s.speaker_id)}">Переименовать</button>`:''}</div><p>${esc(s.text)}</p></div>`).join(''):'<div class="empty">Транскрипт ещё не готов.</div>';
  $('#edit-summary').hidden=$('#add-item').hidden=m.state!=='review';
  reanalyzeButton.hidden=!m.segments.length||['queued','processing','recording'].includes(m.state);
  reanalyzeButton.disabled=reanalyzeButton.hidden;
  const pending=Object.entries(m.speakers||{}).filter(([,name])=>name.endsWith('(проверьте)'));
  $('#approve').disabled=pending.length>0;$('#approve').title=pending.length?'Сначала подтвердите имена говорящих':'';
  speakerReview.hidden=m.state!=='review'||!Object.keys(m.speakers||{}).length;
  speakerReview.innerHTML=`<div class="speaker-review-heading"><b>Участники</b>${pending.length?'<button class="link" id="confirm-names">Проверить имена →</button>':'<span class="speaker-confirmed">Имена подтверждены</span>'}</div>${pending.length?'<p>Сверьте голоса с записью и подтвердите предложенные имена перед утверждением.</p>':''}${Object.entries(m.speakers||{}).map(([id,name])=>{const segment=m.segments.find(s=>s.speaker_id===id);return `<div class="speaker-row"><span>${esc(name.replace(/\s*\(проверьте\)$/,''))}${name.endsWith('(проверьте)')?' <small>проверить</small>':''}</span><span>${segment?`<button class="time-link" data-seek="${segment.start}">▶ ${fmtTime(segment.start)}</button> `:''}<button class="speaker-edit" data-speaker="${esc(id)}">${name.endsWith('(проверьте)')?'Подтвердить':'Изменить'}</button></span></div>`;}).join('')}`;
  speakerReview.querySelector('#confirm-names')?.addEventListener('click',confirmSpeakerNames);
}
function userActor(){return sessionStorage.getItem('protocol-actor')||'Секретарь';}
async function closeCapture() {
  state.captureClosed=true;clearInterval(state.meterTimer);state.meterTimer=null;
  state.stream?.getTracks().forEach(t=>t.stop());state.displayStream?.getTracks().forEach(t=>t.stop());
  state.stream=null;state.displayStream=null;
  const context=state.audioContext;state.audioContext=null;
  if(context&&context.state!=='closed')await context.close().catch(()=>{});
  $('#meter-fill').style.width='0%';
}
async function cancelCapture() {
  if(state.starting||state.recordingId)return;
  await closeCapture();$('#recorder').hidden=true;$('#meeting-form').hidden=false;
  $('#record-timer').textContent='00:00';
}
async function prepareCapture() {
  if(state.stream||state.recordingId)return;
  if(state.source==='platform'&&!validateUrl($('#meeting-url').value,state.provider))throw new Error('Проверьте ссылку на выбранную платформу');
  if(!navigator.mediaDevices||!window.MediaRecorder)throw new Error('Нужен Chrome и защищённый контекст localhost');
  let stream,display=null;
  if(state.source==='platform'){
    display=await navigator.mediaDevices.getDisplayMedia({video:true,audio:true,preferCurrentTab:false,selfBrowserSurface:'exclude',systemAudio:'exclude'});
    const audio=display.getAudioTracks();
    if(!audio.length){display.getTracks().forEach(t=>t.stop());throw new Error('Звук вкладки не выбран. В Chrome включите «Передавать звук вкладки».');}
    stream=new MediaStream(audio);
  }else stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
  state.stream=stream;state.displayStream=display;state.captureClosed=false;state.startedAt=0;
  state.localChunks=[];state.gaps=[];state.silentSince=null;state.lastSignal=0;state.recorder=null;
  if(state.recordingUrl)URL.revokeObjectURL(state.recordingUrl);state.recordingUrl=null;
  $('#meeting-form').hidden=true;$('#recorder').hidden=false;$('#record-title').textContent=$('#meeting-title').value;
  $('#record-status').textContent='Ожидание реального аудиосигнала…';$('#record-timer').textContent='00:00';
  $('#begin-recording').disabled=true;$('#begin-recording').hidden=false;$('#stop-recording').hidden=true;
  $('#stop-recording').disabled=false;$('#stop-recording').textContent='Остановить и обработать';
  $('#cancel-capture').hidden=false;$('#cancel-capture').disabled=false;$('#download-recording').hidden=true;
  $('#record-note').textContent='Проверьте звук и начните запись. Пока запись не началась, можно отменить подключение.';
  try{
    const context=new AudioContext();state.audioContext=context;await context.resume();
    const analyser=context.createAnalyser();analyser.fftSize=2048;context.createMediaStreamSource(stream).connect(analyser);
    const samples=new Float32Array(analyser.fftSize);
    state.meterTimer=setInterval(()=>{
      analyser.getFloatTimeDomainData(samples);const rms=Math.sqrt(samples.reduce((sum,v)=>sum+v*v,0)/samples.length);const active=rms>0.008;
      $('#meter-fill').style.width=`${Math.min(100,rms*650)}%`;
      if(active){
        state.lastSignal=Date.now();$('#record-status').textContent='Звук поступает';
        if(state.silentSince!==null){const end=(Date.now()-state.startedAt)/1000;if(state.startedAt&&end-state.silentSince>10)state.gaps.push({start:state.silentSince,end,reason:'Нет звукового сигнала'});state.silentSince=null;}
      }else{
        $('#record-status').textContent='Нет сигнала — проверьте микрофон или вкладку';
        if(state.startedAt&&state.silentSince===null)state.silentSince=(Date.now()-state.startedAt)/1000;
      }
      $('#begin-recording').disabled=state.starting||!state.lastSignal||Date.now()-state.lastSignal>3500;
      if(state.startedAt)$('#record-timer').textContent=fmtTime((Date.now()-state.startedAt)/1000);
    },180);
    (display||stream).getTracks().forEach(track=>track.addEventListener('ended',()=>{
      if(state.captureClosed)return;
      if(state.recordingId){flash('Источник звука отключён. Сохраняю запись.');stopRecording('Захват вкладки или микрофона прерван').catch(e=>flash(e.message));}
      else if(!state.starting){cancelCapture();flash('Источник звука отключён до начала записи. Подключите его заново.');}
    }));
  }catch(e){await closeCapture();$('#recorder').hidden=true;$('#meeting-form').hidden=false;throw e;}
}
async function beginRecording() {
  if(state.starting||state.recordingId)return;
  if(!state.stream?.getAudioTracks().some(track=>track.readyState==='live'))throw new Error('Источник звука отключён. Подключите его заново.');
  state.starting=true;$('#begin-recording').disabled=true;$('#cancel-capture').disabled=true;
  try{
    const body={title:$('#meeting-title').value.trim(),meeting_date:$('#meeting-date').value,participants:$('#meeting-participants').value,source:state.source,provider:state.source==='platform'?state.provider:null,meeting_url:state.source==='platform'?$('#meeting-url').value.trim():null,consent_confirmed:$('#consent').checked};
    const type=MediaRecorder.isTypeSupported('audio/webm;codecs=opus')?'audio/webm;codecs=opus':'audio/webm';
    const recorder=new MediaRecorder(state.stream,{mimeType:type});
    const m=await api('/api/meetings/live',{method:'POST',...json(body)});
    state.recordingId=m.id;state.recordingMeta=body;state.uploadChain=Promise.resolve();state.uploadError=null;state.recorder=recorder;
    state.recorderStopped=new Promise(resolve=>recorder.addEventListener('stop',resolve,{once:true}));
    recorder.ondataavailable=e=>{
      if(!e.data.size)return;
      state.localChunks.push(e.data);
      state.uploadChain=state.uploadChain.then(async()=>{
        const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),20000);
        try{return await api(`/api/meetings/${m.id}/chunks`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:e.data,signal:controller.signal});}
        finally{clearTimeout(timeout);}
      });
      state.uploadChain.catch(error=>{if(!state.uploadError)flash('Связь с сервером прервана. Звук сохранён в этой вкладке.');state.uploadError=error;});
    };
    recorder.onerror=()=>{flash('Браузер остановил запись. Сохраняю полученный звук.');stopRecording('Ошибка браузерной записи').catch(e=>flash(e.message));};
    if(!state.stream?.getAudioTracks().some(track=>track.readyState==='live'))throw new Error('Источник отключён во время запуска. Подключите звук заново.');
    recorder.start(2000);state.startedAt=Date.now();
    $('#begin-recording').hidden=true;$('#stop-recording').hidden=false;$('#cancel-capture').hidden=true;
    $('#record-note').textContent='Записывается только звук. Не закрывайте эту вкладку до сохранения.';
  }catch(e){
    if(!state.startedAt){state.recordingId=null;state.recorder=null;await closeCapture();$('#recorder').hidden=true;$('#meeting-form').hidden=false;}
    throw e;
  }finally{state.starting=false;$('#cancel-capture').disabled=false;}
}
async function finishCapture(reason) {
  $('#stop-recording').disabled=true;$('#stop-recording').textContent='Сохранение…';
  try{
    const recorder=state.recorder;
    if(recorder&&recorder.state!=='inactive')recorder.stop();
    if(state.startedAt&&state.recorderStopped)await state.recorderStopped;
    if(!state.captureClosed){
      const end=state.startedAt?(Date.now()-state.startedAt)/1000:0;
      if(state.silentSince!==null){state.gaps.push({start:state.silentSince,end,reason:reason||'Нет звукового сигнала'});state.silentSince=null;}
      if(reason)state.gaps.push({start:end,end,reason});
      await closeCapture();$('#record-status').textContent='Запись остановлена. Сохраняем звук…';
    }
    if(state.localChunks.length&&!state.recordingUrl){
      state.recordingUrl=URL.createObjectURL(new Blob(state.localChunks,{type:'audio/webm'}));
      $('#download-recording').href=state.recordingUrl;$('#download-recording').hidden=false;
    }
    await state.uploadChain.catch(()=>{});
    let id=state.recordingId;
    if(state.uploadError){
      // A failed response may have already appended bytes: recover using the complete local file.
      const data=new FormData();data.set('file',new Blob(state.localChunks,{type:'audio/webm'}),'meeting.webm');
      data.set('title',state.recordingMeta.title);data.set('meeting_date',state.recordingMeta.meeting_date);data.set('participants',state.recordingMeta.participants);
      const recovered=await api('/api/meetings/upload',{method:'POST',body:data});id=recovered.id;
    }else{
      const current=await api(`/api/meetings/${id}`);
      if(current.state==='recording')await api(`/api/meetings/${id}/finish`,{method:'POST',...json({gaps:state.gaps})});
    }
    state.recordingId=null;state.recorder=null;state.recorderStopped=null;state.startedAt=0;state.localChunks=[];state.recordingMeta=null;
    $('#recorder').hidden=true;$('#meeting-form').hidden=false;await openDetail(id);
  }catch(e){
    $('#record-status').textContent='Сохранение не завершено';$('#record-note').textContent='Звук остаётся в этой вкладке. Повторите сохранение или скачайте файл. Не закрывайте вкладку.';
    $('#stop-recording').disabled=false;$('#stop-recording').textContent='Повторить сохранение';throw e;
  }
}
async function stopRecording(reason=null) {
  if(state.stopPromise)return state.stopPromise;
  if(!state.recordingId)return;
  state.stopPromise=finishCapture(reason);
  try{return await state.stopPromise;}finally{state.stopPromise=null;}
}
$('#meeting-date').value=new Date().toLocaleDateString('en-CA');
$$('.nav').forEach(b=>b.addEventListener('click',()=>show(b.dataset.view)));
$('#new-top').addEventListener('click',()=>show('new'));
document.addEventListener('click',e=>{const target=e.target.closest('[data-go],[data-source],[data-open],[data-seek],[data-edit-item],[data-done],[data-speaker]');if(!target)return;if(target.dataset.go)show(target.dataset.go);if(target.dataset.source)setSource(target.dataset.source);if(target.dataset.open)openDetail(target.dataset.open);if(target.dataset.seek){const audio=$('#source-audio');audio.currentTime=Number(target.dataset.seek);audio.play().catch(()=>{});}if(target.dataset.editItem)editItem(target.dataset.editItem);if(target.dataset.done)toggleDone(target.dataset.done);if(target.dataset.speaker)editSpeaker(target.dataset.speaker);});
$$('.source-tabs button').forEach(b=>b.addEventListener('click',()=>setSource(b.dataset.tab)));
$$('.provider').forEach(b=>b.addEventListener('click',()=>{state.provider=b.dataset.provider;$$('.provider').forEach(x=>x.classList.toggle('selected',x===b));$('#meeting-url').placeholder={meet:'https://meet.google.com/…',zoom:'https://…zoom.us/j/…',teams:'https://teams.microsoft.com/l/meetup-join/…'}[state.provider];}));
$('#meeting-file').addEventListener('change',e=>$('#file-name').textContent=e.target.files[0]?.name||'');
$('#open-meeting').addEventListener('click',()=>{const url=$('#meeting-url').value.trim();if(!validateUrl(url,state.provider)){flash('Введите корректную HTTPS-ссылку выбранной платформы');return;}window.open(url,'_blank','noopener,noreferrer');});
$('#meeting-form').addEventListener('submit',async e=>{e.preventDefault();if(!$('#consent').checked){flash('Сначала подтвердите уведомление и согласие участников');return;}const button=$('#submit-meeting');button.disabled=true;try {if(state.source==='upload'){const file=$('#meeting-file').files[0];if(!file)throw new Error('Выберите запись');const data=new FormData();data.set('file',file);data.set('title',$('#meeting-title').value.trim());data.set('meeting_date',$('#meeting-date').value);data.set('participants',$('#meeting-participants').value);const m=await api('/api/meetings/upload',{method:'POST',body:data});await openDetail(m.id);}else await prepareCapture();}catch(err){flash(err.message);}finally{button.disabled=false;}});
$('#begin-recording').addEventListener('click',()=>beginRecording().catch(e=>flash(e.message)));
$('#stop-recording').addEventListener('click',()=>stopRecording().catch(e=>flash(e.message)));
$('#cancel-capture').addEventListener('click',()=>cancelCapture().catch(e=>flash(e.message)));
window.addEventListener('beforeunload',event=>{if(state.recordingId||state.starting){event.preventDefault();event.returnValue='';}});
$('#back-to-list').addEventListener('click',()=>show('meetings'));
$('#approve').addEventListener('click',()=>openEditor({title:'Утверждение протокола',description:'Подтвердите, что вы проверили содержание, имена участников, ответственных и сроки. Утверждённая версия сохранится в архиве.',fields:[],submitLabel:'Утвердить',onSave:()=>api(`/api/meetings/${state.current.id}/approve`,{method:'POST',...json({actor:userActor(),confirmed:true})})}));
$('#retry').addEventListener('click',async()=>{$('#retry').disabled=true;try{await api(`/api/meetings/${state.current.id}/retry`,{method:'POST'});await openDetail(state.current.id);loadHealth();}catch(e){flash(e.message);}finally{$('#retry').disabled=false;}});
function editSpeaker(id){openEditor({title:'Участник совещания',description:'Имя предложено по обращениям в разговоре. Подтвердите его — оно изменится во всех репликах этого голоса. Если имя неизвестно, укажите «Не установлен».',fields:[{name:'name',label:'Имя и отчество',value:(state.current.speakers[id]||'').replace(/ \(проверьте\)$/,''),required:true}],submitLabel:'Подтвердить имя',onSave:values=>api(`/api/meetings/${state.current.id}/speakers/${encodeURIComponent(id)}`,{method:'PATCH',...json({actor:userActor(),name:values.name.trim()})})});}
function confirmSpeakerNames(){
  const meetingId=state.current.id;const entries=Object.entries(state.current.speakers||{}).filter(([,name])=>name.endsWith('(проверьте)'));
  openEditor({title:'Подтвердите имена говорящих',description:'Сверьте каждое имя с голосом на записи. Исправьте ошибочные предложения. Для неизвестного участника укажите «Не установлен».',fields:entries.map(([id,name],index)=>({name:`speaker_${index}`,label:`Голос ${id.replace(/^SPEAKER_/i,'')}`,value:name.replace(/\s*\(проверьте\)$/,''),required:true})),submitLabel:'Сохранить подтверждённые имена',onSave:async values=>{
    for(let index=0;index<entries.length;index++){
      const name=values[`speaker_${index}`].trim();if(!name)throw new Error('Заполните имя для каждого голоса');
    }
    for(let index=0;index<entries.length;index++)await api(`/api/meetings/${meetingId}/speakers/${encodeURIComponent(entries[index][0])}`,{method:'PATCH',...json({actor:userActor(),name:values[`speaker_${index}`].trim()})});
  }});
}
function itemFields(item={},creating=false){return [...(creating?[{name:'kind',label:'Тип',type:'select',options:kinds,value:'action'}]:[]),{name:'title',label:'Суть поручения или решения',type:'textarea',value:item.title||'',required:true},{name:'description',label:'Подробности',type:'textarea',value:item.description||''},{name:'owner',label:'Ответственный',value:item.owner||'',placeholder:'Если не назван, оставьте пустым'},{name:'due_text',label:'Срок из разговора',value:item.due_text||'',placeholder:'Например, до пятницы'},{name:'due_date',label:'Календарная дата',type:'date',value:item.due_date||''},...(creating?[{name:'sources',label:'Номера исходных реплик',placeholder:'Например, 7, 8',required:true}]:[]),{name:'review_note',label:'Примечание секретаря',type:'textarea',value:item.review_note||''}];}
function editItem(id){const item=state.current.items.find(x=>x.id===id);if(!item)return;openEditor({title:'Редактирование поручения',description:'Сверьте формулировку с исходной репликой. Все изменения сохраняются в журнале протокола.',fields:itemFields(item),onSave:values=>api(`/api/meetings/${state.current.id}/items/${id}`,{method:'PATCH',...json({actor:userActor(),changes:{...values,owner:values.owner.trim()||null,due_text:values.due_text.trim()||null,due_date:values.due_date||null}})})});}
async function toggleDone(id){const item=state.current.items.find(x=>x.id===id);try{await api(`/api/meetings/${state.current.id}/items/${id}`,{method:'PATCH',...json({actor:userActor(),changes:{status:item.status==='done'?'in_progress':'done'}})});openDetail(state.current.id);}catch(e){flash(e.message);}}
$('#edit-summary').addEventListener('click',()=>openEditor({title:'Краткое содержание совещания',description:'Разделите содержание по темам. Сохраните ключевые цифры, причины проблем и принятые решения.',fields:[{name:'summary',label:'Саммари',type:'textarea',rows:14,value:state.current.summary,required:true}],onSave:values=>api(`/api/meetings/${state.current.id}/summary`,{method:'PATCH',...json({actor:userActor(),summary:values.summary.trim()})})}));
$('#add-item').addEventListener('click',()=>openEditor({title:'Добавить поручение или решение',fields:itemFields({},true),onSave:values=>{const {sources,...item}=values;return api(`/api/meetings/${state.current.id}/items`,{method:'POST',...json({actor:userActor(),...item,owner:item.owner.trim()||null,due_text:item.due_text.trim()||null,due_date:item.due_date||null,source_segment_ids:sources.split(',').map(x=>x.trim()).filter(Boolean)})});}}));
const editor=document.createElement('dialog');editor.className='editor-dialog';document.body.append(editor);
function openEditor({title,description='',fields,submitLabel='Сохранить изменения',onSave}){
  editor.innerHTML=`<form class="editor-form"><div class="editor-heading"><div><p class="eyebrow blue">ПРОВЕРКА ПРОТОКОЛА</p><h2>${esc(title)}</h2></div><button type="button" class="editor-close" aria-label="Закрыть">×</button></div>${description?`<p class="editor-description">${esc(description)}</p>`:''}<div class="editor-fields">${fields.map(f=>`<label>${esc(f.label)}${f.type==='textarea'?`<textarea name="${f.name}" rows="${f.rows||3}" ${f.required?'required':''}>${esc(f.value||'')}</textarea>`:f.type==='select'?`<select name="${f.name}">${Object.entries(f.options).map(([key,label])=>`<option value="${key}" ${key===f.value?'selected':''}>${esc(label)}</option>`).join('')}</select>`:`<input name="${f.name}" type="${f.type||'text'}" value="${esc(f.value||'')}" placeholder="${esc(f.placeholder||'')}" ${f.required?'required':''}>`}</label>`).join('')}</div><p class="editor-error" role="alert" hidden></p><div class="editor-footer"><button type="button" class="button secondary editor-cancel">Отмена</button><button type="submit" class="button primary">${esc(submitLabel)}</button></div></form>`;
  editor.querySelector('.editor-close').onclick=editor.querySelector('.editor-cancel').onclick=()=>editor.close();
  editor.querySelector('form').onsubmit=async event=>{event.preventDefault();const button=editor.querySelector('[type=submit]');button.disabled=true;const error=editor.querySelector('.editor-error');error.hidden=true;try{await onSave(Object.fromEntries(new FormData(event.currentTarget)));editor.close();await openDetail(state.current.id);flash('Изменения сохранены');}catch(e){error.textContent=e.message;error.hidden=false;}finally{button.disabled=false;}};
  editor.showModal();
}
const speakerReview=document.createElement('div');speakerReview.className='speaker-review';speakerReview.hidden=true;$('#detail-segments').before(speakerReview);
const reanalyzeButton=document.createElement('button');reanalyzeButton.className='button secondary';reanalyzeButton.textContent='Обновить AI-анализ';reanalyzeButton.hidden=true;$('.detail-actions').append(reanalyzeButton);reanalyzeButton.onclick=async()=>{reanalyzeButton.disabled=true;try{const m=await api(`/api/meetings/${state.current.id}/reanalyze`,{method:'POST'});await openDetail(m.id);}catch(e){flash(e.message);}finally{if(state.current)renderDetail(state.current);}};
$('#system-pill').title='Нажмите, чтобы повторить проверку';$('#system-pill').addEventListener('click',loadHealth);
loadHealth();loadMeetings();
