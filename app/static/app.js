const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {view:'overview', source:'upload', provider:'meet', meetings:[], current:null, poll:null, detailRequest:0,
  stream:null, displayStream:null, micStream:null, audioContext:null, meterTimer:null, recorder:null, startedAt:0,
  uploadChain:Promise.resolve(), uploadError:null, recordingId:null, gaps:[], silentSince:null, lastSignal:0,
  starting:false, stopPromise:null, recorderStopped:null, localChunks:[], recordingMeta:null, recordingUrl:null, captureClosed:false,
  user:null,csrf:null,users:[],notifications:[],inboxPoll:null,sessionVersion:0,inboxFilter:'all',notificationRequest:0,
  notice:null,noticePromise:null,noticeAcknowledgedVersion:null,demo:false,demoUsers:[],demoDefault:'a.saparova',demoLoginBusy:false};
const labels = {recording:'Идёт запись',queued:'В очереди',processing:'Обработка',review:'Нужна проверка',approved:'Утверждён',error:'Ошибка'};
const kinds = {action:'Поручение',decision:'Решение',initiative:'Инициатива',question:'Открытый вопрос',risk:'Риск'};
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = n => `${String(Math.floor(n/60)).padStart(2,'0')}:${String(Math.floor(n%60)).padStart(2,'0')}`;
function flash(message) { if(!message)return;const box=$('#flash'); box.textContent=message; box.hidden=false; clearTimeout(flash.timer); flash.timer=setTimeout(()=>box.hidden=true,5500); }
async function api(path, options={}) {
  const sessionVersion=state.sessionVersion,headers=new Headers(options.headers);
  if(!['GET','HEAD','OPTIONS'].includes((options.method||'GET').toUpperCase())&&state.csrf)headers.set('x-csrf-token',state.csrf);
  const response=await fetch(path,{...options,headers,credentials:'same-origin'});
  const body=await response.json().catch(()=>null);
  if(sessionVersion!==state.sessionVersion){const error=new Error('');error.stale=true;throw error;}
  if(!response.ok){
    if(response.status===401&&!path.startsWith('/api/auth/'))showAuth(true);
    const error=new Error(typeof body?.detail==='string'?body.detail:`Ошибка ${response.status}`);error.status=response.status;throw error;
  }
  return body;
}
function json(body) {return {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};}
function canEdit(){return Boolean(state.user&&state.user.role!=='viewer');}
function show(view) {if(!state.user)return;if(view==='new'&&!canEdit())view='overview';if(view==='users'&&state.user.role!=='admin')view='overview';state.view=view;if(view!=='detail'){clearInterval(state.poll);state.detailRequest++;} $$('.view').forEach(el=>el.hidden=el.id!==view); $$('.nav').forEach(el=>el.classList.toggle('active',el.dataset.view===view)); $('#page-title').textContent={overview:'Обзор',new:'Новое совещание',meetings:'Протоколы',dashboard:'Поручения',detail:'Протокол',notifications:'Уведомления',users:'Пользователи'}[view];if(view==='dashboard') loadDashboard();if(view==='meetings'||view==='overview') loadMeetings();if(view==='notifications')loadNotifications();if(view==='users')loadUsers();window.scrollTo(0,0);}
function setSource(source) {if(state.stream||state.recordingId){show('new');flash('Сначала завершите текущую запись');return;}state.source=source;state.noticeAcknowledgedVersion=null;$('#consent').checked=false;$$('#new .source-tabs button').forEach(el=>el.classList.toggle('selected',el.dataset.tab===source));$$('.mode-fields').forEach(el=>el.hidden=el.id!==`${source}-fields`);$('#submit-meeting').textContent=source==='upload'?'Загрузить и обработать →':'Подключить и записывать →';updateNoticeControls();if(source!=='upload')loadRecordingNotice().catch(error=>flash(error.message));show('new');}
function validateUrl(url,provider) {try {const u=new URL(url);if(u.protocol!=='https:')return false;const h=u.hostname.toLowerCase();return provider==='meet'?h==='meet.google.com':provider==='zoom'?(h==='zoom.us'||h.endsWith('.zoom.us')):['teams.microsoft.com','teams.live.com'].includes(h);}catch{return false;}}
async function loadHealth() {const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),8000);try {const h=await api('/api/health',{signal:controller.signal});const ready=Boolean(h.ready);$('#system-pill').textContent=ready?'Модели готовы к обработке':'Нужна настройка моделей';$('#system-pill').classList.toggle('warn',!ready);}catch(e){$('#system-pill').textContent=e.name==='AbortError'?'Проверка заняла больше времени':'Сервер недоступен';$('#system-pill').classList.add('warn');}finally{clearTimeout(timer);}}
async function loadMeetings() {try {state.meetings=await api('/api/meetings');renderList('#recent-list',state.meetings.slice(0,5));renderList('#all-list',state.meetings);}catch(e){flash(e.message);}}
function renderList(selector,meetings) {$(selector).innerHTML=meetings.length?meetings.map(m=>`<div class="list-row"><div><b>${esc(m.title)}</b><small>${esc(m.meeting_date)} · ${m.source==='demo'?'Учебный пример · без аудио':m.source==='platform'?esc((m.provider||'').toUpperCase()):m.source==='room'?'Переговорная':'Загрузка'} · ${m.items.length} элементов</small></div><div><span class="state ${esc(m.state)}">${labels[m.state]||esc(m.state)}</span> <button class="link" data-open="${esc(m.id)}">Открыть →</button></div></div>`).join(''):'<div class="empty">Совещаний пока нет. Создайте первый протокол.</div>';}
async function loadDashboard() {try {const data=await api('/api/dashboard');const names={overdue:'Просрочено',soon:'Скоро срок',on_track:'В работе',no_date:'Без срока',done:'Выполнено'};$('#stats').innerHTML=Object.entries(names).map(([key,label])=>`<div class="stat"><strong>${data.counts[key]||0}</strong><span>${label}</span></div>`).join('');$('#action-list').innerHTML=data.actions.length?data.actions.map(item=>`<div class="list-row"><div><b>${esc(item.title)}</b><small>${esc(item.meeting_title)} · ${esc(item.owner||'Ответственный не указан')} · ${esc(item.due_date||item.due_text||'Без срока')}</small></div><div><span class="state ${item.deadline_state==='overdue'?'error':''}">${esc(names[item.deadline_state]||item.deadline_state)}</span> <button class="link" data-open="${esc(item.meeting_id)}">Открыть →</button></div></div>`).join(''):'<div class="empty">Утверждённых поручений пока нет.</div>';}catch(e){flash(e.message);}}
async function openDetail(id, itemId=null) {
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
      state.current=meeting;renderDetail(meeting);if(itemId){focusItem(itemId);itemId=null;}polling=['queued','processing','recording'].includes(meeting.state);
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
function sourceLink(id,segments){if(state.current?.source==='demo')return `№${esc(id)}`;const segment=segments.find(s=>String(s.id)===String(id));const start=Number(segment?.start);return segment&&Number.isFinite(start)?`<button type="button" class="time-link" data-seek="${start}" title="Прослушать источник №${esc(id)}">№${esc(id)} · ${fmtTime(start)}</button>`:`№${esc(id)}`;}
function renderSummary(meeting){
  const container=$('#detail-summary');
  const topics=Array.isArray(meeting.summary_topics)?meeting.summary_topics.filter(topic=>topic&&typeof topic.text==='string'&&topic.text.trim()):[];
  container.classList.toggle('summary-topics',topics.length>0);
  if(!topics.length){container.textContent=meeting.summary||'Саммари ещё не сформировано.';return;}
  container.innerHTML=topics.map((topic,index)=>`<article class="summary-topic"><h3><span>${index+1}</span>${esc(topic.title||'Итоги обсуждения')}</h3><p>${esc(topic.text)}</p>${Array.isArray(topic.source_segment_ids)&&topic.source_segment_ids.length?`<div class="summary-sources">Источники: ${topic.source_segment_ids.map(id=>sourceLink(id,meeting.segments)).join(' ')}</div>`:''}</article>`).join('');
}
function renderDetail(m) {const canExport=['review','approved'].includes(m.state)||(m.state==='error'&&Boolean(m.summary?.trim())&&m.segments.length>0);$('#detail-title').textContent=m.title;$('#detail-sub').textContent=m.source==='demo'?'УЧЕБНЫЙ ПРИМЕР · БЕЗ АУДИО':m.provider?`ОНЛАЙН · ${m.provider.toUpperCase()}`:m.source==='room'?'ПЕРЕГОВОРНАЯ':'ЗАГРУЖЕННАЯ ЗАПИСЬ';$('#detail-meta').textContent=`${m.meeting_date} · ${m.participants.join(', ')||'Участники не указаны'}`;$('#detail-state').textContent=labels[m.state]||m.state;$('#detail-state').className=`state ${m.state}`;$('#detail-stage').innerHTML=`<b>${esc(m.stage)}</b>${m.error?`<p>${esc(m.error)}</p>`:''}`;$('#detail-content').hidden=!['review','approved','error'].includes(m.state);$('#approve').hidden=!canEdit()||m.state!=='review';$('#export-pdf').hidden=$('#export-docx').hidden=!canExport;$('#retry').hidden=!canEdit()||m.state!=='error';$('#export-pdf').href=`/api/meetings/${m.id}/export?format=pdf&include_transcript=true`;$('#export-docx').href=`/api/meetings/${m.id}/export?format=docx&include_transcript=true`;renderSummary(m);$('#item-count').textContent=`(${m.items.length})`;$('#detail-items').innerHTML=m.items.length?m.items.map(item=>`<div class="item" data-item-id="${esc(item.id)}" tabindex="-1"><div class="item-top"><span class="tag ${esc(item.kind)}">${kinds[item.kind]||esc(item.kind)}</span><span class="state">${esc(item.status)}</span></div><h3>${esc(item.title)}</h3><p>${esc(item.description||'')}</p><small>Ответственный: ${esc(item.owner||'требует уточнения')} · Срок: ${esc(item.due_date||item.due_text||'требует уточнения')}</small><small>Источники: ${item.source_segment_ids.map(id=>sourceLink(id,m.segments)).join(' ')}${item.review_note?' · Проверить: '+esc(item.review_note):''}</small>${canEdit()&&m.state==='review'?`<div class="item-controls"><button data-edit-item="${esc(item.id)}">Изменить данные</button></div>`:canEdit()&&m.state==='approved'&&item.kind==='action'?`<div class="item-controls"><button data-done="${esc(item.id)}">${item.status==='done'?'Вернуть в работу':'Отметить выполненным'}</button><button data-reminder-item="${esc(item.id)}">Напоминания</button></div>`:''}${item.kind==='action'?`<small>Напоминания: ${item.reminder_recipient?'в аккаунте '+esc(recipientLabel(state.users.find(u=>u.username===item.reminder_recipient))||item.reminder_recipient):'не настроены'}</small>`:''}</div>`).join(''):'<div class="empty">Элементов пока нет. Секретарь может добавить пропущенное при проверке.</div>';
  $('#protocol-download').hidden=!canExport;
  $('#protocol-download-note').textContent=m.state==='approved'?'Утверждённая версия протокола.':'Документ доступен до утверждения и будет помечен «Черновик».';
  for(const format of ['pdf','docx']){
    $('#download-'+format).href=`/api/meetings/${m.id}/export?format=${format}&include_transcript=true`;
    $('#download-full-'+format).href=`/api/meetings/${m.id}/export?format=${format}&include_transcript=false`;
  }
  $('#source-audio').hidden=m.source==='demo';$('#source-audio').nextElementSibling.textContent=m.source==='demo'?'Учебный пример: текст создан для демонстрации, исходного аудио нет.':'Нажмите на время реплики, чтобы перейти к фрагменту.';if(m.source==='demo'){$('#source-audio').removeAttribute('src');$('#source-audio').load();}else $('#source-audio').src=`/api/meetings/${m.id}/media`;$('#detail-gaps').innerHTML=m.gaps.map(g=>`<div class="gap">Проверьте ${fmtTime(g.start)}–${fmtTime(g.end)}: ${esc(g.reason)}</div>`).join('');$('#detail-segments').innerHTML=m.segments.length?m.segments.map(s=>`<div class="turn"><div class="turn-meta">${m.source==='demo'?`<span class="small muted">Реплика №${esc(s.id)}</span>`:`<button class="time-link" data-seek="${s.start}">${fmtTime(s.start)}</button>`}<b>${esc(m.speakers[s.speaker_id]||s.speaker_id)}</b>${canEdit()&&m.state==='review'?`<button class="speaker-edit" data-speaker="${esc(s.speaker_id)}">Переименовать</button>`:''}</div><p>${esc(s.text)}</p></div>`).join(''):'<div class="empty">Транскрипт ещё не готов.</div>';
  $('#edit-summary').hidden=$('#add-item').hidden=!canEdit()||m.state!=='review';
  $('#delete-meeting').hidden=state.user?.role!=='admin'||['queued','processing','recording'].includes(m.state);
  reanalyzeButton.hidden=m.source==='demo'||!canEdit()||!m.segments.length||['queued','processing','recording'].includes(m.state);
  reanalyzeButton.disabled=reanalyzeButton.hidden;
  const pending=Object.entries(m.speakers||{}).filter(([,name])=>name.endsWith('(проверьте)'));
  $('#approve').disabled=pending.length>0;$('#approve').title=pending.length?'Сначала подтвердите имена говорящих':'';
  speakerReview.hidden=!canEdit()||m.state!=='review'||!Object.keys(m.speakers||{}).length;
  speakerReview.innerHTML=`<div class="speaker-review-heading"><b>Участники</b>${pending.length?'<button class="link" id="confirm-names">Проверить имена →</button>':'<span class="speaker-confirmed">Имена подтверждены</span>'}</div>${pending.length?'<p>Сверьте голоса с записью и подтвердите предложенные имена перед утверждением.</p>':''}${Object.entries(m.speakers||{}).map(([id,name])=>{const segment=m.segments.find(s=>s.speaker_id===id);return `<div class="speaker-row"><span>${esc(name.replace(/\s*\(проверьте\)$/,''))}${name.endsWith('(проверьте)')?' <small>проверить</small>':''}</span><span>${segment&&m.source!=='demo'?`<button class="time-link" data-seek="${segment.start}">▶ ${fmtTime(segment.start)}</button> `:''}<button class="speaker-edit" data-speaker="${esc(id)}">${name.endsWith('(проверьте)')?'Подтвердить':'Изменить'}</button></span></div>`;}).join('')}`;
  speakerReview.querySelector('#confirm-names')?.addEventListener('click',confirmSpeakerNames);
}
function userActor(){return state.user?.username||'Секретарь';}
function focusItem(id){
  const item=$$('#detail-items [data-item-id]').find(element=>element.dataset.itemId===String(id));
  if(!item){flash('Поручение больше недоступно в этом протоколе.');return;}
  $$('.item-highlight').forEach(element=>element.classList.remove('item-highlight'));
  item.classList.add('item-highlight');item.focus({preventScroll:true});item.scrollIntoView({behavior:'smooth',block:'center'});
}
function updateNoticeControls(){
  const live=state.source!=='upload';$('#recording-notice-card').hidden=!live;
  $('#consent-text').textContent=live?'Я показал или зачитал предупреждение участникам о записи и обработке речи.':'Подтверждаю, что у меня есть право загрузить эту запись для подготовки протокола.';
  $('#consent-help').textContent=live?'Это отметка секретаря об уведомлении, а не индивидуальное согласие каждого участника. Для подключившихся позже повторите предупреждение.':'Этот файл уже записан. Загрузка не уведомляет участников задним числом.';
  $('#consent').disabled=live&&!state.notice;
}
async function loadRecordingNotice(){
  if(state.notice)return state.notice;
  if(state.noticePromise)return state.noticePromise;
  const pending=api('/api/recording-notice').then(notice=>{
    if(!notice?.version||!notice.text_ru||!notice.text_kk||!notice.text)throw new Error('Не удалось загрузить предупреждение о записи. Повторите попытку.');
    state.notice=notice;$('#notice-ru').textContent=notice.text_ru;$('#notice-kk').textContent=notice.text_kk;
    $('#notice-dialog-ru').textContent=notice.text_ru;$('#notice-dialog-kk').textContent=notice.text_kk;
    $('#notice-load-status').hidden=true;updateNoticeControls();return notice;
  }).catch(error=>{if(!error.stale){$('#notice-load-status').textContent='Предупреждение не загружено. Нажмите «Показать участникам», чтобы повторить.';$('#notice-load-status').hidden=false;}throw error;})
    .finally(()=>{if(state.noticePromise===pending)state.noticePromise=null;});
  state.noticePromise=pending;return pending;
}
async function showRecordingNotice(){
  try{await loadRecordingNotice();$('#notice-copy-help').hidden=true;$('#notice-copy-text').hidden=true;if(!$('#notice-dialog').open)$('#notice-dialog').showModal();}
  catch(error){flash(error.message);}
}
async function copyRecordingNotice(){
  try{
    const notice=await loadRecordingNotice();
    try{await navigator.clipboard.writeText(notice.text);$('#notice-copy-help').textContent='Текст скопирован. Вставьте его в чат встречи вручную.';$('#notice-copy-help').hidden=false;flash('Предупреждение скопировано. Вставьте его в чат встречи.');}
    catch{if(!$('#notice-dialog').open)$('#notice-dialog').showModal();const field=$('#notice-copy-text');field.value=notice.text;field.hidden=false;field.focus();field.select();$('#notice-copy-help').textContent='Браузер не разрешил копирование. Текст выделен: нажмите Ctrl+C или ⌘C.';$('#notice-copy-help').hidden=false;}
  }catch(error){flash(error.message);}
}
async function closeCapture() {
  state.captureClosed=true;clearInterval(state.meterTimer);state.meterTimer=null;$('#recording-banner').hidden=true;
  state.stream?.getTracks().forEach(t=>t.stop());state.displayStream?.getTracks().forEach(t=>t.stop());state.micStream?.getTracks().forEach(t=>t.stop());
  state.stream=null;state.displayStream=null;state.micStream=null;
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
  try{
    if(state.source==='platform'){
      display=await navigator.mediaDevices.getDisplayMedia({video:true,audio:true,preferCurrentTab:false,selfBrowserSurface:'exclude',systemAudio:'exclude'});
      state.displayStream=display;
      const audio=display.getAudioTracks();
      if(!audio.length)throw new Error('Звук вкладки не выбран. В Chrome включите «Передавать звук вкладки».');
      stream=new MediaStream(audio);state.stream=stream;
      if($('#include-microphone').checked){
        try{state.micStream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});}
        catch{throw new Error('Нет доступа к микрофону. Разрешите его в браузере или отключите «Добавить мой микрофон» и подключитесь снова.');}
        const context=new AudioContext();state.audioContext=context;await context.resume();
        const mixed=context.createMediaStreamDestination();
        for(const input of [stream,state.micStream]){const gain=context.createGain();gain.gain.value=.7;context.createMediaStreamSource(input).connect(gain).connect(mixed);}
        stream=mixed.stream;
      }
    }else{stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});state.micStream=stream;}
  }catch(error){await closeCapture();throw error;}
  state.stream=stream;state.displayStream=display;state.captureClosed=false;state.startedAt=0;
  state.localChunks=[];state.gaps=[];state.silentSince=null;state.lastSignal=0;state.recorder=null;
  if(state.recordingUrl)URL.revokeObjectURL(state.recordingUrl);state.recordingUrl=null;
  $('#meeting-form').hidden=true;$('#recorder').hidden=false;$('#record-title').textContent=$('#meeting-title').value;
  $('#record-status').textContent='Ожидание реального аудиосигнала…';$('#record-timer').textContent='00:00';
  $('#stop-recording').hidden=true;
  $('#stop-recording').disabled=false;$('#stop-recording').textContent='Остановить и обработать';
  $('#cancel-capture').hidden=true;$('#cancel-capture').disabled=false;$('#download-recording').hidden=true;
  $('#record-note').textContent='Источник подключён. Запись запускается автоматически…';
  try{
    const context=state.audioContext||new AudioContext();state.audioContext=context;await context.resume();
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
      if(state.startedAt)$('#record-timer').textContent=fmtTime((Date.now()-state.startedAt)/1000);
    },180);
    [...new Set([...(display||stream).getTracks(),...(state.micStream?.getTracks()||[])])].forEach(track=>track.addEventListener('ended',()=>{
      if(state.captureClosed)return;
      if(state.recordingId){flash('Источник звука отключён. Сохраняю запись.');stopRecording('Захват вкладки или микрофона прерван').catch(e=>flash(e.message));}
      else if(!state.starting){cancelCapture();flash('Источник звука отключён до начала записи. Подключите его заново.');}
    }));
    await beginRecording();
  }catch(e){await closeCapture();$('#recorder').hidden=true;$('#meeting-form').hidden=false;throw e;}
}
async function beginRecording() {
  if(state.starting||state.recordingId)return;
  if(!state.stream?.getAudioTracks().some(track=>track.readyState==='live'))throw new Error('Источник звука отключён. Подключите его заново.');
  if(!$('#consent').checked||!state.notice||state.noticeAcknowledgedVersion!==state.notice.version)throw new Error('Сначала покажите или зачитайте предупреждение и подтвердите уведомление участников.');
  state.starting=true;$('#cancel-capture').disabled=true;
  try{
    const body={title:$('#meeting-title').value.trim(),meeting_date:$('#meeting-date').value,participants:$('#meeting-participants').value,source:state.source,provider:state.source==='platform'?state.provider:null,meeting_url:state.source==='platform'?$('#meeting-url').value.trim():null,consent_confirmed:$('#consent').checked,notice_version:state.noticeAcknowledgedVersion};
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
    recorder.start(2000);state.startedAt=Date.now();$('#recording-banner').hidden=false;
    $('#stop-recording').hidden=false;$('#cancel-capture').hidden=true;
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
$('#meeting-date').value=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Almaty'});
$$('.nav').forEach(b=>b.addEventListener('click',()=>show(b.dataset.view)));
$('#new-top').addEventListener('click',()=>show('new'));
document.addEventListener('click',e=>{const target=e.target.closest('[data-go],[data-source],[data-open],[data-seek],[data-edit-item],[data-done],[data-speaker],[data-reminder-item],[data-read-notification]');if(!target)return;if(target.dataset.go)show(target.dataset.go);if(target.dataset.source)setSource(target.dataset.source);if(target.dataset.open)openDetail(target.dataset.open,target.dataset.openItem||null);if(target.dataset.seek){const audio=$('#source-audio');audio.currentTime=Number(target.dataset.seek);audio.play().catch(()=>{});}if(target.dataset.editItem)editItem(target.dataset.editItem);if(target.dataset.done)toggleDone(target.dataset.done);if(target.dataset.speaker)editSpeaker(target.dataset.speaker);if(target.dataset.reminderItem)editReminder(target.dataset.reminderItem);if(target.dataset.readNotification)markNotificationRead(target.dataset.readNotification);});
$$('#new .source-tabs button').forEach(b=>b.addEventListener('click',()=>setSource(b.dataset.tab)));
$$('.provider').forEach(b=>b.addEventListener('click',()=>{state.provider=b.dataset.provider;$$('.provider').forEach(x=>x.classList.toggle('selected',x===b));$('#meeting-url').placeholder={meet:'https://meet.google.com/…',zoom:'https://…zoom.us/j/…',teams:'https://teams.microsoft.com/l/meetup-join/…'}[state.provider];}));
$('#meeting-file').addEventListener('change',e=>$('#file-name').textContent=e.target.files[0]?.name||'');
$('#open-meeting').addEventListener('click',()=>{const url=$('#meeting-url').value.trim();if(!validateUrl(url,state.provider)){flash('Введите корректную HTTPS-ссылку выбранной платформы');return;}window.open(url,'_blank','noopener,noreferrer');});
$('#meeting-form').addEventListener('submit',async e=>{e.preventDefault();if(!$('#consent').checked){flash(state.source==='upload'?'Подтвердите право на обработку записи.':'Подтвердите, что вы уведомили участников о записи.');return;}const button=$('#submit-meeting');button.disabled=true;try {if(state.source==='upload'){const file=$('#meeting-file').files[0];if(!file)throw new Error('Выберите запись');const data=new FormData();data.set('file',file);data.set('title',$('#meeting-title').value.trim());data.set('meeting_date',$('#meeting-date').value);data.set('participants',$('#meeting-participants').value);const m=await api('/api/meetings/upload',{method:'POST',body:data});await openDetail(m.id);}else await prepareCapture();}catch(err){flash(err.message);}finally{button.disabled=false;}});
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
function itemFields(item={},creating=false){return [...(creating?[{name:'kind',label:'Тип',type:'select',options:kinds,value:'action'}]:[]),{name:'title',label:'Суть поручения или решения',type:'textarea',value:item.title||'',required:true},{name:'description',label:'Подробности',type:'textarea',value:item.description||''},{name:'owner',label:'Ответственный',value:item.owner||'',placeholder:'Если не назван, оставьте пустым'},{name:'due_text',label:'Срок из разговора',value:item.due_text||'',placeholder:'Например, до пятницы'},{name:'due_date',label:'Календарная дата',type:'date',value:item.due_date||''},...(creating?[{name:'sources',label:'Номера исходных реплик',placeholder:'Например, 7, 8',required:true}]:[]),...reminderFields(item),{name:'review_note',label:'Примечание секретаря',type:'textarea',value:item.review_note||''}];}
function editItem(id){const item=state.current.items.find(x=>x.id===id);if(!item)return;openEditor({title:'Редактирование поручения',description:'Сверьте формулировку с исходной репликой. Все изменения сохраняются в журнале протокола.',fields:itemFields(item),onSave:values=>api(`/api/meetings/${state.current.id}/items/${id}`,{method:'PATCH',...json({actor:userActor(),changes:{...values,owner:values.owner.trim()||null,due_text:values.due_text.trim()||null,due_date:values.due_date||null,reminder_recipient:values.reminder_recipient||null,reminder_channel:'inbox'}})})});}
async function toggleDone(id){const item=state.current.items.find(x=>x.id===id);try{await api(`/api/meetings/${state.current.id}/items/${id}`,{method:'PATCH',...json({actor:userActor(),changes:{status:item.status==='done'?'in_progress':'done'}})});openDetail(state.current.id);}catch(e){flash(e.message);}}
$('#edit-summary').addEventListener('click',()=>openEditor({title:'Саммари по ключевым пунктам',description:'Разделите содержание по темам. Сохраните ключевые цифры, причины проблем и принятые решения.',fields:[{name:'summary',label:'Саммари',type:'textarea',rows:14,value:state.current.summary,required:true}],onSave:values=>api(`/api/meetings/${state.current.id}/summary`,{method:'PATCH',...json({actor:userActor(),summary:values.summary.trim()})})}));
$('#add-item').addEventListener('click',()=>openEditor({title:'Добавить поручение или решение',fields:itemFields({},true),onSave:values=>{const {sources,...item}=values;return api(`/api/meetings/${state.current.id}/items`,{method:'POST',...json({actor:userActor(),...item,owner:item.owner.trim()||null,due_text:item.due_text.trim()||null,due_date:item.due_date||null,reminder_recipient:item.reminder_recipient||null,reminder_channel:'inbox',source_segment_ids:sources.split(',').map(x=>x.trim()).filter(Boolean)})});}}));
const editor=document.createElement('dialog');editor.className='editor-dialog';document.body.append(editor);
function openEditor({title,description='',fields,submitLabel='Сохранить изменения',onSave,afterSave,successMessage='Изменения сохранены'}){
  const sessionVersion=state.sessionVersion;
  editor.innerHTML=`<form class="editor-form"><div class="editor-heading"><div><p class="eyebrow blue">ПРОВЕРКА ПРОТОКОЛА</p><h2>${esc(title)}</h2></div><button type="button" class="editor-close" aria-label="Закрыть">×</button></div>${description?`<p class="editor-description">${esc(description)}</p>`:''}<div class="editor-fields">${fields.map(f=>`<label>${esc(f.label)}${f.type==='textarea'?`<textarea name="${f.name}" rows="${f.rows||3}" ${f.required?'required':''}>${esc(f.value||'')}</textarea>`:f.type==='select'?`<select name="${f.name}">${Object.entries(f.options).map(([key,label])=>`<option value="${esc(key)}" ${key===f.value?'selected':''}>${esc(label)}</option>`).join('')}</select>`:`<input name="${f.name}" type="${f.type||'text'}" value="${esc(f.value||'')}" placeholder="${esc(f.placeholder||'')}" ${f.required?'required':''} ${f.minLength?`minlength="${f.minLength}"`:''} ${f.type==='password'?'autocomplete="new-password"':''}>`}</label>`).join('')}</div><p class="editor-error" role="alert" hidden></p><div class="editor-footer"><button type="button" class="button secondary editor-cancel">Отмена</button><button type="submit" class="button primary">${esc(submitLabel)}</button></div></form>`;
  editor.querySelector('.editor-close').onclick=editor.querySelector('.editor-cancel').onclick=()=>editor.close();
  editor.querySelector('form').onsubmit=async event=>{event.preventDefault();const button=editor.querySelector('[type=submit]');button.disabled=true;const error=editor.querySelector('.editor-error');error.hidden=true;try{const result=await onSave(Object.fromEntries(new FormData(event.currentTarget)));if(sessionVersion!==state.sessionVersion)return;editor.close();if(afterSave)await afterSave(result);else if(state.current)await openDetail(state.current.id);flash(successMessage);}catch(e){if(e.stale||sessionVersion!==state.sessionVersion)return;error.textContent=e.message;error.hidden=false;}finally{button.disabled=false;}};
  editor.showModal();
}
const speakerReview=document.createElement('div');speakerReview.className='speaker-review';speakerReview.hidden=true;$('#detail-segments').before(speakerReview);
const reanalyzeButton=document.createElement('button');reanalyzeButton.className='button secondary';reanalyzeButton.textContent='Обновить AI-анализ';reanalyzeButton.hidden=true;$('.detail-actions').append(reanalyzeButton);reanalyzeButton.onclick=async()=>{reanalyzeButton.disabled=true;try{const m=await api(`/api/meetings/${state.current.id}/reanalyze`,{method:'POST'});await openDetail(m.id);}catch(e){flash(e.message);}finally{if(state.current)renderDetail(state.current);}};
$('#system-pill').title='Нажмите, чтобы повторить проверку';$('#system-pill').addEventListener('click',loadHealth);
const roleNames={admin:'Администратор',secretary:'Секретарь',viewer:'Наблюдатель'};
const authDialog=$('#auth-dialog');
authDialog.addEventListener('cancel',event=>event.preventDefault());
function showAuth(configured){
  state.sessionVersion++;state.notificationRequest++;
  state.user=null;state.csrf=null;state.authConfigured=configured;
  state.users=[];state.notifications=[];state.meetings=[];state.current=null;state.notice=null;state.noticePromise=null;state.noticeAcknowledgedVersion=null;
  for(const selector of ['#recent-list','#all-list','#user-list','#notification-list','#action-list','#stats','#detail-items','#detail-segments','#detail-summary'])$(selector).replaceChildren();
  $('#inbox-badge').hidden=true;$('#detail-content').hidden=true;$('#account-name').textContent='';$('#flash').hidden=true;
  $('#consent').checked=false;$('#notice-dialog').close();updateNoticeControls();
  clearInterval(state.poll);clearInterval(state.inboxPoll);state.detailRequest++;
  document.body.classList.add('auth-locked');
  if(editor.open)editor.close();
  $('#source-audio').pause();$('#source-audio').removeAttribute('src');$('#source-audio').load();
  $('#auth-title').textContent=state.demo?'Открыть демонстрацию':configured?'Вход в приложение':'Создайте администратора';
  $('#auth-description').textContent=state.demo?'Демо открывается без пароля. Внутри можно переключаться между сотрудниками и смотреть их уведомления.':configured?'Войдите в аккаунт, выданный администратором организации.':'Первый запуск: создайте личный аккаунт администратора на компьютере сервера. Пароль — не менее 12 символов.';
  $('#auth-display-label').hidden=state.demo||configured;
  $('#auth-fields').hidden=state.demo;$$('#auth-fields input').forEach(input=>input.disabled=state.demo);
  const form=$('#auth-form');form.elements.display_name.required=!state.demo&&!configured;
  form.elements.password.minLength=configured?1:12;
  form.elements.password.autocomplete=configured?'current-password':'new-password';
  $('#auth-submit').textContent=state.demo?'Войти в демо':configured?'Войти':'Создать администратора';
  if(!authDialog.open){form.reset();$('#auth-error').hidden=true;authDialog.showModal();}
}
async function acceptSession(session){
  state.sessionVersion++;state.notificationRequest++;clearInterval(state.poll);state.detailRequest++;
  state.user=session.user;state.csrf=session.csrf;state.current=null;state.users=[];state.meetings=[];
  if(editor.open)editor.close();$('#source-audio').pause();$('#source-audio').removeAttribute('src');$('#source-audio').load();
  for(const selector of ['#recent-list','#all-list','#user-list','#action-list','#stats','#detail-items','#detail-segments','#detail-summary'])$(selector).replaceChildren();
  $('#detail-content').hidden=true;
  state.inboxFilter='all';state.notifications=[];renderNotifications();
  authDialog.close();$('#auth-form').reset();document.body.classList.remove('auth-locked');
  document.body.classList.toggle('role-viewer',!canEdit());
  $('#account-name').textContent=state.demo?roleNames[state.user.role]:`${state.user.display_name} · ${roleNames[state.user.role]}`;
  $('#logout').hidden=state.demo;$('#demo-account-control').hidden=!state.demo;
  if(state.demo){$('#demo-account').innerHTML=[...new Set(state.demoUsers.map(user=>user.role))].map(role=>`<optgroup label="${esc(roleNames[role]||role)}">${state.demoUsers.filter(user=>user.role===role).map(user=>`<option value="${esc(user.username)}">${esc(user.display_name)}</option>`).join('')}</optgroup>`).join('');$('#demo-account').value=state.user.username;}
  $('[data-view="users"]').hidden=state.user.role!=='admin';
  for(const selector of ['[data-view="new"]','#new-top','[data-go="new"]','.entry-grid','#quick-start-heading'])$(selector).hidden=!canEdit();
  clearInterval(state.inboxPoll);state.inboxPoll=setInterval(()=>{if(state.user)loadNotifications();},30000);
  show(state.demo&&state.user.role==='viewer'?'notifications':'overview');loadHealth();loadNotifications();if(canEdit())loadUsers();updateNoticeControls();if(state.source!=='upload')loadRecordingNotice().catch(error=>flash(error.message));
}
async function bootstrapAuth(){
  try{
    const status=await api('/api/auth/status');
    state.demo=status.demo===true;state.demoUsers=Array.isArray(status.demo_users)?status.demo_users:[];state.demoDefault=status.demo_default||'a.saparova';
    if(state.demo){
      const chosen=sessionStorage.getItem('protocol-demo-persona');
      const target=state.demoUsers.some(user=>user.username===chosen)?chosen:state.demoDefault;
      try{const session=await api('/api/auth/me');if(session.user.username===target)await acceptSession(session);else await enterDemo(target);}
      catch(error){if(error.status!==401)throw error;await enterDemo(target);}
      return;
    }
    if(!status.configured){showAuth(false);return;}
    try{await acceptSession(await api('/api/auth/me'));}
    catch(error){if(error.status!==401)throw error;showAuth(true);}
  }catch(error){showAuth(true);$('#auth-error').textContent=error.message;$('#auth-error').hidden=false;}
}
async function enterDemo(username=state.demoDefault){
  if(!state.demo||state.demoLoginBusy)return;
  state.demoLoginBusy=true;$('#demo-account').disabled=true;$('#auth-submit').disabled=true;
  try{await acceptSession(await api('/api/auth/demo-login',{method:'POST',...json({username})}));sessionStorage.setItem('protocol-demo-persona',username);}
  catch(error){if(error.stale)return;showAuth(true);$('#auth-error').textContent=error.message||'Не удалось открыть демо. Повторите вход.';$('#auth-error').hidden=false;}
  finally{state.demoLoginBusy=false;$('#demo-account').disabled=false;$('#auth-submit').disabled=false;}
}
$('#demo-account').addEventListener('change',event=>{
  const username=event.target.value;event.target.value=state.user?.username||state.demoDefault;
  if(state.recordingId||state.starting||state.stream){flash('Сначала завершите запись или отмените проверку звука.');return;}
  if(username!==state.user?.username)enterDemo(username);
});
$('#auth-form').addEventListener('submit',async event=>{
  event.preventDefault();$('#auth-error').hidden=true;if(state.demo){await enterDemo();return;}const button=$('#auth-submit');button.disabled=true;
  const values=Object.fromEntries(new FormData(event.currentTarget));
  try{const endpoint=state.authConfigured?'login':'setup';const body=state.authConfigured?{username:values.username,password:values.password}:values;await acceptSession(await api(`/api/auth/${endpoint}`,{method:'POST',...json(body)}));}
  catch(error){if(error.status===409){try{const status=await api('/api/auth/status');if(status.configured)showAuth(true);}catch{}}$('#auth-error').textContent=error.message;$('#auth-error').hidden=false;}
  finally{button.disabled=false;}
});
$('#logout').addEventListener('click',async()=>{
  if(state.recordingId||state.starting||state.stream){flash('Сначала завершите запись или отмените проверку звука.');return;}
  try{await api('/api/auth/logout',{method:'POST'});state.meetings=[];state.current=null;state.notifications=[];state.users=[];$('#detail-content').hidden=true;showAuth(true);}
  catch(error){flash(error.message);}
});
async function loadUsers(){
  if(!canEdit())return;
  try{state.users=await api('/api/users');$('#user-list').innerHTML=state.users.map(user=>`<div class="list-row"><div><b>${esc(user.display_name)}</b><small>${esc(user.email||'Email не указан')} · Логин: ${esc(user.username)}</small>${user.email?.endsWith('@samruk-demo.test')?'<small class="demo-address">Вымышленный адрес для демо · доставка только в приложении</small>':''}</div><span class="state">${esc(roleNames[user.role])}</span></div>`).join('');}
  catch(error){flash(error.message);}
}
$('#add-user').addEventListener('click',()=>openEditor({title:'Новый пользователь',description:'Доступ распространяется на общий архив организации. Передайте логин и пароль пользователю лично. Email служит для выбора сотрудника; сообщения сохраняются только в приложении.',fields:[{name:'username',label:'Логин: латинские буквы, цифры, . _ -',required:true,minLength:3},{name:'display_name',label:'Имя пользователя',required:true},{name:'email',label:'Email (необязательно)',type:'email',placeholder:'name@samruk-demo.test'},{name:'password',label:'Пароль: не менее 12 символов',type:'password',required:true,minLength:12},{name:'role',label:'Права доступа',type:'select',options:roleNames,value:'viewer'}],submitLabel:'Создать пользователя',onSave:values=>api('/api/users',{method:'POST',...json({...values,email:values.email.trim()||null})}),afterSave:loadUsers,successMessage:'Пользователь создан'}));
function recipientLabel(user){return user?`${user.display_name} — ${user.email||user.username}`:'';}
function reminderFields(item={}){return [{name:'reminder_recipient',label:'Получатель уведомлений в приложении',type:'select',value:item.reminder_recipient||'',options:{'':'Уведомления выключены',...Object.fromEntries(state.users.map(user=>[user.username,recipientLabel(user)])),...(item.reminder_recipient&&!state.users.some(user=>user.username===item.reminder_recipient)?{[item.reminder_recipient]:item.reminder_recipient}:{})}}];}
function editReminder(id){
  const item=state.current.items.find(entry=>entry.id===id);if(!item||!canEdit())return;
  openEditor({title:'Уведомления по поручению',description:`${item.title}. После утверждения протокола получатель увидит назначение в своём аккаунте. При указанной дате добавятся напоминания за 3 дня до срока и при просрочке. Выполненные и отменённые поручения не уведомляют.`,fields:reminderFields(item),onSave:values=>api(`/api/meetings/${state.current.id}/items/${id}`,{method:'PATCH',...json({actor:userActor(),changes:{reminder_recipient:values.reminder_recipient||null,reminder_channel:'inbox'}})})});
}
async function loadNotifications(){
  if(!state.user)return;
  const request=++state.notificationRequest;
  try{
    const notifications=await api('/api/notifications');if(request!==state.notificationRequest)return;
    state.notifications=notifications;renderNotifications();$('#inbox-status').textContent='Обновляется каждые 30 секунд. Доставка только внутри приложения.';
  }catch(error){if(error.status!==401)flash(error.message);}
}
function renderNotifications(){
  const unread=state.notifications.filter(notice=>!notice.read_at).length;
  $('#inbox-badge').textContent=String(unread);$('#inbox-badge').hidden=!unread;$('#inbox-unread-count').textContent=String(unread);
  $$('[data-inbox-filter]').forEach(button=>{const active=button.dataset.inboxFilter===state.inboxFilter;button.classList.toggle('selected',active);button.setAttribute('aria-pressed',String(active));});
  const notices=state.inboxFilter==='unread'?state.notifications.filter(notice=>!notice.read_at):state.notifications;
  $('#notification-list').innerHTML=notices.length?notices.map(notice=>`<div class="list-row notification-row ${notice.read_at?'':'unread'}"><div><span class="state ${notice.event==='overdue'?'error':notice.event==='assigned'?'approved':''}">${esc(notice.title)}</span><b>${esc(notice.item_title)}</b><small>${esc(notice.meeting_title)} · ${notice.due_date?'Срок: '+esc(notice.due_date):'Срок не указан'}${notice.owner?' · '+esc(notice.owner):''}</small></div><div class="notification-actions"><button class="link" data-open="${esc(notice.meeting_id)}" data-open-item="${esc(notice.item_id)}">Открыть поручение →</button>${notice.read_at?'<small>Прочитано</small>':`<button class="link" data-read-notification="${esc(notice.id)}">Отметить прочитанным</button>`}</div></div>`).join(''):`<div class="empty">${state.inboxFilter==='unread'?'Непрочитанных уведомлений нет.':'Уведомлений пока нет. Они появятся после назначения вам поручения в утверждённом протоколе.'}</div>`;
}
async function markNotificationRead(id){try{await api(`/api/notifications/${encodeURIComponent(id)}/read`,{method:'POST'});await loadNotifications();}catch(error){flash(error.message);}}
$('#delete-meeting').addEventListener('click',()=>{
  const meeting=state.current;if(!meeting||state.user?.role!=='admin')return;
  openEditor({title:'Удалить запись и протокол',description:`Будут удалены аудио, транскрипт, протокол, поручения и напоминания совещания «${meeting.title}». Введите название точно, чтобы подтвердить удаление.`,fields:[{name:'confirm_title',label:'Название совещания',required:true}],submitLabel:'Удалить без восстановления',onSave:values=>{if(values.confirm_title!==meeting.title)throw new Error('Название должно точно совпадать с названием совещания.');return api(`/api/meetings/${meeting.id}`,{method:'DELETE',...json(values)});},afterSave:async()=>{state.current=null;$('#source-audio').pause();$('#source-audio').removeAttribute('src');$('#source-audio').load();show('meetings');await loadNotifications();},successMessage:'Совещание и запись удалены'});
});
$$('[data-show-notice]').forEach(button=>button.addEventListener('click',showRecordingNotice));
$$('[data-copy-notice]').forEach(button=>button.addEventListener('click',copyRecordingNotice));
$('#close-notice').addEventListener('click',()=>$('#notice-dialog').close());
$('#return-notice').addEventListener('click',()=>$('#notice-dialog').close());
$('#consent').addEventListener('change',()=>{state.noticeAcknowledgedVersion=$('#consent').checked&&state.source!=='upload'?state.notice?.version||null:null;});
$$('[data-inbox-filter]').forEach(button=>button.addEventListener('click',()=>{state.inboxFilter=button.dataset.inboxFilter;renderNotifications();}));
$('#refresh-inbox').addEventListener('click',loadNotifications);
bootstrapAuth();
