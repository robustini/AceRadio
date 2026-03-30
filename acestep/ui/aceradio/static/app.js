let lastRunning=false;
let applyTimer=null;
const qs=id=>document.getElementById(id);
const tr=(key, params={})=>window.AceRadioI18n?window.AceRadioI18n.t(key, params):key;
const setUiLanguage=lang=>window.AceRadioI18n?window.AceRadioI18n.setLanguage(lang):lang;
const getUiLanguagePreference=()=>window.AceRadioI18n&&typeof window.AceRadioI18n.getPreference==='function'?window.AceRadioI18n.getPreference():(window.AceRadioI18n&&typeof window.AceRadioI18n.getLanguage==='function'?window.AceRadioI18n.getLanguage():'auto');
const player=qs('player');
const playerB=qs('playerB');

var activeDeck='A';
try{ activeDeck=sessionStorage.getItem('aceradio_activeDeck')||'A'; }catch(_){ activeDeck='A'; }
let cfInterval=null;
let deckBTrackId=null;
let deckBLoaded=false;
let waveformUrlA='';
let waveformUrlB='';
let waveformDecodeCtx=null;
let refreshInFlight=false;
let deckBPromoting=false;
let deckAPromoting=false;
let manualCrossfadeCommitLock=false;
let autoCrossfadeActive=false;
let cfCancelGuardUntil=0;
let manualStopA=false;
let manualStopB=false;
let suppressCrossfaderAutoplay=false;
let lastCrossfaderValue=0;
const state={genres:new Set(), themes:new Set(), languages:new Set(['en']), generationSourceDirty:false};
let pendingCustomCatalogFile=null;
let currentTrackId=null;
let optionsCache={};
let localPreviewPaused=false;
let lastAuthoritativePlaybackElapsed=0;
let lastAuthoritativePlaybackTrackId='';
let uiHoldUntil={};
let pendingUiState={};
let hiddenSettingsState={};
const ADMIN_MONITOR_LEVEL_KEY='aceradio_adminPreviewMaster_v1';
let adminMonitorLevel=1;
let livePlaybackRate=1;
let playbackRateApplyTimer=null;
let currentSpeedUserDragging=false;
let currentSpeedLastTrackId='';
let currentSpeedResetGuardUntil=0;
let currentSpeedResetGuardTimer=null;
let previewResyncTimers=[];
function clamp01(value){ const n=Number(value); if(!Number.isFinite(n)) return 0; return Math.min(1, Math.max(0, n)); }
function previewGain(value){ return clamp01(value) * adminMonitorLevel; }
function normalizeMediaSrc(src){
  const raw=String(src||'').trim();
  if(!raw) return '';
  try{ return new URL(raw, window.location.href).href; }catch(_){ return raw; }
}
function audioSourceMatches(audio, url){
  if(!audio || !url) return false;
  const current=normalizeMediaSrc(audio.getAttribute('src') || audio.currentSrc || '');
  const target=normalizeMediaSrc(url);
  return !!current && current===target;
}
function getPreviewBaseGain(audio){
  if(!audio) return 0;
  const stored=Number(audio.dataset?.previewBaseGain);
  if(Number.isFinite(stored)) return clamp01(stored);
  const current=clamp01(Number(audio.volume||0));
  if(adminMonitorLevel<=0) return 0;
  return clamp01(current/adminMonitorLevel);
}
function setPreviewBaseGain(audio, value){
  if(!audio) return 0;
  const base=clamp01(value);
  if(audio.dataset) audio.dataset.previewBaseGain=String(base);
  audio.volume=previewGain(base);
  return base;
}
function refreshPreviewMonitorMix(){
  [player, playerB].forEach(audio=>{
    if(audio) setPreviewBaseGain(audio, getPreviewBaseGain(audio));
  });
  if(typeof playerJAdmin !== 'undefined' && playerJAdmin){
    const base=Number(playerJAdmin.dataset.baseVolume||playerJAdmin.dataset.previewBaseGain||playerJAdmin.volume||1);
    setPreviewBaseGain(playerJAdmin, base);
    if(playerJAdmin.dataset) playerJAdmin.dataset.baseVolume=String(clamp01(base));
  }
}
function formatMonitorDb(value){ return value<=0 ? '-∞ dB' : (20*Math.log10(value)).toFixed(1)+' dB'; }
function updateAdminMonitorUi(value){ const slider=qs('adminMonitorLevel'); const db=qs('adminMonitorDb'); const pct=qs('adminMonitorPct'); const next=clamp01(value); if(slider){ slider.value=String(next); slider.style.setProperty('--vol-pct',(next*100)+'%'); } if(db) db.textContent=formatMonitorDb(next); if(pct) pct.textContent=`${Math.round(next*100)}%`; }
function applyAdminMonitorLevel(value,{persist=true,refreshMix=true}={}){ adminMonitorLevel=clamp01(value); updateAdminMonitorUi(adminMonitorLevel); if(persist){ try{ localStorage.setItem(ADMIN_MONITOR_LEVEL_KEY, String(adminMonitorLevel)); }catch(_){} } if(!refreshMix) return; const cf=qs('crossfader'); suppressCrossfaderAutoplay=true; applyCrossfaderVolumes(parseFloat(cf?.value||0),{allowAutoplay:false}); suppressCrossfaderAutoplay=false; refreshPreviewMonitorMix(); }
function initAdminMonitorLevel(){ let stored=1; try{ const raw=localStorage.getItem(ADMIN_MONITOR_LEVEL_KEY); if(raw!=null&&raw!=='') stored=Number(raw); }catch(_){} adminMonitorLevel=clamp01(Number.isFinite(stored)?stored:1); updateAdminMonitorUi(adminMonitorLevel); }
function normalizePlaybackRate(value){ const n=Number(value); return Number.isFinite(n) ? Math.max(0.5, Math.min(2, n)) : 1; }
function sliderValueToPlaybackRate(value){ const raw=Number(value); const safe=Number.isFinite(raw) ? raw : 50; const v=Math.max(0, Math.min(100, safe)); return v<=50 ? 0.5 + (v/50)*0.5 : 1 + ((v-50)/50); }
function playbackRateToSliderValue(value){ const rate=normalizePlaybackRate(value); return rate<=1 ? Math.round(((rate-0.5)/0.5)*50) : Math.round(50+((rate-1)/1)*50); }
function applyPitchPreservedRate(audio, value){ if(!audio) return 1; const rate=normalizePlaybackRate(value); try{ audio.preservesPitch=true; }catch(_){} try{ audio.mozPreservesPitch=true; }catch(_){} try{ audio.webkitPreservesPitch=true; }catch(_){} audio.playbackRate=rate; return rate; }
function effectiveBpmText(track, rate){ const bpm=Number(track?.bpm||0); if(!Number.isFinite(bpm) || bpm<=0) return '—'; return `${Math.round(bpm * normalizePlaybackRate(rate))} BPM`; }
function renderPlaybackSpeedControl(rate, trackId=''){ const slider=qs('currentSpeedSlider'); const valueEl=qs('currentSpeedValue'); const resetBtn=qs('currentSpeedResetBtn'); const wrap=qs('currentSpeedSliderWrap'); if(!slider || !valueEl) return; const normalized=normalizePlaybackRate(rate); if(!currentSpeedUserDragging) slider.value=String(playbackRateToSliderValue(normalized)); const pctText=`${Math.round(normalized*100)}%`; valueEl.textContent=pctText; valueEl.classList.toggle('shifted', Math.abs(normalized-1)>=0.001); if(resetBtn) resetBtn.disabled=Math.abs(normalized-1)<0.001 || currentSpeedUserDragging || Date.now()<currentSpeedResetGuardUntil; slider.setAttribute('aria-valuenow', String(Math.round(normalized*100))); slider.setAttribute('aria-valuetext', pctText); if(wrap) wrap.dataset.rate=String(normalized); currentSpeedLastTrackId=String(trackId||''); }
function setValidateButtonState(kind='idle', label=tr('common.check_streaming')){ const btn=qs('streamValidateBtn'); if(!btn) return; btn.classList.remove('ok','error','working'); btn.textContent=label; if(kind==='ok') btn.classList.add('ok'); else if(kind==='error') btn.classList.add('error'); else if(kind==='working') btn.classList.add('working'); }
function resetValidateButtonState(){ setValidateButtonState('idle',tr('common.check_streaming')); }
function clearPreviewResyncTimers(){ while(previewResyncTimers.length){ clearTimeout(previewResyncTimers.pop()); } }
function schedulePreviewResync(expectedTrackId, targetDeck){
  clearPreviewResyncTimers();
  [220, 900].forEach(delay=>{
    const timer=setTimeout(async()=>{
      if(localPreviewPaused) return;
      try{
        const resolved=await waitForBackendResumePlayback(5, 140);
        const activeCurrent=resolved?.activeCurrent||null;
        const activeId=String(activeCurrent?.id||'');
        if(expectedTrackId && activeId && activeId!==String(expectedTrackId)) return;
        const targetAudio=targetDeck==='B' ? playerB : player;
        if(!targetAudio || !activeCurrent) return;
        seekAudioToSyncPoint(targetAudio, resolved.syncPoint);
        if(targetAudio.paused) ensurePreviewAudioPlaying(targetAudio).catch(()=>{});
      }catch(_){ }
    }, delay);
    previewResyncTimers.push(timer);
  });
}
function renderOperationalPanels(data){
  const modifiers=data?.playback_modifiers||{};
  const transition=data?.transition_state||{};
  const reservoir=data?.reservoir_state||{};
  const backend=data?.backend_health||{};
  const events=Array.isArray(data?.ops_events)?data.ops_events:[];
  const headline=qs('opsHeadline');
  if(headline) headline.textContent=modifiers.active ? 'Playout is running with active playback modifiers' : 'Standard playout';
  function setPill(id,cls,text){
    const el=document.getElementById(id);
    if(!el)return;
    el.className='ops-hero-pill ops-state-pill '+cls;
    el.textContent=text;
  }
  const healthClass=backend.healthy ? 'ok' : (backend.degraded ? 'error' : (backend.fallback_mode ? 'warn' : 'idle'));
  const healthText=backend.healthy ? 'healthy' : (backend.degraded ? 'degraded' : (backend.fallback_mode ? 'fallback' : 'idle'));
  setPill('opsHeroHealthPill',healthClass,healthText);
  const listEl=document.getElementById('opsHeroListeners');
  if(listEl) listEl.textContent=Math.max(0,Number(data?.listener_count||0));
  setPill('opsHeroRuntime',backend.runtime_active ? 'ok' : 'idle',backend.runtime_active ? 'active' : 'idle');
  const authorityText=backend.authority_source==='playout' ? 'backend' : (backend.authority_source==='runtime' ? 'runtime' : 'idle');
  const authorityClass=backend.authority_source==='playout' ? 'ok' : (backend.authority_source==='runtime' ? 'warn' : 'idle');
  setPill('opsHeroAuthority',authorityClass,authorityText);
  const syncClass=backend.snapshot_fresh ? (backend.stale ? 'warn' : 'ok') : 'warn';
  const syncText=backend.snapshot_fresh ? (backend.stale ? 'stale' : 'fresh') : 'waiting';
  setPill('opsHeroSync',syncClass,syncText);
  const childClass=backend.playout_child_active ? 'ok' : (backend.child_alive ? 'warn' : 'idle');
  const childText=backend.playout_child_active ? 'active' : (backend.child_alive ? 'standby' : 'offline');
  setPill('opsHeroChild',childClass,childText);
  const activeModifiers=[];
  if(Number(modifiers.transition_cut_seconds||0)>0) activeModifiers.push(`<div class="ops-line"><strong>Auto transition cut</strong><span>${Math.round(Number(modifiers.transition_cut_seconds||0))} s</span></div>`);
  if(Number(modifiers.separator_before_end_seconds||0)>0) activeModifiers.push(`<div class="ops-line"><strong>Separator lead</strong><span>${Number(modifiers.separator_before_end_seconds||0).toFixed(1)} s</span></div>`);
  if(modifiers.speed_active) activeModifiers.push(`<div class="ops-line"><strong>Speed</strong><span>${Math.round(Number(modifiers.speed_percent||100))}%</span></div>`);
  const modifiersBox=qs('playbackModifiersBox');
  if(modifiersBox) modifiersBox.innerHTML=activeModifiers.length ? activeModifiers.join('') : '<div class="ops-empty">No active modifiers.</div>';
  const nextDeck=activeDeck==='A' ? 'B' : 'A';
  const transitionLines=[];
  if(transition.current_track_title) transitionLines.push(`<div class="ops-line"><strong>Live now</strong><span>${transition.current_track_title}</span></div>`);
  if(transition.active_jingle) transitionLines.push(`<div class="ops-line"><strong>Jingle active</strong><span>${String(transition.active_jingle_mode||'').toUpperCase()} · ${transition.active_jingle}</span></div>`);
  if(transition.queued_separator) transitionLines.push(`<div class="ops-line"><strong>Queued separator</strong><span>${transition.queued_separator}</span></div>`);
  transitionLines.push(`<div class="ops-line"><strong>Next deck</strong><span>${nextDeck}</span></div>`);
  if(transition.next_track_title) transitionLines.push(`<div class="ops-line"><strong>Next track</strong><span>${transition.next_track_title}</span></div>`);
  if(transition.remaining_to_cut_seconds!=null) transitionLines.push(`<div class="ops-line"><strong>Cut in</strong><span>${Math.max(0,Number(transition.remaining_to_cut_seconds||0)).toFixed(1)} s</span></div>`);
  if(transition.playback_rate_percent && Number(transition.playback_rate_percent)!==100) transitionLines.push(`<div class="ops-line"><strong>Speed</strong><span>${Math.round(Number(transition.playback_rate_percent||100))}%</span></div>`);
  const transitionBox=qs('transitionStatusBox');
  if(transitionBox) transitionBox.innerHTML=transitionLines.length ? transitionLines.join('') : '<div class="ops-empty">No transition activity yet.</div>';
  const reservoirLines=[
    `<div class="ops-line"><strong>Prepared</strong><span>${Number(reservoir.prepared_tracks||0)} / ${Number(reservoir.reservoir_target||0)}</span></div>`,
    `<div class="ops-line"><strong>Preparing now</strong><span>${Number(reservoir.preparing_tracks||0)}</span></div>`,
    `<div class="ops-line"><strong>Refill threshold</strong><span>${Number(reservoir.refill_threshold||0)}</span></div>`,
    `<div class="ops-line"><strong>Cache pool</strong><span>${Number(reservoir.cache_pool_ready||0)}</span></div>`,
    `<div class="ops-line"><strong>Replenishment</strong><span>${String(reservoir.replenishment_state||'idle')}</span></div>`
  ];
  if(reservoir.last_refill_reason) reservoirLines.push(`<div class="ops-note">${reservoir.last_refill_reason}</div>`);
  if(reservoir.last_generation_action) reservoirLines.push(`<div class="ops-note">${reservoir.last_generation_action}</div>`);
  const reservoirBox=qs('reservoirStateBox');
  if(reservoirBox) reservoirBox.innerHTML=reservoirLines.join('');
  const backendNotes=[];
  if(backend.runtime_active && backend.authority_source!=='playout') backendNotes.push('<div class="ops-note">Radio on air — timing driven by live runtime, authoritative playout snapshot not active.</div>');
  if(backend.stale_reason) backendNotes.push(`<div class="ops-note">${backend.stale_reason}</div>`);
  if(backend.last_error) backendNotes.push(`<div class="ops-note ops-note-error">${backend.last_error}</div>`);
  const backendBox=qs('backendHealthBox');
  const backendCard=document.getElementById('opsBackendNotesCard');
  if(backendBox) backendBox.innerHTML=backendNotes.join('');
  if(backendCard) backendCard.style.display=backendNotes.length ? '' : 'none';
  const eventsBox=qs('opsEventLogBox');
  if(eventsBox){
    if(!events.length){
      eventsBox.innerHTML='<div class="ops-empty">No backend playout events yet.</div>';
    } else {
      eventsBox.innerHTML=events.slice().reverse().map(ev=>`<div class="ops-log-entry ${String(ev.level||'info')}"><span class="ops-log-time">${new Date(Number(ev.ts||0)*1000).toLocaleTimeString()}</span><div class="ops-log-main"><strong>${String(ev.title||'Event')}</strong><span>${String(ev.detail||'')}</span></div></div>`).join('');
    }
  }
}

function normalizeBitrateKbps(value){
  if(value==null||value===''||value===0)return 0;
  if(typeof value==='number'&&Number.isFinite(value)) return value>0?Math.round(value):0;
  const raw=String(value).trim().toLowerCase();
  const m=raw.match(/(\d+(?:[.,]\d+)?)/);
  if(!m)return 0;
  let num=Number(String(m[1]).replace(',', '.'));
  if(!Number.isFinite(num)||num<=0)return 0;
  if(raw.includes('mb')) num*=1000;
  return Math.round(num);
}

function normalizeSampleRateHz(value){
  if(value==null||value===''||value===0)return 0;
  if(typeof value==='number'&&Number.isFinite(value)) return value>0?Math.round(value):0;
  const raw=String(value).trim().toLowerCase();
  const m=raw.match(/(\d+(?:[.,]\d+)?)/);
  if(!m)return 0;
  let num=Number(String(m[1]).replace(',', '.'));
  if(!Number.isFinite(num)||num<=0)return 0;
  if(raw.includes('khz')||/\bk\b/.test(raw)||num<1000) num*=1000;
  return Math.round(num);
}

function formatSampleRateLabel(hz){
  return hz ? `${(hz/1000).toFixed(hz % 1000 === 0 ? 0 : 1)} kHz` : '';
}

function formatGenreThemeMeta(track){
  const genre=String(track?.genre||track?.prompt?.genre||track?.prompt?.style||'').trim();
  const theme=String(track?.theme||track?.prompt?.theme||'').trim();
  if(genre && theme) return `Genre: ${genre} · Theme: ${theme}`;
  if(genre) return `Genre: ${genre}`;
  if(theme) return `Theme: ${theme}`;
  return '';
}
function escapeHtml(value){
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function cleanTrackCaption(text, track){
  let value=String(text||'').replace(/\s+/g,' ').trim().replace(/^[,;|\-\s]+|[,;|\-\s]+$/g,'');
  if(!value) return '';
  const title=String(track?.song_title||track?.prompt?.song_title||'').trim().toLowerCase();
  const genre=String(track?.genre||track?.prompt?.genre||track?.prompt?.style||'').trim().toLowerCase();
  const parts=value.split('|').map(x=>x.trim()).filter(Boolean);
  if(parts.length){
    if(title && parts[0]?.toLowerCase()===title) parts.shift();
    if(genre && parts[0]?.toLowerCase()===genre) parts.shift();
    value=parts.join(' | ').trim() || value;
  }
  const metaText=formatGenreThemeMeta(track).toLowerCase();
  if(value.toLowerCase()===metaText) return '';
  return value;
}
function buildPromptCaption(track){
  const prompt=track?.prompt||{};
  const pieces=[prompt.instruments,prompt.mood,prompt.vocal_style,prompt.production]
    .map(x=>String(x||'').replace(/\s+/g,' ').trim())
    .filter(Boolean);
  const out=[];
  const seen=new Set();
  for(const piece of pieces){
    const lower=piece.toLowerCase();
    if(seen.has(lower)) continue;
    seen.add(lower);
    out.push(piece);
  }
  return out.join(', ');
}
function getTrackCaption(track){
  return cleanTrackCaption(track?.caption || track?.prompt?.caption || buildPromptCaption(track) || '', track);
}
function getDisplayTrackTags(track){
  const explicit=formatGenreThemeMeta(track);
  if(explicit) return explicit;
  const raw=String(track?.tags||'').replace(/\s+/g,' ').trim();
  if(!raw) return '';
  let text=raw;
  if(text.includes('|')){
    const parts=text.split('|').map(x=>x.trim()).filter(Boolean);
    if(parts.length>=2) text=parts.slice(1).join(' | ').trim();
  }
  const ignored=new Set();
  const remember=value=>{
    const v=String(value||'').trim().toLowerCase();
    if(v) ignored.add(v);
  };
  remember(track?.song_title);
  remember(track?.lora_id);
  remember(track?.lora_label);
  String(track?.lora_label||'').split(/[\/|]+/).forEach(remember);
  const parts=text.split(',').map(x=>x.trim()).filter(Boolean);
  const cleaned=[];
  const seen=new Set();
  for(const part of parts.length?parts:[text]){
    const lower=part.toLowerCase();
    if(!lower) continue;
    if(ignored.has(lower)) continue;
    if(/^(?:ollama|ai_generated|file|cache|both)\s+radio\s+generation$/i.test(lower)) continue;
    if(/^(?:generated|imported|cached|mixed)(?:\s+radio\s+generation)?$/i.test(lower)) continue;
    if(lower==='radio generation') continue;
    if(seen.has(lower)) continue;
    seen.add(lower);
    cleaned.push(part);
  }
  return (cleaned.join(', ') || text).trim();
}

function markUiDirty(key, value, holdMs=2500){
  uiHoldUntil[key]=Date.now()+holdMs;
  pendingUiState[key]=value;
}
function consumeUiDirty(key, value){
  const pending=pendingUiState[key];
  if(pending===undefined) return;
  const same=(typeof pending==='boolean') ? (!!pending===!!value) : String(pending||'')===String(value||'');
  if(same){
    delete pendingUiState[key];
    delete uiHoldUntil[key];
  }
}
function shouldRespectBackendValue(key){
  return !(uiHoldUntil[key] && Date.now() < uiHoldUntil[key]);
}

function setCrossfadeTimer(text=''){
  const value=String(text||'');
  const src=qs('cfTimer');
  const dst=qs('cfTimerInline');
  if(src) src.textContent=value;
  if(dst) dst.textContent=value;
}

function updateMonitorMuteState(){
  const muted=!!qs('monitorMuted')?.checked;
  if(player) player.muted=muted;
  if(playerB) playerB.muted=muted;

  if(typeof playerJAdmin !== 'undefined' && playerJAdmin) playerJAdmin.muted=muted;
  if(vuAudio){
    if(muted && vuAudio.state==='running') vuAudio.suspend().catch(()=>{});
    if(!muted && vuAudio.state==='suspended') vuAudio.resume().catch(()=>{});
  }
  const btn=qs('monitorMuteBtn');
  if(btn){
    btn.classList.toggle('muted', muted);
    btn.setAttribute('aria-pressed', muted ? 'true' : 'false');
    const icon=btn.querySelector('.monitor-toggle-icon');
    const label=btn.querySelector('.monitor-toggle-label');
    if(icon) icon.textContent=muted ? '🔇' : '🔊';
    if(label) label.textContent=muted ? 'LOCAL MUTED' : 'LOCAL ON';
  }
}

function setDeckVisualState(deck){
  const isB=deck==='B';
  activeDeck=isB?'B':'A';
  try{ sessionStorage.setItem('aceradio_activeDeck', activeDeck); }catch(_){}
  const deckA=qs('deckA');
  const deckB=qs('deckB');
  const badgeA=qs('deckABadge');
  const badgeB=qs('deckBBadge');
  const label=qs('deckActiveLabel');
  const indA=qs('cfIndA');
  const indB=qs('cfIndB');
  if(deckA) deckA.classList.toggle('active-deck', !isB);
  if(deckB) deckB.classList.toggle('active-deck', isB);
  if(badgeA){
    badgeA.textContent=!isB?'ON AIR':'CUE';
    badgeA.className=!isB?'deck-badge on':'deck-badge cued';
  }
  if(badgeB){
    badgeB.textContent=isB?'ON AIR':(deckBLoaded?'CUE':'—');
    badgeB.className=isB?'deck-badge on':(deckBLoaded?'deck-badge cued':'deck-badge off');
  }
  if(label) label.textContent=isB?'DECK B ON AIR':'DECK A ON AIR';
  if(indA) indA.classList.toggle('active', !isB);
  if(indB) indB.classList.toggle('active', isB);
}
function getDisplayedLyrics(track){
  if(track?.instrumental) return 'Instrumental — no lyrics';
  return String(track?.lyrics||'').trim();
}
function renderDeckSlot(deck, track){
  const isB=deck==='B';
  renderTrack(qs(isB?'nextMeta':'currentMeta'), track||null);
  const lyricsEl=qs(isB?'lyricsB':'lyrics');
  if(lyricsEl) lyricsEl.textContent=getDisplayedLyrics(track);
  const dlBtn=qs(isB?'deckBDownloadBtn':'deckADownloadBtn');
  if(dlBtn){
    dlBtn.disabled=!(track&&track.id);
    dlBtn.dataset.trackId=String(track?.id||'');
  }
  drawWaveform(deck, track?.audio_url||'');
  genAlbumArt(isB?'albumArtB':'albumArtA', track||null);
}
function downloadDeckTrack(deck){
  const btn=qs(deck==='B' ? 'deckBDownloadBtn' : 'deckADownloadBtn');
  const trackId=String(btn?.dataset?.trackId||'');
  if(!trackId) return;
  const a=document.createElement('a');
  a.href=`/api/radio/download/${trackId}`;
  a.download='';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
function makePlaybackSyncPoint(elapsed=0, receivedAt=null){
  return {
    elapsed: Math.max(0, Number(elapsed||0)),
    receivedAt: Number.isFinite(receivedAt) ? Number(receivedAt) : performance.now(),
  };
}
function makeFrozenPlaybackSyncPoint(elapsed=0){
  return {
    elapsed: Math.max(0, Number(elapsed||0)),
    receivedAt: performance.now(),
    frozen: true,
  };
}
function resolvePlaybackSyncPoint(syncPoint){
  if(syncPoint && typeof syncPoint==='object'){
    const base=Math.max(0, Number(syncPoint.elapsed||0));
    if(syncPoint.frozen) return base;
    const receivedAt=Number.isFinite(syncPoint.receivedAt) ? Number(syncPoint.receivedAt) : performance.now();
    return Math.max(0, base + Math.max(0, (performance.now()-receivedAt)/1000));
  }
  return Math.max(0, Number(syncPoint||0));
}
function isBackendPlaybackAuthoritative(data){
  const playout=data?.playout||{};
  return !!(data?.playback_authoritative && !playout?.stale && playout?.child_alive!==false && playout?.snapshot_fresh!==false);
}
function resolveBackendPlaybackElapsed(data, fallbackElapsed=0){
  const playout=data?.playout||{};
  const trackId=String(data?.current_track?.id||'');
  const playoutTrackId=String(playout?.current_track_id||'');
  const statusElapsed=Math.max(0, Number(data?.playback_elapsed ?? fallbackElapsed ?? 0));
  const playoutElapsed=Number(playout?.track_elapsed);
  const playoutFreshForCurrent=!!(
    trackId &&
    playoutTrackId &&
    trackId===playoutTrackId &&
    Number.isFinite(playoutElapsed) &&
    !playout?.stale &&
    playout?.child_alive!==false &&
    playout?.snapshot_fresh!==false
  );
  if(playoutFreshForCurrent){
    return Math.max(0, playoutElapsed);
  }
  return statusElapsed;
}
function makeBackendPlaybackSyncPoint(data, receivedAt, fallbackElapsed=0){
  const playout=data?.playout||{};
  const trackId=String(data?.current_track?.id||'');
  const elapsed=resolveBackendPlaybackElapsed(data, fallbackElapsed);
  if(isBackendPlaybackAuthoritative(data)){
    lastAuthoritativePlaybackTrackId=trackId;
    lastAuthoritativePlaybackElapsed=elapsed;
    return makePlaybackSyncPoint(elapsed, receivedAt);
  }
  const backendRunning=!!(
    trackId &&
    Number.isFinite(elapsed) &&
    elapsed >= 0 &&
    (
      data?.running ||
      playout?.running ||
      data?.radio_state==='playing' ||
      data?.radio_state==='on_air'
    )
  );
  if(backendRunning){
    return makePlaybackSyncPoint(elapsed, receivedAt);
  }
  const frozenElapsed=(trackId && trackId===lastAuthoritativePlaybackTrackId)
    ? lastAuthoritativePlaybackElapsed
    : elapsed;
  return makeFrozenPlaybackSyncPoint(frozenElapsed);
}
function extractBackendResumePlayback(data, receivedAt){
  const playout=data?.playout||{};
  const activeCurrent=data?.current_track||null;
  const activeTrackId=String(activeCurrent?.id||'');
  const playoutTrackId=String(playout?.current_track_id||'');
  const playoutElapsed=Number(playout?.track_elapsed);
  const statusElapsed=Number(data?.playback_elapsed);
  if(!activeCurrent || !activeTrackId){
    return {ok:false, reason:'No active live track is available from the backend yet.'};
  }
  if(isBackendPlaybackAuthoritative(data)){
    if(!playoutTrackId || playoutTrackId!==activeTrackId){
      return {ok:false, reason:'Playout track does not match the current live track yet.'};
    }
    if(!Number.isFinite(playoutElapsed) || playoutElapsed < 0){
      return {ok:false, reason:'Playout elapsed time is not valid yet.'};
    }
    lastAuthoritativePlaybackTrackId=activeTrackId;
    lastAuthoritativePlaybackElapsed=Math.max(0, playoutElapsed);
    return {
      ok:true,
      data,
      activeCurrent,
      elapsed:lastAuthoritativePlaybackElapsed,
      syncPoint:makePlaybackSyncPoint(lastAuthoritativePlaybackElapsed, receivedAt),
      authoritative:true,
    };
  }
  const serverElapsed=Math.max(0, Number.isFinite(statusElapsed) ? statusElapsed : NaN);
  const backendClockUsable=Number.isFinite(serverElapsed) && (
    (playoutTrackId && playoutTrackId===activeTrackId) ||
    data?.running ||
    playout?.running ||
    data?.radio_state==='playing' ||
    data?.radio_state==='on_air'
  );
  if(backendClockUsable){
    return {
      ok:true,
      data,
      activeCurrent,
      elapsed:serverElapsed,
      syncPoint:makePlaybackSyncPoint(serverElapsed, receivedAt),
      authoritative:false,
      degraded:true,
    };
  }
  return {ok:false, reason:'Live playback point is not ready from the backend yet.'};
}
async function waitForBackendResumePlayback(maxAttempts=16, retryDelayMs=220){
  let lastReason='Live playback point is not ready from the backend yet.';
  let previousSample=null;
  for(let attempt=0; attempt<maxAttempts; attempt++){
    const statusResult=await api('/api/radio/status').then(data=>({data, receivedAt:performance.now()}));
    const statusData=statusResult.data||{};
    const resolved=extractBackendResumePlayback(statusData, statusResult.receivedAt);
    if(resolved.ok) return resolved;
    const currentTrack=statusData.current_track||null;
    const currentTrackId=String(currentTrack?.id||'');
    const currentElapsed=Number(statusData.playback_elapsed);
    const backendRunning=!!(
      statusData?.running ||
      statusData?.playout?.running ||
      statusData?.radio_state==='playing' ||
      statusData?.radio_state==='on_air'
    );
    if(currentTrackId && Number.isFinite(currentElapsed) && currentElapsed>=0 && backendRunning){
      const prev=previousSample;
      const sameTrack=!!(prev && prev.trackId===currentTrackId);
      const progressed=!!(sameTrack && Number.isFinite(prev.elapsed) && currentElapsed >= prev.elapsed && (currentElapsed - prev.elapsed) >= 0.05);
      const stableSameTrack=!!(sameTrack && Number.isFinite(prev.elapsed) && currentElapsed >= prev.elapsed);
      if(progressed || currentElapsed > 1.0 || (stableSameTrack && currentElapsed >= 0.25)){
        return {
          ok:true,
          data:statusData,
          activeCurrent:currentTrack,
          elapsed:Math.max(0,currentElapsed),
          syncPoint:makePlaybackSyncPoint(Math.max(0,currentElapsed), statusResult.receivedAt),
          authoritative:false,
          degraded:true,
        };
      }
      previousSample={trackId:currentTrackId, elapsed:currentElapsed, receivedAt:statusResult.receivedAt};
    }
    lastReason=resolved.reason || lastReason;
    if(attempt < maxAttempts-1){
      await new Promise(resolve=>setTimeout(resolve, retryDelayMs));
    }
  }
  throw new Error(lastReason);
}
function seekAudioToSyncPoint(audio, syncPoint){
  const seekTo=resolvePlaybackSyncPoint(syncPoint);
  if(!(seekTo>0)) return 0;
  try{ audio.currentTime=Math.min(seekTo, Math.max(0,(audio.duration||seekTo)-0.25)); }catch(_){ }
  return seekTo;
}

async function ensurePreviewAudioPlaying(audio, attempts=4, delayMs=180){
  let lastError=null;
  if(!audio || !audio.getAttribute('src')) return false;
  for(let i=0;i<attempts;i++){
    try{
      const maybePromise=audio.play();
      if(maybePromise && typeof maybePromise.then==='function') await maybePromise;
      if(!audio.paused) return true;
    }catch(err){
      lastError=err;
    }
    await new Promise(resolve=>setTimeout(resolve, delayMs));
  }
  if(lastError) throw lastError;
  return !audio.paused;
}

function playAudioFromSyncPoint(audio, syncPoint){
  if(!audio) return;
  let done=false;
  const resume=()=>{
    if(done || !audio) return;
    done=true;
    seekAudioToSyncPoint(audio, syncPoint);
    if(audio.paused) ensurePreviewAudioPlaying(audio).catch(()=>{});
  };
  if(audio.readyState>=1){
    resume();
    return;
  }
  audio.addEventListener('loadedmetadata', resume, {once:true});
  audio.addEventListener('canplay', resume, {once:true});
  setTimeout(resume, 1500);
}
function updateStopPreviewButton(){
  const btn=qs('stopBtn');
  if(!btn) return;
  btn.textContent=localPreviewPaused ? '▶ Resume play' : '■ Stop preview';
  btn.title=localPreviewPaused
    ? 'Resume only the local admin preview and reattach it to the current live position.'
    : 'Pause only the local admin preview in this browser. Live radio and stream keep running.';
}
function stopLocalAdminJinglePreview(){
  if(typeof jfAdminCancelFade === 'function') jfAdminCancelFade();
  const deckPlayer = typeof jfAdminActiveDeck === 'function' ? jfAdminActiveDeck() : null;
  const restoreGain = Number.isFinite(typeof jfAdminPreDuckVol === 'number' ? jfAdminPreDuckVol : NaN) ? jfAdminPreDuckVol : null;
  if(typeof playerJAdmin !== 'undefined' && playerJAdmin){
    playerJAdmin.pause();
    playerJAdmin.removeAttribute('src');
    playerJAdmin.load();
  }
  if(deckPlayer && restoreGain!=null){
    setPreviewBaseGain(deckPlayer, restoreGain);
  }
  if(typeof jfAdminStopHold === 'function') jfAdminStopHold();
  if(typeof jfAdminReset === 'function') jfAdminReset();
}
function pauseLocalPreviewOnly(){
  clearPreviewResyncTimers();
  localPreviewPaused=true;
  manualStopA=true;
  manualStopB=true;
  if(player && !player.paused) player.pause();
  if(playerB && !playerB.paused) playerB.pause();
  stopLocalAdminJinglePreview();
  updateStopPreviewButton();
}
async function resumeLocalPreviewFromLive(){
  const resolved=await waitForBackendResumePlayback();
  const data=resolved.data||{};
  const liveSyncPoint=resolved.syncPoint;
  const activeCurrent=resolved.activeCurrent||null;
  const preparedNext=data.next_track||null;
  const queuedReserve=(data.reservoir && data.reservoir.length) ? data.reservoir[0] : null;

  const currentMatchesDeckA=!!(
    activeCurrent &&
    currentTrackId &&
    activeCurrent.id===currentTrackId &&
    audioSourceMatches(player, activeCurrent.audio_url)
  );
  const currentMatchesDeckB=!!(
    activeCurrent &&
    deckBTrackId &&
    activeCurrent.id===deckBTrackId &&
    audioSourceMatches(playerB, activeCurrent.audio_url)
  );
  const targetDeck=currentMatchesDeckB ? 'B' : (currentMatchesDeckA ? 'A' : (activeDeck==='B' ? 'B' : 'A'));

  clearPreviewResyncTimers();
  manualStopA=false;
  manualStopB=false;
  localPreviewPaused=false;
  stopLocalAdminJinglePreview();

  const targetAudio = targetDeck==='B' ? playerB : player;
  const targetAlreadyLoaded = targetDeck==='B' ? currentMatchesDeckB : currentMatchesDeckA;

  if(player && !player.paused && player!==targetAudio){ player.pause(); }
  if(playerB && !playerB.paused && playerB!==targetAudio){ playerB.pause(); }

  if(targetAlreadyLoaded && targetAudio && activeCurrent){
    seekAudioToSyncPoint(targetAudio, liveSyncPoint);
    if(targetAudio.paused) ensurePreviewAudioPlaying(targetAudio).catch(()=>{});
    schedulePreviewResync(activeCurrent.id, targetDeck);
    if(targetDeck==='B'){
      loadDeckAudio('A', preparedNext || queuedReserve, false, 0);
      setDeckVisualState('B');
      qs('crossfader').value=1;
    }else{
      loadDeckAudio('B', preparedNext, false, 0);
      setDeckVisualState('A');
      qs('crossfader').value=0;
      reinitVU();
      document.title=`${activeCurrent.song_title} · AceRadio V1.0`;
      qs('bpmDisplay').textContent=effectiveBpmText(activeCurrent, livePlaybackRate);
    }
  }else if(targetDeck==='B'){
    deckBTrackId=null;
    loadDeckAudio('B', activeCurrent, true, liveSyncPoint, true);
    schedulePreviewResync(activeCurrent?.id||'', 'B');
    loadDeckAudio('A', preparedNext || queuedReserve, false, 0, true);
    setDeckVisualState('B');
    qs('crossfader').value=1;
  }else if(activeCurrent){
    currentTrackId=null;
    loadDeckAudio('A', activeCurrent, true, liveSyncPoint, true);
    schedulePreviewResync(activeCurrent?.id||'', 'A');
    loadDeckAudio('B', preparedNext, false, 0, true);
    setDeckVisualState('A');
    qs('crossfader').value=0;
    reinitVU();
    document.title=`${activeCurrent.song_title} · AceRadio V1.0`;
    qs('bpmDisplay').textContent=effectiveBpmText(activeCurrent, livePlaybackRate);
  }else if(preparedNext || queuedReserve){
    loadDeckAudio('A', preparedNext || null, false, 0, true);
    loadDeckAudio('B', queuedReserve && preparedNext && queuedReserve.id!==preparedNext.id ? queuedReserve : null, false, 0, true);
    setDeckVisualState('A');
    qs('crossfader').value=0;
  }else{
    loadDeckAudio('A', null, false, 0);
    loadDeckAudio('B', null, false, 0);
  }

  suppressCrossfaderAutoplay=true;
  applyCrossfaderVolumes(parseFloat(qs('crossfader').value||0),{allowAutoplay:false});
  suppressCrossfaderAutoplay=false;
  if(targetAudio && activeCurrent){
    playAudioFromSyncPoint(targetAudio, liveSyncPoint);
  }
  refreshPreviewMonitorMix();
  window._jdLastStatus = data;
  updateStopPreviewButton();
}
async function resumeDeckPreviewFromLive(deck){
  const resolved=await waitForBackendResumePlayback();
  const data=resolved.data||{};
  const activeCurrent=resolved.activeCurrent||null;
  const syncPoint=resolved.syncPoint;
  const isB=deck==='B';
  const audio=isB ? playerB : player;
  const otherAudio=isB ? player : playerB;
  const trackId=isB ? deckBTrackId : currentTrackId;
  const matchesLive=!!(
    activeCurrent &&
    trackId &&
    activeCurrent.id===trackId &&
    audioSourceMatches(audio, activeCurrent.audio_url)
  );
  if(!matchesLive || !audio) return false;
  if(isB) manualStopB=false;
  else manualStopA=false;
  localPreviewPaused=false;
  stopLocalAdminJinglePreview();
  if(otherAudio && !otherAudio.paused) otherAudio.pause();
  setDeckVisualState(isB ? 'B' : 'A');
  qs('crossfader').value=isB ? 1 : 0;
  suppressCrossfaderAutoplay=true;
  applyCrossfaderVolumes(parseFloat(qs('crossfader').value||0),{allowAutoplay:false});
  suppressCrossfaderAutoplay=false;
  seekAudioToSyncPoint(audio, syncPoint);
  if(audio.paused) await ensurePreviewAudioPlaying(audio).catch(()=>{});
  schedulePreviewResync(activeCurrent.id, deck);
  if(!isB){
    reinitVU();
    document.title=`${activeCurrent.song_title} · AceRadio V1.0`;
    qs('bpmDisplay').textContent=effectiveBpmText(activeCurrent, livePlaybackRate);
  }
  window._jdLastStatus = data;
  refreshPreviewMonitorMix();
  updateStopPreviewButton();
  return true;
}
function loadDeckAudio(deck, track, autoplay=false, seekTo=0, forceReload=false){
  const isB=deck==='B';
  const audio=isB?playerB:player;
  const volEl=qs(isB?'deckBVol':'deckAVol');
  const manualStopped=isB?manualStopB:manualStopA;
  if(!track){
    if(isB){ deckBTrackId=null; deckBLoaded=false; waveformUrlB=''; manualStopB=false; }
    else { currentTrackId=null; waveformUrlA=''; manualStopA=false; }
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    applyPitchPreservedRate(audio, livePlaybackRate);
    renderDeckSlot(deck, null);
    return;
  }
  const prevId=isB?deckBTrackId:currentTrackId;
  const nextId=track.id||null;
  const changed=!!forceReload || prevId!==nextId || audio.getAttribute('src')!==track.audio_url;
  const syncPoint=(seekTo&&typeof seekTo==='object') ? seekTo : (Number(seekTo||0)>0 ? makePlaybackSyncPoint(seekTo) : null);
  const targetSyncTime=resolvePlaybackSyncPoint(syncPoint);
  const hasSyncPoint=targetSyncTime>0.25;
  if(isB){ deckBTrackId=nextId; deckBLoaded=true; if(changed) manualStopB=false; }
  else { currentTrackId=nextId; if(changed) manualStopA=false; }
  renderDeckSlot(deck, track);
  applyPitchPreservedRate(audio, livePlaybackRate);
  if(changed){
    const wasPaused=audio.paused;
    const prevTime=audio.currentTime||0;
    let syncApplied=false;
    audio.pause();
    audio.src=track.audio_url;
    audio.load();
    applyPitchPreservedRate(audio, livePlaybackRate);
    if(hasSyncPoint){
      const syncAndMaybePlay=()=>{
        if(syncApplied || audio.readyState<1) return;
        syncApplied=true;
        seekAudioToSyncPoint(audio, syncPoint);
        if(autoplay && audio.paused && !manualStopped){ ensurePreviewAudioPlaying(audio).catch(()=>{}); }
      };
      audio.addEventListener('loadedmetadata',syncAndMaybePlay,{once:true});
      audio.addEventListener('canplay',syncAndMaybePlay,{once:true});
      setTimeout(syncAndMaybePlay,5000);
    } else if(!wasPaused && prevId===nextId && prevTime>0){
      audio.addEventListener('loadedmetadata',()=>{ try{ audio.currentTime=Math.min(prevTime, Math.max(0,(audio.duration||prevTime)-0.25)); }catch(_){ } },{once:true});
    }
  } else if(hasSyncPoint && autoplay && !manualStopped){
    const currentTime=Math.max(0, Number(audio.currentTime||0));
    const syncDrift=Math.abs(currentTime-targetSyncTime);
    if(currentTime < 1 || syncDrift > 2){
      seekAudioToSyncPoint(audio, syncPoint);
      if(audio.paused) ensurePreviewAudioPlaying(audio).catch(()=>{});
    }
  }

  if(!(typeof jfAdminActive !== 'undefined' && jfAdminActive)){
    setPreviewBaseGain(audio, Number(volEl?.value||1));
  }
  if(autoplay && audio.paused && !manualStopped && !(changed && hasSyncPoint)){ ensurePreviewAudioPlaying(audio).catch(()=>{}); }
}

let bootstrapReady=false;
let bootstrapPhase='booting';
let streamLogEntries=[];
const streamLogSeen=new Set();
const ADMIN_LOG_STORAGE_KEY='aceradio_adminRuntimeLog_v1';
const ADMIN_HARD_REFRESH_STORAGE_KEY='aceradio_adminHardRefreshPending_v1';
let adminLogEntries=[];
const adminLogSeen=new Set();
let lastRuntimeErrorSeen='';
function fmtStamp(ts){ try{ const d=new Date(Number(ts)*1000); return Number.isFinite(d.getTime())?d.toLocaleString():String(ts); }catch(_){ return String(ts); } }
function formatLogLines(lines){ return (lines||[]).filter(Boolean).map(x=>String(x)).join('\n'); }
function persistAdminLog(){ try{ localStorage.setItem(ADMIN_LOG_STORAGE_KEY, JSON.stringify(adminLogEntries.slice(-300))); }catch(_){ } }
function loadAdminLog(){ try{ const raw=localStorage.getItem(ADMIN_LOG_STORAGE_KEY); const parsed=JSON.parse(raw||'[]'); adminLogEntries=Array.isArray(parsed)?parsed.slice(-300):[]; adminLogEntries.forEach(entry=>{ const key=entry?.dedupeKey||''; if(key) adminLogSeen.add(key); }); }catch(_){ adminLogEntries=[]; } }
function getNavigationType(){
  try{
    const entries=performance.getEntriesByType?.('navigation');
    if(entries && entries.length && entries[0] && entries[0].type){
      return String(entries[0].type||'').trim().toLowerCase();
    }
  }catch(_){ }
  try{
    const nav=performance?.navigation;
    if(nav){
      if(nav.type===1) return 'reload';
      if(nav.type===2) return 'back_forward';
      if(nav.type===0) return 'navigate';
    }
  }catch(_){ }
  return '';
}
function consumePendingHardRefresh(){
  let pending=false;
  try{
    pending=sessionStorage.getItem(ADMIN_HARD_REFRESH_STORAGE_KEY)==='1';
    if(pending) sessionStorage.removeItem(ADMIN_HARD_REFRESH_STORAGE_KEY);
  }catch(_){ }
  try{
    const url=new URL(window.location.href);
    if(url.searchParams.has('_hard_reload')){
      url.searchParams.delete('_hard_reload');
      window.history.replaceState({}, document.title, url.toString());
    }
  }catch(_){ }
  return pending;
}
async function restoreAdminPreviewAfterHardRefresh(){
  if(localPreviewPaused) return false;
  try{
    await resumeLocalPreviewFromLive();
    return true;
  }catch(_){
    return false;
  }
}
async function hardRefreshAdminPage(){
  try{
    if(window.caches && caches.keys){
      const keys=await caches.keys();
      await Promise.all(keys.map(key=>caches.delete(key).catch(()=>false)));
    }
  }catch(_){ }
  try{ sessionStorage.clear(); }catch(_){ }
  try{ sessionStorage.setItem(ADMIN_HARD_REFRESH_STORAGE_KEY,'1'); }catch(_){ }
  try{ localStorage.removeItem(ADMIN_LOG_STORAGE_KEY); }catch(_){ }
  const url=new URL(window.location.href);
  url.searchParams.set('_hard_reload', String(Date.now()));
  window.location.replace(url.toString());
}

function renderAdminLog(){ const box=qs('adminLogBox'); if(!box) return; if(!adminLogEntries.length){ box.value=tr('common.admin_errors_log'); box.className='stream-log-box empty'; return; } box.className='stream-log-box'; box.value=adminLogEntries.map(entry=>`[${entry.stamp}] ${entry.title}\n${entry.body}`).join('\n\n------------------------------\n\n'); box.scrollTop=box.scrollHeight; }
function appendAdminLog(kind, title, lines, dedupeKey=''){ const body=formatLogLines(Array.isArray(lines)?lines:[lines]); if(!body) return; const key=dedupeKey||''; if(key && adminLogSeen.has(key)) return; if(key) adminLogSeen.add(key); adminLogEntries.push({kind:kind||'error', title:title||tr('common.admin_runtime_event'), body, stamp:new Date().toLocaleString(), dedupeKey:key}); if(adminLogEntries.length>300) adminLogEntries=adminLogEntries.slice(-300); persistAdminLog(); renderAdminLog(); }
function clearAdminLog(){ adminLogEntries=[]; adminLogSeen.clear(); lastRuntimeErrorSeen=''; try{ localStorage.removeItem(ADMIN_LOG_STORAGE_KEY); }catch(_){ } renderAdminLog(); }
function noteRuntimeError(message, source=tr('common.radio_runtime_error')){ const msg=String(message||'').trim(); if(!msg) return; appendAdminLog('error', source, [msg], `${source}|${msg}`); lastRuntimeErrorSeen=msg; }
function setSettingsStatus(message, kind='info'){ const el=qs('settingsStatus'); if(el){ el.textContent=message||''; el.className=`settings-status-block ${kind}`.trim(); } if(kind==='error' && message){ appendAdminLog('error','Admin panel message',[String(message)], `settings-error|${String(message)}`); } }
function updateSettingsPathDisplay(path=''){ const el=qs('settingsPath'); if(el) el.textContent=path||''; }
function renderBootstrap(bootstrap){ const ov=qs('bootstrapOverlay'); const badge=qs('bootstrapBadge'); if(!bootstrap) return; bootstrapReady=!!bootstrap.ready; bootstrapPhase=bootstrap.phase||'booting'; if(badge) badge.textContent=bootstrapReady?'ready':bootstrapPhase; const msg=bootstrap.error || bootstrap.message || bootstrapPhase; const msgEl=qs('bootstrapMessage'); const phaseEl=qs('bootstrapPhase'); if(msgEl) msgEl.textContent=msg; if(phaseEl) phaseEl.textContent=bootstrapPhase; ['startBtn','skipBtn','stopBtn','saveSettingsBtn','saveAsSettingsBtn','browseSettingsBtn','customCatalogBrowseBtn','customCatalogApplyBtn','customCatalogRemoveBtn','streamBtn','streamValidateBtn'].forEach(id=>{ const b=qs(id); if(b) b.disabled=!bootstrapReady; }); if(ov) ov.style.display=bootstrapReady?'none':'flex'; updateStopPreviewButton(); }
async function pollBootstrap(){ const data=await api('/api/bootstrap_status'); renderBootstrap(data); if(data?.error){ throw new Error(data.error); } return data||{ready:false, phase:'booting'}; }
function api(path, options={}){ const controller=new AbortController(); const timeoutMs=Number(options.timeoutMs||45000); const timer=timeoutMs>0?setTimeout(()=>controller.abort(new DOMException('Request timeout','AbortError')), timeoutMs):null; const headers={'Content-Type':'application/json', ...(options.headers||{})}; const fetchOptions={...options, headers, signal: options.signal||controller.signal, cache: options.cache||'no-store'}; delete fetchOptions.timeoutMs; return fetch(path, fetchOptions).then(async res=>{ if(res.status===401){ const body=await res.json().catch(()=>({})); if(body.auth_required||body.detail==='Unauthorized'){ window.location.href='/login'; throw new Error(tr('common.session_expired')); } throw new Error(await res.text()||`HTTP ${res.status}`); } if(!res.ok) throw new Error(await res.text()||`HTTP ${res.status}`); const ct=res.headers.get('content-type')||''; return ct.includes('application/json')?res.json():res.text(); }).catch(err=>{ if(err?.name==='AbortError') throw new Error(`Request timeout for ${path}`); throw err; }).finally(()=>{ if(timer) clearTimeout(timer); }); }
function uniq(arr){ return [...new Set((arr||[]).filter(Boolean))]; }

function customCatalogOverrideActive(){ return !!hiddenSettingsState.custom_catalog_enabled; }
function customCatalogState(){ return { active: !!hiddenSettingsState.custom_catalog_enabled, file: String(hiddenSettingsState.custom_catalog_file||''), name: String(hiddenSettingsState.custom_catalog_name||''), songCount: Number(hiddenSettingsState.custom_catalog_song_count||0), ignoredCount: Number(hiddenSettingsState.custom_catalog_ignored_count||0) }; }
async function apiMultipart(path, formData){ const res = await fetch(path, { method:'POST', body:formData, cache:'no-store' }); if(!res.ok){ const txt = await res.text().catch(()=>''); throw new Error(txt || `HTTP ${res.status}`); } const ct=res.headers.get('content-type')||''; return ct.includes('application/json') ? res.json() : res.text(); }
function setDisabled(el, disabled){ if(!el) return; el.disabled=!!disabled; el.setAttribute('aria-disabled', disabled ? 'true' : 'false'); if(disabled) el.tabIndex=-1; else el.removeAttribute('tabindex'); }
function setFieldLocked(el, locked){ const wrap=el?.closest?.('.field, .field-chip-block'); if(!wrap) return; wrap.classList.toggle('is-locked', !!locked); }
function updatePromptOverrideUI(){ const locked=customCatalogOverrideActive(); ['languageRotationMode','generationMode','catalogSource','generationSourceBothPercent'].forEach(id=>{ const el=qs(id); setDisabled(el, locked); setFieldLocked(el, locked); }); ['genreSelectAllBtn','genreClearBtn','themeSelectAllBtn','themeClearBtn'].forEach(id=>setDisabled(qs(id), locked)); ['genreChips','themeChips','languageChips'].forEach(id=>{ const el=qs(id); const wrap=el?.closest?.('.field, .field-chip-block'); if(wrap) wrap.classList.toggle('is-locked', !!locked); }); makeChips('genreChips', optionsCache.default_genres||[], state.genres); makeChips('themeChips', optionsCache.default_themes||[], state.themes); makeChips('languageChips', uniq(optionsCache.valid_languages||['en','it']), state.languages); }
function updateCustomCatalogUI(){ const current=customCatalogState(); const pathEl=qs('customCatalogPath'); const infoEl=qs('customCatalogInfo'); const applyBtn=qs('customCatalogApplyBtn'); const removeBtn=qs('customCatalogRemoveBtn'); const browseBtn=qs('customCatalogBrowseBtn'); const pendingName=pendingCustomCatalogFile ? String(pendingCustomCatalogFile.name||'').trim() : ''; const hasPending=!!pendingName; if(pathEl){ pathEl.value=pendingName || current.name || current.file || ''; pathEl.placeholder=current.active ? tr('common.custom_catalog_active_short') : tr('common.no_custom_catalog_loaded'); pathEl.title=(pendingCustomCatalogFile && pendingCustomCatalogFile.path) ? String(pendingCustomCatalogFile.path) : String(current.file||''); } if(applyBtn){ applyBtn.hidden=!hasPending; applyBtn.disabled=!bootstrapReady || !hasPending; } if(removeBtn){ removeBtn.hidden=!current.active; removeBtn.disabled=!bootstrapReady || !current.active; } if(browseBtn) browseBtn.disabled=!bootstrapReady; if(infoEl){ if(hasPending){ const pendingIgnored=Number(pendingCustomCatalogFile.ignoredCount||0); const pendingSongs=Number(pendingCustomCatalogFile.songCount||0); const pendingMeta=pendingSongs>0 ? ` (${pendingSongs} ${tr(pendingSongs===1?'common.song':'common.songs')}${pendingIgnored>0?`, ${tr('common.custom_catalog_ignored_ok',{count:pendingIgnored}).replace(/^\s*·\s*/,'')}`:''})` : ''; infoEl.textContent=current.active ? tr('common.custom_catalog_pending_replace',{name:pendingName,meta:pendingMeta}) : tr('common.custom_catalog_pending_activate',{name:pendingName,meta:pendingMeta}); infoEl.className='settings-status-block'; } else if(current.active){ const ignored=current.ignoredCount>0 ? tr('common.custom_catalog_ignored_ok',{count:current.ignoredCount}) : ''; infoEl.textContent=tr('common.custom_catalog_active_info',{count:current.songCount,name:current.name||tr('common.selected_catalog'),ignored}); infoEl.className='settings-status-block ok'; } else { infoEl.textContent=tr('common.no_custom_catalog_active'); infoEl.className='settings-status-block'; } } updatePromptOverrideUI(); }

function modelNameLower(modelName){
  return String(modelName||'').trim().toLowerCase();
}
function isSftModelName(modelName){
  return modelNameLower(modelName).includes('sft');
}
function isBaseModelName(modelName){
  return modelNameLower(modelName).includes('base');
}
function isTurboModelName(modelName){
  const name=modelNameLower(modelName);
  return !!name && name.includes('turbo') && !name.includes('sft');
}
function modelUsesShiftOne(modelName){
  return isSftModelName(modelName) || isBaseModelName(modelName);
}
function resolveAutoShift(modelName){
  return modelUsesShiftOne(modelName) ? 1 : 3;
}
function modelInventory(){
  if(Array.isArray(optionsCache?.dit_model_inventory)) return optionsCache.dit_model_inventory;
  if(Array.isArray(optionsCache?.engine?.models)) return optionsCache.engine.models;
  return [];
}
function modelInventoryEntry(modelName){
  const key=String(modelName||'').trim();
  return modelInventory().find(item=>String(item?.name||'').trim()===key)||null;
}
function getModelStepLimit(modelName){
  const key=String(modelName||'').trim();
  const fromOptions=Number(optionsCache?.engine?.model_limits?.[key]?.max_inference_steps);
  if(Number.isFinite(fromOptions) && fromOptions>0) return fromOptions;
  if(isSftModelName(modelName)) return 200;
  if(isBaseModelName(modelName)) return 200;
  return 20;
}
function resolveAutoInferenceSteps(modelName){
  if(isSftModelName(modelName)) return 50;
  if(isBaseModelName(modelName)) return 32;
  return 8;
}
function setManualOverrideFlag(el, manual){
  if(!el) return;
  el.dataset.manualOverride = manual ? 'true' : 'false';
}
function hasManualOverride(el){
  return !!el && el.dataset.manualOverride === 'true';
}
function applyResolvedFieldValue(el, value, autoValue){
  if(!el || value==null) return;
  const numericValue=Number(value);
  const numericAuto=Number(autoValue);
  const hasFiniteValue=Number.isFinite(numericValue);
  const hasFiniteAuto=Number.isFinite(numericAuto);
  el.value=String(value);
  setManualOverrideFlag(el, !(hasFiniteValue && hasFiniteAuto && numericValue===numericAuto));
}
function syncInferenceStepsWithSelectedModel(force=false){
  const modelEl=qs('model');
  const stepsEl=qs('inferenceSteps');
  const hintEl=qs('stepsAutoHint');
  if(!modelEl || !stepsEl) return;
  const resolved=resolveAutoInferenceSteps(modelEl.value);
  const maxSteps=getModelStepLimit(modelEl.value);
  const raw=String(stepsEl.value||'').trim();
  stepsEl.max=String(maxSteps);
  if(force || !raw || !Number.isFinite(Number(raw)) || !hasManualOverride(stepsEl)){
    stepsEl.value=String(resolved);
    setManualOverrideFlag(stepsEl, false);
  } else {
    const parsed=Math.round(Number(raw));
    const clamped=Math.max(1, Math.min(maxSteps, parsed));
    stepsEl.value=String(clamped);
    setManualOverrideFlag(stepsEl, clamped!==resolved);
  }
  stepsEl.dataset.autoResolved=String(resolved);
  if(hintEl){
    hintEl.textContent=isSftModelName(modelEl.value)
      ? 'Auto: 50 for SFT DiT models.'
      : isBaseModelName(modelEl.value)
        ? 'Auto: 32 for Base DiT models.'
        : 'Auto: 8 for Turbo/other DiT models.';
  }
}
function syncShiftWithSelectedModel(force=false){
  const modelEl=qs('model');
  const shiftEl=qs('shift');
  const hintEl=qs('shiftAutoHint');
  if(!modelEl || !shiftEl) return;
  const resolved=resolveAutoShift(modelEl.value);
  const raw=String(shiftEl.value||'').trim();
  if(force || !raw || !Number.isFinite(Number(raw)) || !hasManualOverride(shiftEl)){
    shiftEl.value=String(resolved);
    setManualOverrideFlag(shiftEl, false);
  }
  shiftEl.dataset.autoResolved=String(resolved);
  if(hintEl){
    hintEl.textContent=resolved===1
      ? tr('common.shift_auto_hint_base_sft')
      : tr('common.shift_auto_hint_turbo');
  }
}
function normalizeLoras(raw){ if(Array.isArray(raw)) return raw; if(Array.isArray(raw?.items)) return raw.items; if(Array.isArray(raw?.loras)) return raw.loras; if(raw&&typeof raw==='object') return Object.values(raw); return []; }
function selectionLimit(containerId){ return 0; }
function clampSelection(selectedSet, limit){ if(limit>0 && selectedSet.size>limit){ [...selectedSet].slice(limit).forEach(v=>selectedSet.delete(v)); } }
function makeChips(containerId, values, selectedSet){ const el=qs(containerId); if(!el) return; el.innerHTML=''; const isLanguage=containerId==='languageChips'; const limit=selectionLimit(containerId); const locked=customCatalogOverrideActive() && ['genreChips','themeChips','languageChips'].includes(containerId); clampSelection(selectedSet, limit); values.forEach(v=>{ const b=document.createElement('button'); b.type='button'; b.className='chip'+(selectedSet.has(v)?' active':''); b.textContent=isLanguage ? String(v).toUpperCase() : v; b.title=isLanguage ? String(v).toUpperCase() : String(v); b.disabled=locked; b.onclick=()=>{ if(locked) return; if(selectedSet.has(v)) selectedSet.delete(v); else { if(limit>0 && selectedSet.size>=limit) return; selectedSet.add(v); } makeChips(containerId, values, selectedSet); scheduleLiveApply(); }; el.appendChild(b); }); }
function selectAllGenres(){ if(customCatalogOverrideActive()) return; (optionsCache.default_genres||[]).forEach(g=>state.genres.add(g)); makeChips('genreChips', optionsCache.default_genres||[], state.genres); scheduleLiveApply(); }
function clearAllGenres(){ if(customCatalogOverrideActive()) return; state.genres.clear(); makeChips('genreChips', optionsCache.default_genres||[], state.genres); scheduleLiveApply(); }
function selectAllThemes(){ if(customCatalogOverrideActive()) return; state.themes=new Set(optionsCache.default_themes||[]); makeChips('themeChips', optionsCache.default_themes||[], state.themes); scheduleLiveApply(); }
function clearAllThemes(){ if(customCatalogOverrideActive()) return; state.themes.clear(); makeChips('themeChips', optionsCache.default_themes||[], state.themes); scheduleLiveApply(); }
function clampLoraWeightValue(value, fallback=0.6){ const n=Number(value); if(!Number.isFinite(n)) return fallback; return Math.max(0, Math.min(2, Math.round(n*100)/100)); }
function findLoraRow(id){ return [...document.querySelectorAll('.lora-item[data-lora-row-id]')].find(row=>row.dataset.loraRowId===String(id||''))||null; }
function getLoraAdvancedKeys(){ return ['self_attn','cross_attn','ffn']; }
function getLoraAdvancedInput(row, key){ return row?.querySelector(`[data-lora-adv="${key}"]`)||null; }
function getLoraAdvancedButton(row, key){ return row?.querySelector(`[data-lora-link="${key}"]`)||null; }
function getLoraMainWeightValue(row){ const input=row?.querySelector('[data-lora-weight]'); return clampLoraWeightValue(input?.value, 0.6); }
function getLoraAdvancedFieldLabel(key){ return key==='self_attn' ? tr('common.lora_self_attn') : key==='cross_attn' ? tr('common.lora_cross_attn') : tr('common.lora_ffn'); }
function getLoraAdvancedSummaryLabel(key){ return key==='self_attn' ? tr('common.lora_summary_self') : key==='cross_attn' ? tr('common.lora_summary_cross') : tr('common.lora_summary_ffn'); }
function isLoraAdvancedFollowing(row, key){ return getLoraAdvancedInput(row, key)?.dataset.followMain!=='0'; }
function loraAdvancedSummaryText(row){ const overrides=getLoraAdvancedKeys().filter(key=>!isLoraAdvancedFollowing(row,key)).map(key=>getLoraAdvancedSummaryLabel(key)); if(!overrides.length) return tr('common.lora_follow_main'); return overrides.length===1 ? tr('common.lora_summary_override_one',{label:overrides[0]}) : tr('common.lora_summary_override_many',{labels:overrides.join(' · ')}); }
function syncLoraAdvancedControl(row, key){ if(!row) return; const main=getLoraMainWeightValue(row); const input=getLoraAdvancedInput(row, key); const button=getLoraAdvancedButton(row, key); if(!input || !button) return; const follow=isLoraAdvancedFollowing(row, key); const value=follow ? main : clampLoraWeightValue(input.value, main); input.value=String(value.toFixed(2)); button.textContent=follow ? tr('common.lora_follow') : tr('common.lora_override'); button.classList.toggle('is-linked', follow); button.classList.toggle('is-override', !follow); }
function refreshLoraRow(row){ if(!row) return; getLoraAdvancedKeys().forEach(key=>syncLoraAdvancedControl(row, key)); const summary=row.querySelector('[data-lora-advanced-summary]'); if(summary) summary.textContent=loraAdvancedSummaryText(row); const details=row.querySelector('[data-lora-advanced]'); if(details && getLoraAdvancedKeys().some(key=>!isLoraAdvancedFollowing(row, key))) details.open=true; }
function setLoraAdvancedFollow(row, key, follow){ const input=getLoraAdvancedInput(row, key); if(!input) return; input.dataset.followMain=follow ? '1' : '0'; if(follow) input.value=String(getLoraMainWeightValue(row).toFixed(2)); refreshLoraRow(row); }
function applyLoraEntryToRow(entry){ const row=findLoraRow(entry?.id); if(!row) return; const checkbox=row.querySelector('[data-lora-id]'); const main=row.querySelector('[data-lora-weight]'); if(checkbox) checkbox.checked=!!entry?.enabled; if(main) main.value=String(clampLoraWeightValue(entry?.weight, 0.6).toFixed(2)); getLoraAdvancedKeys().forEach(key=>{ const input=getLoraAdvancedInput(row, key); if(!input) return; const field=key==='self_attn' ? 'weight_self_attn' : key==='cross_attn' ? 'weight_cross_attn' : 'weight_ffn'; const follow=entry?.[field]==null; input.dataset.followMain=follow ? '1' : '0'; input.value=String(clampLoraWeightValue(follow ? entry?.weight : entry?.[field], 0.6).toFixed(2)); }); refreshLoraRow(row); }
function renderLoras(items){ const el=qs('loraList'); const previous=new Map(collectLoras().map(item=>[item.id,item])); el.innerHTML=''; items.forEach(item=>{ const id=item.id||item.key||item.name||item.filename; if(!id) return; const label=(item.label||item.name||id); const row=document.createElement('div'); row.className='lora-item'; row.dataset.loraRowId=String(id); row.innerHTML=`<div class="lora-top"><label class="lora-main"><input class="lora-check" type="checkbox" data-lora-id="${escapeHtml(id)}"><span class="lora-name" title="${escapeHtml(label)}">${escapeHtml(label)}</span></label><div class="lora-main-weight"><span class="lora-mini-label">${escapeHtml(tr('common.lora_main_weight'))}</span><input type="number" min="0" max="2" step="0.05" value="0.60" data-lora-weight="${escapeHtml(id)}"></div></div><details class="lora-advanced" data-lora-advanced><summary><span>${escapeHtml(tr('common.lora_advanced'))}</span><span class="lora-advanced-summary" data-lora-advanced-summary>${escapeHtml(tr('common.lora_follow_main'))}</span></summary><div class="lora-advanced-grid"><div class="lora-advanced-field"><span class="lora-mini-label">${escapeHtml(getLoraAdvancedFieldLabel('self_attn'))}</span><div class="lora-advanced-inputs"><input type="number" min="0" max="2" step="0.05" value="0.60" data-lora-adv="self_attn" data-lora-adv-id="${escapeHtml(id)}"><button type="button" class="lora-link-toggle is-linked" data-lora-link="self_attn" data-lora-link-id="${escapeHtml(id)}">${escapeHtml(tr('common.lora_follow'))}</button></div></div><div class="lora-advanced-field"><span class="lora-mini-label">${escapeHtml(getLoraAdvancedFieldLabel('cross_attn'))}</span><div class="lora-advanced-inputs"><input type="number" min="0" max="2" step="0.05" value="0.60" data-lora-adv="cross_attn" data-lora-adv-id="${escapeHtml(id)}"><button type="button" class="lora-link-toggle is-linked" data-lora-link="cross_attn" data-lora-link-id="${escapeHtml(id)}">${escapeHtml(tr('common.lora_follow'))}</button></div></div><div class="lora-advanced-field"><span class="lora-mini-label">${escapeHtml(getLoraAdvancedFieldLabel('ffn'))}</span><div class="lora-advanced-inputs"><input type="number" min="0" max="2" step="0.05" value="0.60" data-lora-adv="ffn" data-lora-adv-id="${escapeHtml(id)}"><button type="button" class="lora-link-toggle is-linked" data-lora-link="ffn" data-lora-link-id="${escapeHtml(id)}">${escapeHtml(tr('common.lora_follow'))}</button></div></div></div></details>`; el.appendChild(row); const snapshot=previous.get(String(id))||{id:String(id),weight:0.6,weight_self_attn:null,weight_cross_attn:null,weight_ffn:null,enabled:false}; applyLoraEntryToRow(snapshot); }); }
function collectLoras(){ return [...document.querySelectorAll('.lora-item[data-lora-row-id]')].map(row=>{ const checkbox=row.querySelector('[data-lora-id]'); const id=String(row.dataset.loraRowId||'').trim(); if(!id) return null; const main=getLoraMainWeightValue(row); const selfAttnInput=getLoraAdvancedInput(row,'self_attn'); const crossAttnInput=getLoraAdvancedInput(row,'cross_attn'); const ffnInput=getLoraAdvancedInput(row,'ffn'); return {id, weight:main, weight_self_attn:isLoraAdvancedFollowing(row,'self_attn') ? null : clampLoraWeightValue(selfAttnInput?.value, main), weight_cross_attn:isLoraAdvancedFollowing(row,'cross_attn') ? null : clampLoraWeightValue(crossAttnInput?.value, main), weight_ffn:isLoraAdvancedFollowing(row,'ffn') ? null : clampLoraWeightValue(ffnInput?.value, main), enabled:!!checkbox?.checked}; }).filter(Boolean); }

async function promoteDeckBToAir(refreshAfter=true){
  if(deckBPromoting) return;
  if(!deckBLoaded || !deckBTrackId) return;
  deckBPromoting=true;
  try{
    manualStopA=false;
    manualStopB=false;
    player.pause();

    if(!(typeof jfAdminActive !== 'undefined' && jfAdminActive)){
      setPreviewBaseGain(playerB, parseFloat(qs('deckBVol').value||1));
    }
    if(playerB.paused) await ensurePreviewAudioPlaying(playerB).catch(()=>{});
    setDeckVisualState('B');
    qs('crossfader').value=1;
    suppressCrossfaderAutoplay=true;
    applyCrossfaderVolumes(1,{allowAutoplay:false});
    suppressCrossfaderAutoplay=false;
    await api('/api/radio/track-ended',{method:'POST',body:JSON.stringify({track_id:currentTrackId||''}),headers:{'Content-Type':'application/json'}}).catch(()=>{});
    if(refreshAfter) await refresh();
  } finally {
    deckBPromoting=false;
  }
}
async function promoteDeckAToAir(refreshAfter=true){
  if(deckAPromoting) return;
  if(!player.src) return;
  deckAPromoting=true;
  try{
    manualStopA=false;
    manualStopB=false;
    playerB.pause();

    if(!(typeof jfAdminActive !== 'undefined' && jfAdminActive)){
      setPreviewBaseGain(player, parseFloat(qs('deckAVol').value||1));
    }
    if(player.paused && player.src) await ensurePreviewAudioPlaying(player).catch(()=>{});
    setDeckVisualState('A');
    qs('crossfader').value=0;
    suppressCrossfaderAutoplay=true;
    applyCrossfaderVolumes(0,{allowAutoplay:false});
    suppressCrossfaderAutoplay=false;
    await api('/api/radio/track-ended',{method:'POST',body:JSON.stringify({track_id:deckBTrackId||''}),headers:{'Content-Type':'application/json'}}).catch(()=>{});
    if(refreshAfter) await refresh();
  } finally {
    deckAPromoting=false;
  }
}

function hasDeckATrack(){
  return !!(player && player.getAttribute('src'));
}
function hasDeckBTrack(){
  return !!(deckBLoaded && deckBTrackId && playerB && playerB.getAttribute('src'));
}
function bothDecksReadyForCrossfade(){
  return hasDeckATrack() && hasDeckBTrack();
}
function deckIsStopped(deck){
  return deck==='B' ? manualStopB : manualStopA;
}
function setDeckStopped(deck, stopped=true){
  if(deck==='B') manualStopB=!!stopped;
  else manualStopA=!!stopped;
}
function applyCrossfaderVolumes(v, opts={}){
  if(!bothDecksReadyForCrossfade()){
    syncCrossfaderToActiveDeck();
    return;
  }

  if(typeof jfAdminActive !== 'undefined' && jfAdminActive) return;
  const volA=parseFloat(qs('deckAVol').value||1);
  const volB=parseFloat(qs('deckBVol').value||1);
  setPreviewBaseGain(player, Math.max(0,1-v)*volA);
  setPreviewBaseGain(playerB, Math.max(0,v)*volB);
  if(v<=0.001 && playerB.volume===0 && !playerB.paused) playerB.pause();
  if(v>=0.999 && player.volume===0 && !player.paused) player.pause();
  qs('cfIndA').classList.toggle('active', v<0.5);
  qs('cfIndB').classList.toggle('active', v>=0.5);
}

function maybeStartCrossfadeTarget(prevV, nextV){
  if(!bothDecksReadyForCrossfade()) return;
  if(activeDeck==='A' && prevV<0.5 && nextV>=0.5 && deckBLoaded && deckBTrackId && playerB.src && playerB.paused && !manualStopB){
    ensurePreviewAudioPlaying(playerB).catch(()=>{});
    return;
  }
  if(activeDeck==='B' && prevV>0.5 && nextV<=0.5 && player.src && player.paused && !manualStopA){
    ensurePreviewAudioPlaying(player).catch(()=>{});
  }
}
function syncCrossfaderToActiveDeck(){
  const cf=qs('crossfader');
  if(!cf) return;
  cf.value=activeDeck==='B'?1:0;
  lastCrossfaderValue=parseFloat(cf.value||0);

  if(typeof jfAdminActive !== 'undefined' && jfAdminActive) return;
  const volA=parseFloat(qs('deckAVol').value||1);
  const volB=parseFloat(qs('deckBVol').value||1);
  setPreviewBaseGain(player, (activeDeck==='B'?0:1)*volA);
  setPreviewBaseGain(playerB, (activeDeck==='B'?1:0)*volB);
  if(activeDeck==='A' && playerB && !playerB.paused) playerB.pause();
  if(activeDeck==='B' && player && !player.paused) player.pause();
  qs('cfIndA').classList.toggle('active', activeDeck!=='B');
  qs('cfIndB').classList.toggle('active', activeDeck==='B');
}
async function commitManualCrossfader(v){
  if(manualCrossfadeCommitLock || deckAPromoting || deckBPromoting) return;
  if(!bothDecksReadyForCrossfade()) return;
  if(activeDeck==='A' && v>=0.98 && deckBLoaded && deckBTrackId){
    manualCrossfadeCommitLock=true;
    try{ await promoteDeckBToAir(true); } finally { manualCrossfadeCommitLock=false; }
    return;
  }
  if(activeDeck==='B' && v<=0.02 && player.src){
    manualCrossfadeCommitLock=true;
    try{ await promoteDeckAToAir(true); } finally { manualCrossfadeCommitLock=false; }
  }
}

function getTrackSourceBadges(track){
  const rawSrc = (track?.source || 'ai_generated')+'';
  let displaySrc = ((track?.display_source || track?.prompt?.display_source || (rawSrc === 'cache' ? (track?.prompt?.source || 'ai_generated') : rawSrc))+'').toLowerCase();
  const labelMap = {library:tr('common.library'), ai_catalog:tr('common.ai_catalog'), ai_generated:tr('common.ai_generated'), mixed:tr('common.mixed'), cached:tr('common.cached'), custom_catalog:tr('common.custom_catalog_source'), file:tr('common.library'), both:tr('common.mixed'), ollama:tr('common.ai_generated'), cache:tr('common.cached')};
  if(track?.instrumental && (displaySrc === 'library' || displaySrc === 'ai_catalog' || displaySrc === 'mixed' || displaySrc === 'file' || displaySrc === 'both')) displaySrc = 'ai_generated';
  if(displaySrc === 'file') displaySrc = 'library';
  if(displaySrc === 'ollama') displaySrc = 'ai_generated';
  if(displaySrc === 'cache') displaySrc = 'cached';
  if(displaySrc === 'both') displaySrc = 'mixed';
  const primaryLabel = labelMap[displaySrc] || track?.display_source_label || tr('common.ai_generated');
  const primaryClass = displaySrc === 'library' ? 'library' : (displaySrc === 'ai_catalog' || displaySrc === 'custom_catalog') ? 'ai-catalog' : displaySrc === 'mixed' ? 'mixed' : displaySrc === 'cached' ? 'cache' : 'ai-generated';
  const badges = [`<span class="tag-source ${primaryClass}">${primaryLabel}</span>`];
  if(rawSrc.toLowerCase() === 'cache') badges.push(`<span class="tag-source cache">${tr('common.cached')}</span>`);
  return { rawSrc, displaySrc, primaryLabel, primaryClass, badgesHtml: badges.join('') };
}

function renderTrack(target, track){
  if(!track){
    target.innerHTML=`<span class="deck-meta-empty">${tr('common.waiting_for_track')}</span>`;
    const deck=target.closest('.deck');
    if(deck) deck.querySelectorAll('.waveform-wrap').forEach(w=>w.classList.add('empty'));
    return;
  }
  const deck=target.closest('.deck');
  if(deck) deck.querySelectorAll('.waveform-wrap').forEach(w=>w.classList.remove('empty'));

  const sourceInfo = getTrackSourceBadges(track);

  const loraName = track.lora_label || track.lora_id || '';
  const lora = loraName ? `<span class="tag-lora">LoRA · ${loraName}</span>` : '';

  const fmtBytes = b => b>1048576?(b/1048576).toFixed(1)+' MB':b>1024?(b/1024).toFixed(0)+' KB':'';
  const specParts = [];
  const bitrateKbps = normalizeBitrateKbps(track.bitrate_kbps ?? track.prompt?.bitrate_kbps ?? track.prompt?.export_applied?.applied_bitrate_kbps ?? track.prompt?.export_applied?.before_bitrate_kbps ?? track.prompt?.mp3_bitrate);
  const sampleRateHz = normalizeSampleRateHz(track.sample_rate_hz ?? track.prompt?.sample_rate_hz ?? track.prompt?.export_applied?.applied_sample_rate ?? track.prompt?.export_applied?.before_sample_rate ?? track.prompt?.mp3_sample_rate);
  if(track.audio_format) specParts.push(`<span class="deck-spec-chip">${track.audio_format.toUpperCase()}</span>`);
  if(bitrateKbps) specParts.push(`<span class="deck-spec-chip">${bitrateKbps} kbps</span>`);
  if(sampleRateHz) specParts.push(`<span class="deck-spec-chip">${formatSampleRateLabel(sampleRateHz)}</span>`);
  if(track.audio_size_bytes) { const s=fmtBytes(track.audio_size_bytes); if(s) specParts.push(`<span class="deck-spec-chip">${s}</span>`); }
  specParts.push(`<span class="deck-spec-chip vote-chip">❤ ${Math.max(0, Number(track.vote_count||0))}</span>`);
  const specs = specParts.length ? `<div class="track-specs">${specParts.join('')}</div>` : '';

  const metaText=formatGenreThemeMeta(track);
  const captionText=getTrackCaption(track);
  const desc = `<div class="track-desc">${escapeHtml(captionText) || '&nbsp;'}</div>`;
  const genreTheme = `<div class="track-genre-theme">${escapeHtml(metaText) || '&nbsp;'}</div>`;

  const techParts = [];
  if(track.lora_label||track.lora_id) techParts.push(`<span class="tag-lora">LoRA · ${track.lora_label||track.lora_id}</span>`);
  if(track.seed) techParts.push(`<span class="tag-tech">Seed · ${track.seed}</span>`);
  if(track.duration) techParts.push(`<span class="tag-tech">${track.duration}s</span>`);
  if(track.prompt?.model) techParts.push(`<span class="tag-tech">${String(track.prompt.model).replace('acestep-v15-','v1.5-').replace('acestep-v1-','v1-')}</span>`);
  if(track.prompt?.inference_steps) techParts.push(`<span class="tag-tech">${track.prompt.inference_steps} steps</span>`);
  if(track.prompt?.infer_method) techParts.push(`<span class="tag-tech">${(track.prompt.infer_method||'').toUpperCase()}</span>`);
  if(track.prompt?.guidance_scale) techParts.push(`<span class="tag-tech">CFG ${track.prompt.guidance_scale}</span>`);
  if(track.prompt?.shift&&track.prompt.shift!==3) techParts.push(`<span class="tag-tech">shift ${track.prompt.shift}</span>`);
  const techRow = techParts.length ? `<div class="track-tech-row">${techParts.join('')}</div>` : '';

  target.innerHTML =
    `<div class="track-title">${track.song_title || 'Untitled'}</div>` +
    `<div class="track-metaline">` +
      `<span>${(track.language || '?').toUpperCase()}</span>` +
      `<span>·</span>` +
      `<span>${track.bpm || '—'} BPM</span>` +
      `<span>·</span>` +
      `<span>${track.key_scale || '—'}</span>` +
      `<span>·</span>` +
      `<span>${track.instrumental ? 'Instrumental' : 'Vocal'}</span>` +
      `${sourceInfo.badgesHtml}` +
    `</div>` +
    genreTheme +
    desc +
    specs +
    `<div class="track-tags">${techRow}</div>` +
    `<div class="deck-meta-spacer" aria-hidden="true"></div>`;
}
function setRadioVisual(state, count, target){ const pill=qs('radioPill'); const sub=qs('radioSubstate'); if(!pill || !sub) return;
  const startBtn=qs('startBtn');
  if(startBtn){
    if(state==='on_air'||state==='refilling'||state==='idle'||state==='running'){
      startBtn.textContent=tr('common.stop_radio'); startBtn.classList.add('running');
    } else {
      startBtn.textContent=tr('common.start_radio'); startBtn.classList.remove('running');
    }
  } const normalized=(state||'stopped').replace('_',' '); pill.className='radio-pill'; if(state==='on_air'){ pill.classList.add('on'); pill.textContent=tr('common.on_air_short'); sub.textContent=tr('common.playback_active'); }else if(state==='refilling'){ pill.classList.add('warming'); pill.textContent=tr('common.refilling'); sub.textContent=tr('common.generating_next_tracks'); }else if(state==='idle' || state==='running' || state==='ready'){ pill.classList.add('idle'); pill.textContent='READY'; sub.textContent=tr('common.radio_active_waiting_playback'); }else if(state==='error'){ pill.classList.add('error'); pill.textContent='ERROR'; sub.textContent=tr('common.radio_runtime_error'); }else{ pill.classList.add('off'); pill.textContent=tr('common.off_air'); sub.textContent=tr('common.radio_stopped'); } const small=qs('radioSubstateSmall'); if(small) small.textContent=sub.textContent; }
function reservoirCard(track){
  const sourceInfo = getTrackSourceBadges(track);
  const reservoirBadgesHtml = sourceInfo.badgesHtml;
  const fmtBytes = b => b>1048576?(b/1048576).toFixed(1)+' MB':b>1024?(b/1024).toFixed(0)+' KB':'';
  const specParts = [];
  const bitrateKbps = normalizeBitrateKbps(track.bitrate_kbps ?? track.prompt?.bitrate_kbps ?? track.prompt?.export_applied?.applied_bitrate_kbps ?? track.prompt?.export_applied?.before_bitrate_kbps ?? track.prompt?.mp3_bitrate);
  const sampleRateHz = normalizeSampleRateHz(track.sample_rate_hz ?? track.prompt?.sample_rate_hz ?? track.prompt?.export_applied?.applied_sample_rate ?? track.prompt?.export_applied?.before_sample_rate ?? track.prompt?.mp3_sample_rate);
  if(track.audio_format) specParts.push(`<span class="deck-spec-chip">${track.audio_format.toUpperCase()}</span>`);
  if(bitrateKbps) specParts.push(`<span class="deck-spec-chip">${bitrateKbps} kbps</span>`);
  if(sampleRateHz) specParts.push(`<span class="deck-spec-chip">${formatSampleRateLabel(sampleRateHz)}</span>`);
  if(track.audio_size_bytes) { const s=fmtBytes(track.audio_size_bytes); if(s) specParts.push(`<span class="deck-spec-chip">${s}</span>`); }
  specParts.push(`<span class="deck-spec-chip vote-chip">❤ ${Math.max(0, Number(track.vote_count||0))}</span>`);
  const specs = specParts.length ? `<div class="track-specs">${specParts.join('')}</div>` : '';
  const metaText=formatGenreThemeMeta(track);
  const captionText=getTrackCaption(track);
  const techParts = [];
  if(track.lora_label||track.lora_id) techParts.push(`<span class="tag-lora">LoRA · ${track.lora_label||track.lora_id}</span>`);
  if(track.seed) techParts.push(`<span class="tag-tech">Seed · ${track.seed}</span>`);
  if(track.duration) techParts.push(`<span class="tag-tech">${track.duration}s</span>`);
  if(track.prompt?.model) techParts.push(`<span class="tag-tech">${String(track.prompt.model).replace('acestep-v15-','v1.5-').replace('acestep-v1-','v1-')}</span>`);
  if(track.prompt?.inference_steps) techParts.push(`<span class="tag-tech">${track.prompt.inference_steps} steps</span>`);
  if(track.prompt?.infer_method) techParts.push(`<span class="tag-tech">${(track.prompt.infer_method||'').toUpperCase()}</span>`);
  if(track.prompt?.guidance_scale) techParts.push(`<span class="tag-tech">CFG ${track.prompt.guidance_scale}</span>`);
  if(track.prompt?.shift&&track.prompt.shift!==3) techParts.push(`<span class="tag-tech">shift ${track.prompt.shift}</span>`);
  const techRow = techParts.length ? `<div class="track-tech-row">${techParts.join('')}</div>` : '';
  return `<div class="track-card">
    <div class="track-title">${track.song_title || 'Untitled'}</div>
    <div class="track-meta">${(track.language || '-').toUpperCase()} · ${track.duration || '-'}s · ${track.bpm || '-'} BPM · ${track.key_scale || '-'}<span class="track-source-badges">${reservoirBadgesHtml}</span></div>
    <div class="track-genre-theme">${escapeHtml(metaText) || '&nbsp;'}</div>
    <div class="track-desc">${escapeHtml(captionText) || '&nbsp;'}</div>
    ${specs}
    ${techRow}
  </div>`;
}

function getSelectedAudioFormat(){
  const hidden=qs('audioFormat');
  if(hidden && hidden.value) return String(hidden.value).trim().toLowerCase();
  const active=document.querySelector('.fmt-chip.active');
  return active?.dataset?.fmt ? String(active.dataset.fmt).trim().toLowerCase() : 'mp3';
}
function getMp3BitrateValue(){
  const el=qs('mp3Bitrate');
  return String(el?.value || optionsCache?.defaults?.mp3_bitrate || '128k').trim().toLowerCase();
}
function getMp3SampleRateValue(){
  const el=qs('mp3SampleRate');
  const v=Number(el?.value || optionsCache?.defaults?.mp3_sample_rate || 48000);
  return Number.isFinite(v) && v>0 ? v : 48000;
}
function refreshMp3GenerationControls(){
  const wrap=qs('mp3GenerationControls');
  if(!wrap) return;
  wrap.style.display = getSelectedAudioFormat()==='mp3' ? 'block' : 'none';
}
function updateAutomaticDurationUI(){
  const enabled=!!qs('automaticDuration')?.checked;
  ['minDurationRow','maxDurationRow'].forEach(id=>{
    const row=qs(id);
    if(!row) return;
    row.style.display = enabled ? 'none' : '';
    row.setAttribute('aria-hidden', enabled ? 'true' : 'false');
  });
  const minEl=qs('minDuration');
  const maxEl=qs('maxDuration');
  if(minEl) minEl.disabled=enabled;
  if(maxEl) maxEl.disabled=enabled;
}
function normalizeGenerationModeValue(value){ const normalized=String(value||'').trim().toLowerCase(); if(normalized==='ai'||normalized==='ai generated'||normalized==='ai_generated'||normalized==='ollama') return 'ai_generated'; if(normalized==='local catalog'||normalized==='local'||normalized==='local_catalog') return 'local_catalog'; if(['ai_generated','hybrid'].includes(normalized)) return normalized; return ''; }
function normalizeCatalogSourceValue(value){ const normalized=String(value||'').trim().toLowerCase(); if(normalized==='ai catalog'||normalized==='ai_catalog') return 'generated'; if(normalized==='all local'||normalized==='all_local') return 'all_local'; if(['library','generated'].includes(normalized)) return normalized; return 'library'; }
function coerceGenerationControlState(mode, catalog){ const nextCatalog=normalizeCatalogSourceValue(catalog); let nextMode=normalizeGenerationModeValue(mode) || 'ai_generated'; if(nextCatalog==='all_local' && nextMode==='hybrid') nextMode='local_catalog'; return { mode: nextMode, catalog: nextCatalog, hybridBlocked: nextCatalog==='all_local' }; }
function resolveGenerationControls(settings={}){ const legacy=String(settings.generation_source||'').trim().toLowerCase(); let mode=normalizeGenerationModeValue(settings.generation_mode); if(!mode){ if(legacy==='file'||legacy==='cache') mode='local_catalog'; else if(legacy==='both') mode='hybrid'; else mode='ai_generated'; } const coerced=coerceGenerationControlState(mode, settings.catalog_source); return { mode: coerced.mode, catalog: coerced.catalog, legacySource: legacy, hybridBlocked: coerced.hybridBlocked }; }
function deriveLegacyGenerationSource(mode){ return mode==='hybrid' ? 'both' : mode==='local_catalog' ? 'file' : 'ai_generated'; }
function setFieldRowVisible(row, visible){ if(!row) return; row.hidden=!visible; row.classList.toggle('is-hidden', !visible); row.setAttribute('aria-hidden', visible ? 'false' : 'true'); row.style.setProperty('display', visible ? 'flex' : 'none', 'important'); row.style.setProperty('visibility', visible ? 'visible' : 'hidden', 'important'); row.style.setProperty('pointer-events', visible ? 'auto' : 'none', 'important'); row.style.setProperty('height', visible ? 'auto' : '0', 'important'); row.style.setProperty('margin', visible ? '' : '0', 'important'); row.style.setProperty('padding', visible ? '' : '0', 'important'); row.style.setProperty('overflow', visible ? 'visible' : 'hidden', 'important'); row.style.setProperty('opacity', visible ? '1' : '0', 'important'); }
function currentSettings(){ const stream=streamConfig(); const modelValue=qs('model').value; const autoShift=resolveAutoShift(modelValue); const autoSteps=resolveAutoInferenceSteps(modelValue); const stepsValue=Number(qs('inferenceSteps')?.value||autoSteps); const shiftValue=Number(qs('shift')?.value||autoShift); const generation=resolveGenerationControls({generation_mode:qs('generationMode')?.value, catalog_source:qs('catalogSource')?.value, generation_source:hiddenSettingsState.generation_source}); const customActive=!!hiddenSettingsState.custom_catalog_enabled; const legacySource=customActive ? 'file' : (hiddenSettingsState.generation_source==='cache' && !state.generationSourceDirty ? 'cache' : deriveLegacyGenerationSource(generation.mode)); return {...hiddenSettingsState, station_prompt:qs('stationPrompt').value, station_negative_prompt:qs('stationNegativePrompt')?.value||'', genres:[...state.genres], themes:[...state.themes], languages:[...state.languages], min_duration:Number(qs('minDuration').value||60), max_duration:Number(qs('maxDuration').value||90), automatic_duration:!!qs('automaticDuration')?.checked, instrumental_probability:Number(qs('instrumentalProbability').value||25), model:modelValue, selected_loras:collectLoras(), batch_size:1, use_adg:qs('useAdg').checked, inference_steps:Number.isFinite(stepsValue) ? stepsValue : autoSteps, infer_method:qs('inferMethod').value, guidance_scale:Number(qs('guidanceScale').value||7), shift:Number.isFinite(shiftValue) ? shiftValue : autoShift, cfg_interval_start:Number(qs('cfgStart').value||0), cfg_interval_end:Number(qs('cfgEnd').value||1), enable_normalization:qs('enableNormalization').checked, normalization_db:Number(qs('normalizationDb').value||-1), score_scale:Number(qs('scoreScale').value||0.5), auto_score:qs('autoScore').checked, latent_shift:Number(qs('latentShift').value||0), latent_rescale:Number(qs('latentRescale').value||1), timesteps:qs('timesteps').value, thinking:qs('thinking').checked, lm_temperature:Number(qs('lmTemperature').value||0.85), lm_cfg_scale:Number(qs('lmCfgScale').value||2), lm_top_k:Number(qs('lmTopK').value||0), lm_top_p:Number(qs('lmTopP').value||0.9), lm_negative_prompt:qs('lmNegativePrompt').value, use_constrained_decoding:qs('useConstrainedDecoding').checked, use_cot_metas:qs('useCotMetas').checked, use_cot_caption:qs('useCotCaption').checked, use_cot_language:qs('useCotLanguage').checked, parallel_thinking:qs('parallelThinking').checked, constrained_decoding_debug:qs('constrainedDecodingDebug').checked, ui_language:getUiLanguagePreference(), language_rotation_mode:qs('languageRotationMode').value, generation_mode:generation.mode, catalog_source:generation.catalog, generation_source:legacySource, generation_source_both_percent:Number(qs('generationSourceBothPercent')?.value||50), custom_catalog_enabled:!!hiddenSettingsState.custom_catalog_enabled, custom_catalog_file:hiddenSettingsState.custom_catalog_file||'', custom_catalog_name:hiddenSettingsState.custom_catalog_name||'', custom_catalog_song_count:Number(hiddenSettingsState.custom_catalog_song_count||0), custom_catalog_ignored_count:Number(hiddenSettingsState.custom_catalog_ignored_count||0), vram_cleanup_mode:qs('vramCleanupMode').value, max_saved_tracks:Number(qs('maxSavedTracks').value||100), lora_use_probability:Number(qs('loraUseProbability').value||100), reservoir_target:Number(qs('reservoirTarget').value||10), refill_threshold:Number(qs('refillThreshold').value||3), auto_transition_cut_seconds:Number(qs('autoTransitionCutSeconds')?.value||0), audio_format:qs('audioFormat').value||'mp3', mp3_bitrate:getMp3BitrateValue(), mp3_sample_rate:getMp3SampleRateValue(), monitor_muted:qs('monitorMuted').checked, jingle_separator_arm_offset_s:Number(qs('jingleSeparatorArmOffset')?.value||0), jingle_separator_min_remaining_offset_s:Number(qs('jingleSeparatorMinRemainingOffset')?.value||0), jingle_overlay_mid_offset_s:Number(qs('jingleOverlayMidOffset')?.value||0), jingle_overlay_trigger_window_s:Number(qs('jingleOverlayTriggerWindow')?.value||3), jingle_overlay_min_duration_s:Number(qs('jingleOverlayMinDuration')?.value||60), admin_separator_fade_ms:Number(qs('adminSeparatorFadeMs')?.value||500), admin_overlay_pre_duck_ms:Number(qs('adminOverlayPreDuckMs')?.value||300), admin_overlay_restore_ms:Number(qs('adminOverlayRestoreMs')?.value||700), stream_preset:stream.stream_preset, stream_protocol:stream.protocol, stream_host:stream.host, stream_port:stream.port, stream_mount:stream.mount, stream_username:stream.username, stream_password:stream.password, stream_bitrate:stream.bitrate, stream_format:stream.format, stream_name:stream.name, stream_description:stream.description, stream_genre:stream.genre, stream_public:stream.public}; }
function applySettings(s={}){ hiddenSettingsState={...hiddenSettingsState, ...(s.auth_username!=null?{auth_username:s.auth_username}:{}), ...(s.auth_password!=null?{auth_password:s.auth_password}:{}), ...(s.generation_source!=null?{generation_source:s.generation_source}:{}), ...(s.generation_mode!=null?{generation_mode:s.generation_mode}:{}), ...(s.catalog_source!=null?{catalog_source:s.catalog_source}:{}), ...(s.custom_catalog_enabled!=null?{custom_catalog_enabled:!!s.custom_catalog_enabled}:{}), ...(s.custom_catalog_file!=null?{custom_catalog_file:s.custom_catalog_file}:{}), ...(s.custom_catalog_name!=null?{custom_catalog_name:s.custom_catalog_name}:{}), ...(s.custom_catalog_song_count!=null?{custom_catalog_song_count:s.custom_catalog_song_count}:{}), ...(s.custom_catalog_ignored_count!=null?{custom_catalog_ignored_count:s.custom_catalog_ignored_count}:{})}; state.generationSourceDirty=false; pendingCustomCatalogFile=null; if(s.station_prompt!=null) qs('stationPrompt').value=s.station_prompt; if(s.station_negative_prompt!=null && qs('stationNegativePrompt')) qs('stationNegativePrompt').value=s.station_negative_prompt; state.genres=new Set(s.genres||state.genres||[]); state.themes=new Set(s.themes||state.themes||[]); state.languages=new Set(s.languages||state.languages||['en']); makeChips('genreChips', optionsCache.default_genres||[], state.genres); makeChips('themeChips', optionsCache.default_themes||[], state.themes); makeChips('languageChips', uniq(optionsCache.valid_languages||['en','it']), state.languages); if(s.min_duration!=null) qs('minDuration').value=s.min_duration; if(s.max_duration!=null) qs('maxDuration').value=s.max_duration; if(s.instrumental_probability!=null) qs('instrumentalProbability').value=s.instrumental_probability; if(s.automatic_duration!=null && qs('automaticDuration')) qs('automaticDuration').checked=!!s.automatic_duration; if(s.model!=null) qs('model').value=s.model; if(s.use_adg!=null) qs('useAdg').checked=!!s.use_adg; if(s.inference_steps!=null) applyResolvedFieldValue(qs('inferenceSteps'), s.inference_steps, resolveAutoInferenceSteps(s.model!=null ? s.model : qs('model').value)); if(s.infer_method!=null) qs('inferMethod').value=s.infer_method; if(s.guidance_scale!=null) qs('guidanceScale').value=s.guidance_scale; if(s.cfg_interval_start!=null) qs('cfgStart').value=s.cfg_interval_start; if(s.cfg_interval_end!=null) qs('cfgEnd').value=s.cfg_interval_end; if(s.enable_normalization!=null) qs('enableNormalization').checked=!!s.enable_normalization; if(s.normalization_db!=null) qs('normalizationDb').value=s.normalization_db; if(s.score_scale!=null) qs('scoreScale').value=s.score_scale; if(s.auto_score!=null) qs('autoScore').checked=!!s.auto_score; if(s.latent_shift!=null) qs('latentShift').value=s.latent_shift; if(s.latent_rescale!=null) qs('latentRescale').value=s.latent_rescale; if(s.timesteps!=null) qs('timesteps').value=s.timesteps; if(s.thinking!=null) qs('thinking').checked=!!s.thinking; if(s.lm_temperature!=null) qs('lmTemperature').value=s.lm_temperature; if(s.use_cot_metas!=null) qs('useCotMetas').checked=!!s.use_cot_metas; if(s.use_cot_caption!=null) qs('useCotCaption').checked=!!s.use_cot_caption; if(s.use_cot_language!=null) qs('useCotLanguage').checked=!!s.use_cot_language; if(s.lm_cfg_scale!=null) qs('lmCfgScale').value=s.lm_cfg_scale; if(s.lm_top_k!=null) qs('lmTopK').value=s.lm_top_k; if(s.lm_top_p!=null) qs('lmTopP').value=s.lm_top_p; if(s.lm_negative_prompt!=null) qs('lmNegativePrompt').value=s.lm_negative_prompt; if(s.use_constrained_decoding!=null) qs('useConstrainedDecoding').checked=!!s.use_constrained_decoding; if(s.parallel_thinking!=null) qs('parallelThinking').checked=!!s.parallel_thinking; if(s.constrained_decoding_debug!=null) qs('constrainedDecodingDebug').checked=!!s.constrained_decoding_debug; if(s.shift!=null) applyResolvedFieldValue(qs('shift'), s.shift, resolveAutoShift(s.model!=null ? s.model : qs('model').value)); if(s.language_rotation_mode!=null) qs('languageRotationMode').value=s.language_rotation_mode; const generation=resolveGenerationControls(s); if(qs('generationMode')) qs('generationMode').value=generation.mode; if(qs('catalogSource')) qs('catalogSource').value=generation.catalog; if(s.generation_source_both_percent!=null && qs('generationSourceBothPercent')) qs('generationSourceBothPercent').value=s.generation_source_both_percent; if(s.vram_cleanup_mode!=null) qs('vramCleanupMode').value=s.vram_cleanup_mode; if(s.reservoir_target!=null) qs('reservoirTarget').value=s.reservoir_target; if(s.refill_threshold!=null) qs('refillThreshold').value=s.refill_threshold; if(s.auto_transition_cut_seconds!=null && qs('autoTransitionCutSeconds')) qs('autoTransitionCutSeconds').value=s.auto_transition_cut_seconds; if(s.audio_format!=null){ qs('audioFormat').value=s.audio_format; document.querySelectorAll('.fmt-chip').forEach(b=>b.classList.toggle('active', b.dataset.fmt===s.audio_format)); } if(s.mp3_bitrate!=null && qs('mp3Bitrate')) qs('mp3Bitrate').value=String(s.mp3_bitrate).toLowerCase(); if(s.mp3_sample_rate!=null && qs('mp3SampleRate')) qs('mp3SampleRate').value=String(s.mp3_sample_rate); if(s.max_saved_tracks!=null) qs('maxSavedTracks').value=s.max_saved_tracks; if(s.lora_use_probability!=null) qs('loraUseProbability').value=s.lora_use_probability; if(s.monitor_muted!=null) qs('monitorMuted').checked=!!s.monitor_muted; if(s.stream_protocol!=null) qs('streamProtocol').value=s.stream_protocol; if(s.stream_preset!=null && qs('streamPreset')) { const mapped = s.stream_preset==='listen2myradio_shoutcast2' ? 'listen2myradio_shoutcast2_autodj' : s.stream_preset; qs('streamPreset').value=mapped; } if(s.stream_host!=null) qs('streamHost').value=s.stream_host; if(s.stream_port!=null) qs('streamPort').value=s.stream_port; if(s.stream_mount!=null) qs('streamMount').value=s.stream_mount; if(s.stream_username!=null) qs('streamUser').value=s.stream_username; if(s.stream_password!=null) qs('streamPass').value=s.stream_password; if(s.stream_bitrate!=null) qs('streamBitrate').value=s.stream_bitrate; if(s.stream_format!=null) qs('streamFormat').value=s.stream_format; if(s.stream_name!=null) qs('streamName').value=s.stream_name; if(s.stream_description!=null) qs('streamDesc').value=s.stream_description; if(s.stream_genre!=null) qs('streamGenre').value=s.stream_genre; if(s.stream_public!=null) qs('streamPublic').checked=!!s.stream_public; if(s.jingle_separator_arm_offset_s!=null && qs('jingleSeparatorArmOffset')) qs('jingleSeparatorArmOffset').value=s.jingle_separator_arm_offset_s; if(s.jingle_separator_min_remaining_offset_s!=null && qs('jingleSeparatorMinRemainingOffset')) qs('jingleSeparatorMinRemainingOffset').value=s.jingle_separator_min_remaining_offset_s; if(s.jingle_overlay_mid_offset_s!=null && qs('jingleOverlayMidOffset')) qs('jingleOverlayMidOffset').value=s.jingle_overlay_mid_offset_s; if(s.jingle_overlay_trigger_window_s!=null && qs('jingleOverlayTriggerWindow')) qs('jingleOverlayTriggerWindow').value=s.jingle_overlay_trigger_window_s; if(s.jingle_overlay_min_duration_s!=null && qs('jingleOverlayMinDuration')) qs('jingleOverlayMinDuration').value=s.jingle_overlay_min_duration_s; if(s.admin_separator_fade_ms!=null && qs('adminSeparatorFadeMs')) qs('adminSeparatorFadeMs').value=s.admin_separator_fade_ms; if(s.admin_overlay_pre_duck_ms!=null && qs('adminOverlayPreDuckMs')) qs('adminOverlayPreDuckMs').value=s.admin_overlay_pre_duck_ms; if(s.admin_overlay_restore_ms!=null && qs('adminOverlayRestoreMs')) qs('adminOverlayRestoreMs').value=s.admin_overlay_restore_ms; jfTimingApply(s); syncInferenceStepsWithSelectedModel(false); syncShiftWithSelectedModel(false); refreshMp3GenerationControls(); updateAutomaticDurationUI(); updateGenerationSourceUI(); updateCustomCatalogUI(); updateStreamModeUI({ preserveValues: true }); updateStreamPreview(); updateMonitorMuteState(); const selected=new Map((s.selected_loras||[]).map(x=>[x.id,x])); document.querySelectorAll('.lora-item[data-lora-row-id]').forEach(row=>{ const id=String(row.dataset.loraRowId||''); const entry=selected.get(id)||{id,weight:0.6,weight_self_attn:null,weight_cross_attn:null,weight_ffn:null,enabled:false}; applyLoraEntryToRow(entry); }); const uiLanguagePreference=['auto','en','it'].includes(String(s.ui_language||'').trim().toLowerCase())?String(s.ui_language||'').trim().toLowerCase():'auto'; if(qs('uiLang')) qs('uiLang').value=uiLanguagePreference; setUiLanguage(uiLanguagePreference); }
async function loadOptions(){ const data=await api('/api/options'); optionsCache=data; makeChips('genreChips', data.default_genres||[], state.genres); makeChips('themeChips', data.default_themes||[], state.themes); makeChips('languageChips', uniq(data.valid_languages||['en','it']), state.languages); const model=qs('model'); model.innerHTML=''; const inventory=Array.isArray(data.dit_model_inventory)?data.dit_model_inventory:[]; (data.dit_models||[]).forEach(m=>{ const entry=inventory.find(item=>String(item?.name||'').trim()===String(m).trim())||null; const o=document.createElement('option'); o.value=m; o.textContent=m; if(entry?.is_loaded) o.dataset.loaded='true'; if(entry?.is_default) o.dataset.default='true'; if(m===data.current_dit_model) o.selected=true; model.appendChild(o); }); syncInferenceStepsWithSelectedModel(true); syncShiftWithSelectedModel(true); renderLoras(normalizeLoras(data.lora_catalog)); qs('engineModel').textContent=data.current_dit_model||'-'; qs('ollamaName').textContent=data.ollama_model||'-'; updateSettingsPathDisplay(data.settings_path||''); if(data.saved_settings && Object.keys(data.saved_settings).length){ const savedSettings={...data.saved_settings}; if(data.saved_model_issue){ savedSettings.model=data.current_dit_model||savedSettings.model; } applySettings(savedSettings); } else { syncInferenceStepsWithSelectedModel(true); syncShiftWithSelectedModel(true); }
  const settingsNotice = String(data.settings_notice||'').trim();
  if(settingsNotice){
    setSettingsStatus(settingsNotice, data.settings_notice_level==='error' ? 'error' : 'ok');
  }
  if(data.saved_model_issue){
    setSettingsStatus(String(data.saved_model_issue),'error');
  }
  const fmtContainer = qs('audioFormatChips');
  if(fmtContainer && (data.audio_formats||[]).length){
    fmtContainer.innerHTML='';
    const currentFmt = (data.saved_settings&&data.saved_settings.audio_format)||'mp3';
    (data.audio_formats||['mp3','flac','wav','wav32','opus','aac']).forEach(fmt=>{
      const b=document.createElement('button'); b.type='button'; b.className='fmt-chip'+(fmt===currentFmt?' active':'');
      b.dataset.fmt=fmt; b.textContent=fmt.toUpperCase();
      b.onclick=()=>{ document.querySelectorAll('.fmt-chip').forEach(x=>x.classList.remove('active')); b.classList.add('active'); qs('audioFormat').value=fmt; refreshMp3GenerationControls(); scheduleLiveApply(); };
      fmtContainer.appendChild(b);
    });
    qs('audioFormat').value=currentFmt;
    const mp3BitrateSel = qs('mp3Bitrate');
    const mp3Bitrates = Array.isArray(data.mp3_bitrate_options) ? data.mp3_bitrate_options : ['128k','192k','256k','320k'];
    if (mp3BitrateSel) {
      mp3BitrateSel.innerHTML='';
      mp3Bitrates.forEach((v)=>{ const o=document.createElement('option'); o.value=String(v).toLowerCase(); o.textContent=String(v).replace(/^([0-9]+k)$/i, (_, n) => `${n} kbps`); mp3BitrateSel.appendChild(o); });
      const savedBitrate = (data.saved_settings && data.saved_settings.mp3_bitrate) || (data.defaults && data.defaults.mp3_bitrate) || '128k';
      mp3BitrateSel.value = String(savedBitrate).toLowerCase();
    }
    const mp3SampleRateSel = qs('mp3SampleRate');
    const mp3SampleRates = Array.isArray(data.mp3_sample_rate_options) ? data.mp3_sample_rate_options : [48000, 44100];
    if (mp3SampleRateSel) {
      mp3SampleRateSel.innerHTML='';
      mp3SampleRates.forEach((v)=>{ const n=Number(v); const o=document.createElement('option'); o.value=String(n); o.textContent=(n===44100)?'44.1 kHz':`${(n/1000).toFixed(0)} kHz`; mp3SampleRateSel.appendChild(o); });
      const savedRate = (data.saved_settings && data.saved_settings.mp3_sample_rate) || (data.defaults && data.defaults.mp3_sample_rate) || 48000;
      mp3SampleRateSel.value = String(savedRate);
    }
    refreshMp3GenerationControls();
  }
}
async function refresh(){
  if(refreshInFlight) return;
  refreshInFlight=true;
  try{
    const [statusResult,stats,system]=await Promise.all([
      api('/api/radio/status').then(data=>({data, receivedAt:performance.now()})),
      api('/api/stats').catch(()=>({songs_generated:null})),
      api('/api/system').catch(()=>({}))
    ]);
    const data=statusResult.data;
    const liveSyncPoint=makeBackendPlaybackSyncPoint(data, statusResult.receivedAt, lastAuthoritativePlaybackElapsed);
    livePlaybackRate=normalizePlaybackRate(data.current_playback_rate||1);
    applyPitchPreservedRate(player, livePlaybackRate);
    applyPitchPreservedRate(playerB, livePlaybackRate);
    renderPlaybackSpeedControl(livePlaybackRate, data.current_track?.id||data.next_track?.id||'');
    renderOperationalPanels(data);
    renderBootstrap(data.bootstrap);
    lastRunning=!!data.running;
    const radioState=(data.radio_state|| (data.running?'running':'stopped'));
    qs('running').textContent=radioState.replace('_',' ');
    qs('engineModel').textContent=data.model||'-';
    qs('ollamaName').textContent=data.ollama_model||'-';
    if(data.last_error && data.last_error!==lastRuntimeErrorSeen) noteRuntimeError(data.last_error);
    qs('reservoirInfo').textContent=`${data.prepared_count||0} / ${data.reservoir_target||10}`;
    const _rt=data.reservoir_target||10;
    qs('reservoirTargetInfo').textContent=`${data.prepared_count||0} / ${_rt}`;
    qs('savedTracksInfo').textContent=String(data.archived_tracks||0);

    const cacheCount = data.cache_available || 0;
    const cacheOnDisk = data.cache_on_disk || 0;
    const cacheEl = qs('cacheInfo'); if(cacheEl) cacheEl.textContent = cacheCount ? `${cacheCount} cached` : '0';
    const cacheReadyBox = qs('cacheReadyBox'); if(cacheReadyBox) cacheReadyBox.textContent = cacheCount ? `${cacheCount} track${cacheCount!==1?'s':''} ready` : '0 tracks';
    const cacheOnDiskBox = qs('cacheOnDiskBox'); if(cacheOnDiskBox) cacheOnDiskBox.textContent = cacheOnDisk ? `${cacheOnDisk} track${cacheOnDisk!==1?'s':''}` : '0 tracks';
    qs('reservoirHeadline').textContent=(data.prepared_count||0)?`${data.prepared_count||0} prepared track(s) ready to play`:'No prepared tracks yet';
    const monitorMutedValue=!!data.monitor_muted;
    if(qs('monitorMuted') && shouldRespectBackendValue('monitorMuted')) qs('monitorMuted').checked=monitorMutedValue;
    consumeUiDirty('monitorMuted', monitorMutedValue);
    updateMonitorMuteState();
    setRadioVisual(radioState, data.prepared_count||0, data.reservoir_target||10);
    qs('history').innerHTML='';
    (data.history||[]).forEach(x=>{ const li=document.createElement('li'); li.textContent=x; qs('history').appendChild(li); });
    qs('reservoirList').innerHTML='';
    (data.reservoir||[]).forEach(x=>{ const wrap=document.createElement('div'); wrap.innerHTML=reservoirCard(x); qs('reservoirList').appendChild(wrap.firstChild); });
    if(!(data.reservoir||[]).length){ qs('reservoirList').innerHTML=`<div class="muted">${tr('common.reservoir_empty_now')}</div>`; }
    qs('songsPrepared').textContent=`${data.prepared_count||0} / ${data.reservoir_target||10}`;
    qs('songsGeneratedTotal').textContent=String(stats?.songs_generated_total ?? stats?.songs_generated ?? 0);
    qs('songsGeneratedRun').textContent=String(stats?.songs_generated_this_run ?? 0);
    qs('gpuName').textContent=system?.gpu_name || '-';
    if(system?.vram_used_mb!=null && system?.vram_total_mb!=null){ qs('gpuVram').textContent=`${system.vram_used_mb} / ${system.vram_total_mb} MB`; }else{ qs('gpuVram').textContent='-'; }
    const tempEl=qs('gpuTemp');
    if(system?.gpu_temp_c!=null){ tempEl.textContent=`${system.gpu_temp_c} °C`; tempEl.className='footer-value ' + (system.gpu_temp_c>=80?'gpu-hot':system.gpu_temp_c>=70?'gpu-warn':'gpu-good'); }else{ tempEl.textContent='-'; tempEl.className='footer-value'; }
    const powerEl=qs('gpuPower'); if(powerEl){ powerEl.textContent=system?.gpu_power_w!=null ? `${system.gpu_power_w} W` : '-'; }
    const ipEl=qs('clientIp'); if(ipEl){ ipEl.textContent=system?.client_ip || '-'; }
    const adminVoteScopeEl=qs('adminVoteScope'); if(adminVoteScopeEl){ adminVoteScopeEl.textContent=system?.vote_scope || '1 per browser'; }
    const activeCurrent = data.current_track || null;
    const preparedNext = data.next_track || null;
    const queuedReserve = (data.reservoir && data.reservoir.length) ? data.reservoir[0] : null;
    if(autoCrossfadeActive || cfInterval || Date.now()<cfCancelGuardUntil){

      if(activeCurrent){
        document.title=`${activeCurrent.song_title} · AceRadio V1.0`;
        qs('bpmDisplay').textContent=effectiveBpmText(activeCurrent, livePlaybackRate);

        if(activeDeck==='B') loadDeckAudio('B', activeCurrent, false, liveSyncPoint);
        else                 loadDeckAudio('A', activeCurrent, false, liveSyncPoint);
      }

      const inactiveAudio = activeDeck==='A' ? playerB : player;
      const nextUrl = preparedNext?.audio_url || null;
      if(nextUrl && !audioSourceMatches(inactiveAudio, nextUrl)){
        if(activeDeck==='A') loadDeckAudio('B', preparedNext, false, 0);
        else                 loadDeckAudio('A', preparedNext, false, 0);
      }

      if(data.last_error && data.last_error!==lastRuntimeErrorSeen) noteRuntimeError(data.last_error);
      qs('reservoirList').innerHTML='';
      (data.reservoir||[]).forEach(x=>{ const wrap=document.createElement('div'); wrap.innerHTML=reservoirCard(x); qs('reservoirList').appendChild(wrap.firstChild); });
      if(!(data.reservoir||[]).length){ qs('reservoirList').innerHTML=`<div class="muted">${tr('common.reservoir_empty_now')}</div>`; }
      window._jdLastStatus = data;
      return;
    }
    const currentMatchesDeckA = !!(
      activeCurrent &&
      currentTrackId &&
      activeCurrent.id===currentTrackId &&
      audioSourceMatches(player, activeCurrent.audio_url)
    );
    const currentMatchesDeckB = !!(
      activeCurrent &&
      deckBTrackId &&
      activeCurrent.id===deckBTrackId &&
      audioSourceMatches(playerB, activeCurrent.audio_url)
    );
    const currentIsOnDeckB = !!(
      activeCurrent && (
        currentMatchesDeckB ||
        (activeDeck==='B' && !deckBTrackId && !playerB.getAttribute('src') && !player.getAttribute('src'))
      )
    );
    const currentIsOnDeckA = !!(
      activeCurrent && (
        currentMatchesDeckA ||
        (activeDeck!=='B' && !currentTrackId && !deckBTrackId && !player.getAttribute('src') && !playerB.getAttribute('src'))
      )
    );
    if(currentIsOnDeckB){
      loadDeckAudio('B', activeCurrent, !manualStopB, liveSyncPoint);
      loadDeckAudio('A', preparedNext || queuedReserve, false, 0);
      setDeckVisualState('B');
      qs('crossfader').value=1;
      if((preparedNext || queuedReserve) && !audioSourceMatches(player, (preparedNext || queuedReserve).audio_url)){
        loadDeckAudio('A', preparedNext || queuedReserve, false, 0);
      }
    }else if(currentIsOnDeckA || activeCurrent){
      loadDeckAudio('A', activeCurrent, !manualStopA, liveSyncPoint);
      loadDeckAudio('B', preparedNext, false, 0);
      setDeckVisualState('A');
      qs('crossfader').value=0;
      reinitVU();
      document.title=`${activeCurrent.song_title} · AceRadio V1.0`;
      qs('bpmDisplay').textContent=effectiveBpmText(activeCurrent, livePlaybackRate);

    }else if(preparedNext || queuedReserve){
      loadDeckAudio('A', preparedNext || null, false, 0);
      loadDeckAudio('B', queuedReserve && preparedNext && queuedReserve.id!==preparedNext.id ? queuedReserve : null, false, 0);
      setDeckVisualState('A');
      qs('crossfader').value=0;
      document.title='AceRadio V1.0';
      qs('bpmDisplay').textContent=preparedNext ? effectiveBpmText(preparedNext, livePlaybackRate) : '—';
    }else{
      loadDeckAudio('A', null, false, 0);
      loadDeckAudio('B', null, false, 0);
      setDeckVisualState('A');
      qs('crossfader').value=0;
      document.title='AceRadio V1.0';
      qs('bpmDisplay').textContent='—';
    }
    suppressCrossfaderAutoplay=true;
    applyCrossfaderVolumes(parseFloat(qs('crossfader').value||0),{allowAutoplay:false});
    suppressCrossfaderAutoplay=false;
    if(currentIsOnDeckB && activeCurrent){
      document.title=`${activeCurrent.song_title} · AceRadio V1.0`;
      qs('bpmDisplay').textContent=effectiveBpmText(activeCurrent, livePlaybackRate);

    }
    if(!activeCurrent && !preparedNext){
      loadDeckAudio('A', null, false, 0);
      loadDeckAudio('B', null, false, 0);
      setDeckVisualState('A');
      qs('crossfader').value=0;
      suppressCrossfaderAutoplay=true;
    applyCrossfaderVolumes(parseFloat(qs('crossfader').value||0),{allowAutoplay:false});
    suppressCrossfaderAutoplay=false;
      document.title='AceRadio V1.0';
      qs('bpmDisplay').textContent='—';
    }

    window._jdLastStatus = data;
  }catch(err){
    appendAdminLog('error','Refresh failed',[String(err.message||err)], `refresh|${String(err.message||err)}`);
  }finally{
    refreshInFlight=false;
  }
}
qs('startBtn').onclick=async()=>{ if(!bootstrapReady) return; if(lastRunning){ await api('/api/radio/stop',{method:'POST'}); } else { await api('/api/radio/start',{method:'POST', body:JSON.stringify(currentSettings())}); } refresh(); };
const hardRefreshBtn=qs('hardRefreshBtn'); if(hardRefreshBtn){ hardRefreshBtn.onclick=()=>{ hardRefreshBtn.disabled=true; hardRefreshAdminPage().catch(()=>{ hardRefreshBtn.disabled=false; }); }; }
qs('stopBtn').onclick=async()=>{
  if(!bootstrapReady) return;
  const btn=qs('stopBtn');
  btn.disabled=true;
  const action=localPreviewPaused?'resume':'pause';
  try{
    if(localPreviewPaused){
      await resumeLocalPreviewFromLive();
    }else{
      pauseLocalPreviewOnly();
    }
  }catch(err){
    const msg=String(err.message||err);
    appendAdminLog('error','Preview transport failed',[msg], `preview-stop-toggle|${action}|${msg}`);
    updateStopPreviewButton();
  }finally{
    btn.disabled=!bootstrapReady;
  }
};
qs('skipBtn').onclick=async()=>{
  if(!bootstrapReady) return;
  const prevActiveDeck = activeDeck;
  const prevCfValue = parseFloat(qs('crossfader').value||0);
  let didOptimisticPromote = false;
  if(hasDeckBTrack() && activeDeck==='A'){
    player.pause();
    manualStopA=false; manualStopB=false;
    setDeckVisualState('B');
    qs('crossfader').value=1;
    suppressCrossfaderAutoplay=true;
    applyCrossfaderVolumes(1,{allowAutoplay:false});
    suppressCrossfaderAutoplay=false;
    if(playerB.paused) playerB.play().catch(()=>{});
    didOptimisticPromote=true;
  } else if(hasDeckATrack() && activeDeck==='B'){
    playerB.pause();
    manualStopA=false; manualStopB=false;
    setDeckVisualState('A');
    qs('crossfader').value=0;
    suppressCrossfaderAutoplay=true;
    applyCrossfaderVolumes(0,{allowAutoplay:false});
    suppressCrossfaderAutoplay=false;
    if(player.paused) player.play().catch(()=>{});
    didOptimisticPromote=true;
  }
  try{
    await api('/api/radio/skip',{method:'POST'});
    refresh();
  }catch(err){
    if(didOptimisticPromote){
      if(prevActiveDeck==='A'){ playerB.pause(); setDeckVisualState('A'); }
      else { player.pause(); setDeckVisualState('B'); }
      qs('crossfader').value=prevCfValue;
      suppressCrossfaderAutoplay=true;
      applyCrossfaderVolumes(prevCfValue,{allowAutoplay:false});
      suppressCrossfaderAutoplay=false;
    }
    appendAdminLog('error','Skip failed',[String(err.message||err)], `skip|${String(err.message||err)}`);
  }
};
qs('saveSettingsBtn').onclick=async()=>{ if(!bootstrapReady) return; const btn=qs('saveSettingsBtn'); btn.disabled=true; setSettingsStatus('Saving settings…'); try{ const s=currentSettings(); const data=await api('/api/settings/save',{method:'POST', body:JSON.stringify(s)}); updateSettingsPathDisplay(data.path||''); let msg=`Saved · ${data.bytes||0} bytes`; if(lastRunning){ await api('/api/radio/apply-settings',{method:'POST', body:JSON.stringify(s)}); msg+=' · applied to radio'; } setSettingsStatus(msg,'ok'); }catch(err){ setSettingsStatus(`Save failed: ${String(err.message||err)}`,'error'); } finally{ btn.disabled=false; } };
qs('saveAsSettingsBtn').onclick=async()=>{ if(!bootstrapReady) return; const btn=qs('saveAsSettingsBtn'); btn.disabled=true; setSettingsStatus('Opening Save As dialog…'); try{ const s=currentSettings(); const data=await api('/api/settings/save-as',{method:'POST', body:JSON.stringify(s)}); if(data.cancelled){ setSettingsStatus('Save as cancelled.'); return; } updateSettingsPathDisplay(data.path||''); let msg=`Saved as · ${data.path||''}`; if(lastRunning){ await api('/api/radio/apply-settings',{method:'POST', body:JSON.stringify(s)}); msg+=' · applied to radio'; } setSettingsStatus(msg,'ok'); }catch(err){ setSettingsStatus(`Save as failed: ${String(err.message||err)}`,'error'); } finally{ btn.disabled=false; } };

qs('uiLang').addEventListener('change', e=>{ setUiLanguage(e.target.value); });

async function applyLiveSettings(){
  if(!bootstrapReady || !lastRunning) return;
  try{
    const settings=currentSettings();
    await api('/api/radio/apply-settings',{method:'POST', body:JSON.stringify(settings)});
    consumeUiDirty('monitorMuted', settings.monitor_muted);
    setSettingsStatus('Live settings applied to next generations.','ok');
  }catch(err){
    setSettingsStatus(`Live apply failed: ${String(err.message||err)}`,'error');
  }
}
function scheduleLiveApply(){
  if(!bootstrapReady || !lastRunning) return;
  if(applyTimer) clearTimeout(applyTimer);
  applyTimer=setTimeout(applyLiveSettings, 500);
}

async function init(){
  document.querySelectorAll('.sidebar details.s-section').forEach(el=>{ el.open=false; });
  const pendingHardRefresh=consumePendingHardRefresh();
  const pendingPreviewRestore=pendingHardRefresh || getNavigationType()==='reload';
  const pickedFileName=qs('pickedFileName');
  if(pickedFileName) pickedFileName.textContent=tr('common.no_file_selected');
  initAdminMonitorLevel();
  const cf = qs('crossfader');
  if(cf){ cf.value=0; cf.dispatchEvent(new Event('input')); }
  updateVolDb('A', qs('deckAVol')?.value||1);
  updateVolDb('B', qs('deckBVol')?.value||1);
  manualStopA=false;
  manualStopB=false;
  try{ const state=await pollBootstrap(); if(state.ready){ await loadOptions(); if(!String(optionsCache?.settings_notice||'').trim()) setSettingsStatus(`Active settings file: ${qs('settingsPath')?.textContent||'-'}`); await refresh(); if(pendingPreviewRestore) await restoreAdminPreviewAfterHardRefresh(); } else { const timer=setInterval(async()=>{ try{ const next=await pollBootstrap(); if(next.ready){ clearInterval(timer); await loadOptions(); if(!String(optionsCache?.settings_notice||'').trim()) setSettingsStatus(`Active settings file: ${qs('settingsPath')?.textContent||'-'}`); await refresh(); if(pendingPreviewRestore) await restoreAdminPreviewAfterHardRefresh(); } }catch(err){ setSettingsStatus(`Bootstrap failed: ${String(err.message||err)}`,'error'); } }, 1500); } }catch(err){ setSettingsStatus(`Bootstrap failed: ${String(err.message||err)}`,'error'); } }

async function reloadUiFromActiveSettings(){
  await loadOptions();
  await refresh();
}

qs('browseSettingsBtn').onclick = async () => {
  if (!bootstrapReady) return;
  const btn = qs('browseSettingsBtn');
  const nameEl = qs('pickedFileName');
  btn.disabled = true;
  nameEl.textContent = 'Opening file dialog…';
  setSettingsStatus('Choose a settings file to load and apply…');
  try {
    const data = await api('/api/settings/browse', { method: 'POST' });
    if (data.cancelled || !data.path) {
      nameEl.textContent = 'Cancelled';
      setSettingsStatus('Load cancelled.');
      return;
    }
    if (data.settings && typeof data.settings === 'object') applySettings(data.settings);
    await reloadUiFromActiveSettings();
    let msg = `Loaded and applied · ${data.path.split(/[\/]/).pop()}`;
    let statusKind = 'ok';
    if (lastRunning) {
      await api('/api/radio/apply-settings',{method:'POST', body:JSON.stringify(currentSettings())});
      msg += ' · applied to radio';
    }
    if (data.warning) {
      msg += ` · ${String(data.warning)}`;
      statusKind = 'error';
    }
    updateSettingsPathDisplay(data.path||'');
    nameEl.textContent = data.path.split(/[\/]/).pop();
    nameEl.title = data.path;
    setSettingsStatus(msg, statusKind);
  } catch (err) {
    nameEl.textContent = 'Error';
    setSettingsStatus(`Load failed: ${String(err.message || err)}`, 'error');
  } finally {
    btn.disabled = !bootstrapReady;
  }
};

qs('monitorMuted').addEventListener('change', ()=>{ markUiDirty('monitorMuted', !!qs('monitorMuted').checked); updateMonitorMuteState(); scheduleLiveApply(); });
if(qs('monitorMuteBtn')) qs('monitorMuteBtn').addEventListener('click', ()=>{ const el=qs('monitorMuted'); if(!el) return; el.checked=!el.checked; el.dispatchEvent(new Event('change',{bubbles:true})); });
['mp3Bitrate','mp3SampleRate'].forEach(id=>{ const el=qs(id); if(el){ el.addEventListener('change', scheduleLiveApply); el.addEventListener('input', scheduleLiveApply); }});
setSettingsStatus(tr('common.waiting_bootstrap')); resetValidateButtonState(); updateStopPreviewButton(); init(); setInterval(()=>{ if(bootstrapReady) refresh(); else pollBootstrap().catch(()=>{}); }, 3000);

['stationPrompt','stationNegativePrompt','languageRotationMode','generationSourceBothPercent','minDuration','maxDuration','instrumentalProbability','model','useAdg','inferenceSteps','inferMethod','guidanceScale','shift','cfgStart','cfgEnd','enableNormalization','normalizationDb','scoreScale','autoScore','latentShift','latentRescale','timesteps','thinking','lmTemperature','useCotMetas','useCotCaption','useCotLanguage','lmCfgScale','lmTopK','lmTopP','lmNegativePrompt','useConstrainedDecoding','parallelThinking','constrainedDecodingDebug','uiLang','vramCleanupMode','maxSavedTracks','loraUseProbability','reservoirTarget','refillThreshold','autoTransitionCutSeconds'].forEach(id=>{ const el=qs(id); if(el){ el.addEventListener('change', scheduleLiveApply); el.addEventListener('input', scheduleLiveApply); }});
['inferenceSteps','shift'].forEach(id=>{ const el=qs(id); if(el){ const markManual=()=>setManualOverrideFlag(el, true); el.addEventListener('input', markManual); el.addEventListener('change', markManual); }});
const modelEl=qs('model'); if(modelEl){ modelEl.addEventListener('change', ()=>{ syncInferenceStepsWithSelectedModel(true); syncShiftWithSelectedModel(true); scheduleLiveApply(); }); }
syncInferenceStepsWithSelectedModel(true);
syncShiftWithSelectedModel(true);
document.addEventListener('change', e=>{ if(e.target && (e.target.matches('[data-lora-id]') || e.target.matches('[data-lora-weight]'))){ scheduleLiveApply(); }});
document.addEventListener('input', e=>{ if(!e.target) return; if(e.target.matches('[data-lora-weight]')){ const row=e.target.closest('.lora-item'); if(row){ e.target.value=String(clampLoraWeightValue(e.target.value, 0.6).toFixed(2)); refreshLoraRow(row); } scheduleLiveApply(); } else if(e.target.matches('[data-lora-adv-id]')){ const row=e.target.closest('.lora-item'); const key=e.target.getAttribute('data-lora-adv'); if(row && key){ e.target.dataset.followMain='0'; e.target.value=String(clampLoraWeightValue(e.target.value, getLoraMainWeightValue(row)).toFixed(2)); refreshLoraRow(row); } scheduleLiveApply(); }});
document.addEventListener('change', e=>{ if(!e.target) return; if(e.target.matches('[data-lora-adv-id]')){ const row=e.target.closest('.lora-item'); if(row) refreshLoraRow(row); scheduleLiveApply(); }});
document.addEventListener('click', e=>{ const btn=e.target && e.target.closest('[data-lora-link-id]'); if(!btn) return; const row=btn.closest('.lora-item'); const key=btn.getAttribute('data-lora-link'); if(!row || !key) return; setLoraAdvancedFollow(row, key, !isLoraAdvancedFollowing(row, key)); scheduleLiveApply(); });
window.addEventListener('aceradio:languagechange', ()=>{ if(optionsCache?.lora_catalog) renderLoras(normalizeLoras(optionsCache.lora_catalog)); });
const speedSlider=qs('currentSpeedSlider');
const speedSliderWrap=qs('currentSpeedSliderWrap');
const speedResetBtn=qs('currentSpeedResetBtn');
if(speedSlider){
  const armSpeedResetGuard=(ms=900)=>{
    currentSpeedResetGuardUntil=Math.max(currentSpeedResetGuardUntil, Date.now()+ms);
    if(speedResetBtn){
      speedResetBtn.classList.add('guarded');
      speedResetBtn.disabled=true;
      if(currentSpeedResetGuardTimer) clearTimeout(currentSpeedResetGuardTimer);
      currentSpeedResetGuardTimer=setTimeout(()=>{
        if(Date.now()>=currentSpeedResetGuardUntil){
          speedResetBtn.classList.remove('guarded');
          renderPlaybackSpeedControl(livePlaybackRate, currentTrackId||deckBTrackId||'');
        }
      }, ms+40);
    }
  };
  const applySpeed=(rawValue=null)=>{
    const rate=rawValue==null ? sliderValueToPlaybackRate(speedSlider.value) : normalizePlaybackRate(rawValue);
    if(rawValue!=null) speedSlider.value=String(playbackRateToSliderValue(rate));
    livePlaybackRate=normalizePlaybackRate(rate);
    renderPlaybackSpeedControl(livePlaybackRate, currentTrackId||deckBTrackId||'');
    applyPitchPreservedRate(player, livePlaybackRate);
    applyPitchPreservedRate(playerB, livePlaybackRate);
    if(playbackRateApplyTimer) clearTimeout(playbackRateApplyTimer);
    playbackRateApplyTimer=setTimeout(async()=>{
      try{
        const result=await api('/api/radio/current-speed',{method:'POST', body:JSON.stringify({rate: livePlaybackRate})});
        const applied=normalizePlaybackRate(result?.rate||livePlaybackRate);
        livePlaybackRate=applied;
        renderPlaybackSpeedControl(applied, currentTrackId||deckBTrackId||'');
      }catch(err){
        appendAdminLog('error','Playback speed update failed',[String(err.message||err)], `speed|${String(err.message||err)}`);
      }
    }, 160);
  };
  const finishSliderChange=()=>{
    currentSpeedUserDragging=false;
    armSpeedResetGuard(1200);
    renderPlaybackSpeedControl(sliderValueToPlaybackRate(speedSlider.value), currentTrackId||deckBTrackId||'');
  };
  speedSlider.addEventListener('pointerdown', ()=>{
    currentSpeedUserDragging=true;
    armSpeedResetGuard(1200);
  });
  speedSlider.addEventListener('input', ()=>{
    currentSpeedUserDragging=true;
    armSpeedResetGuard(1200);
    applySpeed();
  });
  speedSlider.addEventListener('pointerup', finishSliderChange);
  speedSlider.addEventListener('keyup', finishSliderChange);
  document.addEventListener('pointerup', ()=>{
    if(!currentSpeedUserDragging) return;
    finishSliderChange();
  });
  if(speedSliderWrap){
    speedSliderWrap.addEventListener('keydown', event=>{
      if(event.target===speedSlider) return;
      let nextValue=null;
      const currentRaw=Number(speedSlider.value); const currentValue=Math.max(0, Math.min(100, Number.isFinite(currentRaw) ? currentRaw : 50));
      if(event.key==='ArrowUp') nextValue=currentValue+2;
      else if(event.key==='ArrowDown') nextValue=currentValue-2;
      else if(event.key==='PageUp') nextValue=currentValue+10;
      else if(event.key==='PageDown') nextValue=currentValue-10;
      else if(event.key==='Home') nextValue=0;
      else if(event.key==='End') nextValue=100;
      else if(event.key===' ' || event.key==='Enter') nextValue=50;
      if(nextValue==null) return;
      event.preventDefault();
      speedSlider.value=String(Math.max(0, Math.min(100, Math.round(nextValue))));
      currentSpeedUserDragging=false;
      armSpeedResetGuard();
      applySpeed();
    });
  }
  if(speedResetBtn){
    const blockResetIfGuarded=event=>{
      if(Date.now()<currentSpeedResetGuardUntil){
        event.preventDefault();
        event.stopPropagation();
        return true;
      }
      return false;
    };
    speedResetBtn.addEventListener('pointerdown', event=>{ if(blockResetIfGuarded(event)) return; });
    speedResetBtn.addEventListener('mousedown', event=>{ if(blockResetIfGuarded(event)) return; });
    speedResetBtn.addEventListener('click', event=>{
      if(blockResetIfGuarded(event)) return;
      currentSpeedUserDragging=false;
      applySpeed(1);
    });
  }
}

function fmtTime(s){ if(!s||isNaN(s)) return '0:00'; const m=Math.floor(s/60); return `${m}:${String(Math.floor(s%60)).padStart(2,'0')}`; }

function updateDeckUI(){
  const pa=player; const pb=playerB;
  const ap=pa.currentTime||0; const ad=pa.duration||0;
  const bp=pb.currentTime||0; const bd=pb.duration||0;
  qs('deckATime').textContent=`${fmtTime(ap)} / ${fmtTime(ad)}`;
  qs('deckBTime').textContent=`${fmtTime(bp)} / ${fmtTime(bd)}`;
  if(ad>0){ qs('deckASeek').value=(ap/ad*100).toFixed(1); qs('waveformProgressA').style.width=(ap/ad*100)+'%'; }
  if(bd>0){ qs('deckBSeek').value=(bp/bd*100).toFixed(1); qs('waveformProgressB').style.width=(bp/bd*100)+'%'; }
  qs('deckAPlayBtn').textContent=pa.paused?'▶':'⏸';
  qs('deckBPlayBtn').textContent=pb.paused?'▶':'⏸';
  requestAnimationFrame(updateDeckUI);
}
requestAnimationFrame(updateDeckUI);

async function toggleDeckA(){
  if(player.paused){
    if(await resumeDeckPreviewFromLive('A').catch(()=>false)) return;
    manualStopA=false;
    localPreviewPaused=false;
    updateStopPreviewButton();
    ensurePreviewAudioPlaying(player).catch(()=>{});
  } else {
    manualStopA=true;
    player.pause();
  }
}
async function toggleDeckB(){
  if(playerB.paused){
    if(await resumeDeckPreviewFromLive('B').catch(()=>false)) return;
    manualStopB=false;
    localPreviewPaused=false;
    updateStopPreviewButton();
    if(activeDeck!=='B'){
      promoteDeckBToAir(true);
    } else {
      ensurePreviewAudioPlaying(playerB).catch(()=>{});
    }
  } else {
    manualStopB=true;
    playerB.pause();
  }
}

function fmtRemaining(cur,dur){ if(!dur||isNaN(dur)) return '-0:00'; const r=dur-cur; const m=Math.floor(r/60); return '-'+m+':'+(Math.floor(r%60)+'').padStart(2,'0'); }

setInterval(()=>{
  const pa=player, pb=playerB;
  const ap=pa.currentTime||0, ad=pa.duration||0;
  const bp=pb.currentTime||0, bd=pb.duration||0;
  qs('deckATime').textContent=fmtTime(ap);
  qs('deckADur').textContent='/ '+fmtTime(ad);
  qs('deckARemaining').textContent=fmtRemaining(ap,ad);
  qs('deckBTime').textContent=fmtTime(bp);
  qs('deckBDur').textContent='/ '+fmtTime(bd);
  qs('deckBRemaining').textContent=fmtRemaining(bp,bd);
  if(ad>0){ qs('deckASeek').value=(ap/ad*100).toFixed(1); qs('waveformProgressA').style.width=(ap/ad*100)+'%'; }
  if(bd>0){ qs('deckBSeek').value=(bp/bd*100).toFixed(1); qs('waveformProgressB').style.width=(bp/bd*100)+'%'; }
  qs('deckAPlayBtn').textContent=pa.paused?'▶':'⏸';
  qs('deckBPlayBtn').textContent=pb.paused?'▶':'⏸';
}, 250);

const _deckScroll = {
  A: { auto: true, userScrolled: false, timer: null, scrolling: false },
  B: { auto: true, userScrolled: false, timer: null, scrolling: false },
};

function toggleDeckScroll(deck) {
  const s = _deckScroll[deck];
  s.auto = !s.auto;
  s.userScrolled = false;
  const btn = qs('lyricsScrollToggle' + deck);
  if (btn) btn.classList.toggle('on', s.auto);
}

(function initDeckLyricsScrollGuard() {
  ['A','B'].forEach(deck => {
    const boxId = deck === 'A' ? 'lyrics' : 'lyricsB';
    const box = qs(boxId);
    if (!box) return;
    box.addEventListener('scroll', () => {
      const s = _deckScroll[deck];
      if (s.scrolling) return;
      s.userScrolled = true;
      if (s.timer) clearTimeout(s.timer);
      s.timer = setTimeout(() => { s.userScrolled = false; }, 4000);
    }, { passive: true });
  });
})();

setInterval(() => {
  function scrollLyrics(deck, cur, dur) {
    const s = _deckScroll[deck];
    if (!s.auto || s.userScrolled) return;
    const boxId = deck === 'A' ? 'lyrics' : 'lyricsB';
    const box = qs(boxId);
    if (!box || !dur || isNaN(dur) || cur < 0.5) return;
    const max = box.scrollHeight - box.clientHeight;
    if (max <= 0) return;
    const target = Math.floor((cur / dur) * max);
    if (Math.abs(box.scrollTop - target) > 3) {
      s.scrolling = true;
      box.scrollTop = target;
      s.scrolling = false;
    }
  }
  scrollLyrics('A', player.currentTime || 0,  player.duration  || 0);
  scrollLyrics('B', playerB.currentTime || 0, playerB.duration || 0);
}, 600);

let _seekSyncTimer=null;
function _currentLiveDeckState(){
  if(activeDeck==='B') return { deck:'B', player:playerB, trackId:deckBTrackId };
  return { deck:'A', player, trackId:currentTrackId };
}
function _sendSeekSyncForLiveDeck(immediate=false){
  const run = () => {
    const live = _currentLiveDeckState();
    if(!live.player || !live.trackId || !live.player.getAttribute('src')) return;
    const elapsed = Number(live.player.currentTime || 0);
    if(!(elapsed >= 0)) return;
    api('/api/radio/seek-sync', {
      method:'POST',
      body:JSON.stringify({ track_id: live.trackId, elapsed }),
      headers:{ 'Content-Type':'application/json' },
    }).catch(()=>{});
  };
  if(immediate){
    if(_seekSyncTimer){ clearTimeout(_seekSyncTimer); _seekSyncTimer=null; }
    run();
    return;
  }
  if(_seekSyncTimer) clearTimeout(_seekSyncTimer);
  _seekSyncTimer = setTimeout(()=>{ _seekSyncTimer=null; run(); }, 120);
}
function _bindSeekSync(slider, audio, deck){
  if(!slider || !audio) return;
  const apply = (value, immediate=false) => {
    if(audio.duration) audio.currentTime = value / 100 * audio.duration;
    if(deck === activeDeck) _sendSeekSyncForLiveDeck(immediate);
  };
  slider.addEventListener('input',e=>apply(Number(e.target.value||0), false));
  slider.addEventListener('change',e=>apply(Number(e.target.value||0), true));
  slider.addEventListener('mouseup',()=>{ if(deck === activeDeck) _sendSeekSyncForLiveDeck(true); });
  slider.addEventListener('touchend',()=>{ if(deck === activeDeck) _sendSeekSyncForLiveDeck(true); }, {passive:true});
}
_bindSeekSync(qs('deckASeek'), player, 'A');
_bindSeekSync(qs('deckBSeek'), playerB, 'B');
qs('deckAVol').addEventListener('input',e=>{ updateVolDb('A',e.target.value); applyCrossfaderVolumes(parseFloat(qs('crossfader').value||0),{allowAutoplay:false}); });
qs('deckBVol').addEventListener('input',e=>{ updateVolDb('B',e.target.value); applyCrossfaderVolumes(parseFloat(qs('crossfader').value||0),{allowAutoplay:false}); });
const adminMonitorSlider=qs('adminMonitorLevel');
if(adminMonitorSlider) adminMonitorSlider.addEventListener('input',e=>applyAdminMonitorLevel(e.target.value));

qs('crossfader').addEventListener('input',e=>{
  const v=parseFloat(e.target.value);
  const prev=lastCrossfaderValue;
  applyCrossfaderVolumes(v,{allowAutoplay:false});
  maybeStartCrossfadeTarget(prev, v);
  lastCrossfaderValue=v;
  if(!autoCrossfadeActive && !cfInterval) commitManualCrossfader(v).catch(()=>{});
});

const CF_DURATION=3000;
const CF_STEPS=60;

if(qs('crossfadeBtn')) qs('crossfadeBtn').addEventListener('click',()=>{
  if(!bothDecksReadyForCrossfade()){
    syncCrossfaderToActiveDeck();
    return;
  }
  if(cfInterval){
    clearInterval(cfInterval);
    cfInterval=null;
    autoCrossfadeActive=false;
    cfCancelGuardUntil=Date.now()+3000;
    qs('crossfadeBtn')?.classList.remove('active');
    setCrossfadeTimer('');
    return;
  }

  const _queuedSep = window._jdLastStatus?.queued_separator;
  if (_queuedSep) {

    api('/api/radio/skip', {method:'POST'}).then(() => refresh()).catch(()=>{});
    return;
  }
  const goingAtoB = activeDeck==='A';
  const targetReady = goingAtoB ? !!(deckBLoaded && deckBTrackId && playerB.src) : !!player.src;
  if(!targetReady){
    setSettingsStatus(goingAtoB ? 'Deck B not loaded yet' : 'Deck A not loaded yet','error');
    return;
  }
  autoCrossfadeActive=true;
  cfCancelGuardUntil=0;
  manualStopA=false;
  manualStopB=false;
  if(goingAtoB){
    setPreviewBaseGain(playerB, 0);
    playerB.pause();
    try{ playerB.currentTime=0; }catch(_){ }
  } else {
    setPreviewBaseGain(player, 0);
    player.pause();
    try{ player.currentTime=0; }catch(_){ }
  }
  qs('crossfadeBtn')?.classList.add('active');
  let step=0;
  const startV=goingAtoB?0:1;
  const endV=goingAtoB?1:0;
  lastCrossfaderValue=startV;
  cfInterval=setInterval(()=>{
    step++;
    const pct=step/CF_STEPS;
    const ease=pct<0.5?2*pct*pct:1-Math.pow(-2*pct+2,2)/2;
    const v=startV + (endV-startV)*ease;
    const prev=lastCrossfaderValue;
    qs('crossfader').value=v;
    applyCrossfaderVolumes(v,{allowAutoplay:false});
    maybeStartCrossfadeTarget(prev, v);
    lastCrossfaderValue=v;
    qs('cfIndA').classList.toggle('active', v<0.5);
    qs('cfIndB').classList.toggle('active', v>=0.5);
    const rem=Math.ceil((CF_STEPS-step)*CF_DURATION/CF_STEPS/1000);
    setCrossfadeTimer(rem>0?rem+'s':'');
    if(step>=CF_STEPS){
      clearInterval(cfInterval);
      cfInterval=null;
      qs('crossfadeBtn')?.classList.remove('active');
      setCrossfadeTimer('');
      const finalize = goingAtoB ? promoteDeckBToAir : promoteDeckAToAir;
      finalize(true).catch(()=>{}).finally(()=>{ autoCrossfadeActive=false; });
    }
  }, CF_DURATION/CF_STEPS);
});

let vuCtxA=null, vuCtxB=null, vuAnalA=null, vuAnalB=null, vuAudio=null;

let vuSrcA=null, vuSrcB=null;

function initVU(){
  if(vuCtxA) return;
  try{
    vuAudio=new (window.AudioContext||window.webkitAudioContext)();
    vuAnalA=vuAudio.createAnalyser(); vuAnalA.fftSize=256; vuAnalA.smoothingTimeConstant=0.8;
    vuAnalB=vuAudio.createAnalyser(); vuAnalB.fftSize=256; vuAnalB.smoothingTimeConstant=0.8;
    vuSrcA=vuAudio.createMediaElementSource(player);
    vuSrcB=vuAudio.createMediaElementSource(playerB);
    vuSrcA.connect(vuAnalA); vuAnalA.connect(vuAudio.destination);
    vuSrcB.connect(vuAnalB); vuAnalB.connect(vuAudio.destination);
    vuCtxA=qs('vuA').getContext('2d');
    vuCtxB=qs('vuB').getContext('2d');
    drawVU();
  } catch(e){ console.warn('VU init failed:', e); }
}

function reinitVU(){

  if(vuAudio && vuAudio.state==='suspended') vuAudio.resume();
  if(!vuCtxA){

    return;
  }

  vuAudio.resume().catch(()=>{});
}

function getRMS(analyser){
  const buf=new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(buf);
  let sum=0; for(let i=0;i<buf.length;i++) sum+=buf[i]*buf[i];
  return Math.sqrt(sum/buf.length);
}

function drawVUBar(ctx, rms){
  const canvas=ctx.canvas;
  const W=canvas.offsetWidth||200; const H=canvas.offsetHeight||10;
  canvas.width=W; canvas.height=H;
  const db=Math.max(-60, 20*Math.log10(rms+0.000001));
  const pct=Math.max(0, Math.min(1, (db+60)/60));
  ctx.clearRect(0,0,W,H);

  const segs=Math.floor(W/4);
  for(let i=0;i<segs;i++){
    const x=i*4; const segPct=i/segs;
    ctx.fillStyle=segPct<0.6?'rgba(0,212,160,.12)':segPct<0.85?'rgba(240,180,41,.12)':'rgba(255,77,109,.1)';
    ctx.fillRect(x,0,3,H);
  }

  const activePct=pct;
  for(let i=0;i<segs;i++){
    const x=i*4; const segPct=i/segs;
    if(segPct>activePct) break;
    ctx.fillStyle=segPct<0.6?'#00d4a0':segPct<0.85?'#f0b429':'#ff4d6d';
    ctx.fillRect(x,1,3,H-2);
  }
}

function drawVU(){
  requestAnimationFrame(drawVU);
  if(!vuCtxA||!vuAnalA) return;
  drawVUBar(vuCtxA, getRMS(vuAnalA));
  drawVUBar(vuCtxB, getRMS(vuAnalB));
}

document.addEventListener('click', ()=>{
  if(!vuCtxA){ initVU(); }
  else if(vuAudio && vuAudio.state==='suspended'){ vuAudio.resume(); }
}, {once:false, passive:true});

function ensureVU(){
  if(!vuCtxA){ initVU(); return; }
  if(vuAudio && vuAudio.state==='suspended') vuAudio.resume().catch(()=>{});
}
player.addEventListener('play', ensureVU);
playerB.addEventListener('play', ensureVU);

let _lastReportedStartedId = (() => {
  try { return sessionStorage.getItem('aceradio_lastReportedStartedId') || null; }
  catch(_) { return null; }
})();
function _rememberReportedStartedId(trackId){
  _lastReportedStartedId = trackId || null;
  try{
    if(trackId) sessionStorage.setItem('aceradio_lastReportedStartedId', trackId);
    else sessionStorage.removeItem('aceradio_lastReportedStartedId');
  }catch(_){ }
}
function _reportTrackStarted(trackId) {
  if (!trackId || trackId === _lastReportedStartedId) return;
  _rememberReportedStartedId(trackId);
  api('/api/radio/track-started', {
    method: 'POST',
    body: JSON.stringify({ track_id: trackId }),
    headers: { 'Content-Type': 'application/json' },
  }).catch(() => {});
  console.debug('[AceRadio] track-started sent for', trackId);
}
player.addEventListener('play', () => {
  if (activeDeck === 'A' && currentTrackId) _reportTrackStarted(currentTrackId);
});
playerB.addEventListener('play', () => {
  if (activeDeck === 'B' && deckBTrackId) _reportTrackStarted(deckBTrackId);
});

function strHash(s){
  let h=0;
  for(let i=0;i<s.length;i++){ h=Math.imul(31,h)+s.charCodeAt(i)|0; }
  return Math.abs(h);
}

function genAlbumArt(canvasId, track){ let p;
  const canvas=qs(canvasId);
  if(!canvas) return;
  const W=canvas.width||64, H=canvas.height||64;
  canvas.width=W; canvas.height=H;
  const ctx=canvas.getContext('2d');
  if(!track){ ctx.clearRect(0,0,W,H); return; }

  const seed=strHash((track.song_title||'')+(track.tags||''));
  const rng=(n)=>{ const x=Math.sin(seed*9301+n*49297+233)*49139; return x-Math.floor(x); };

  const tags=(track.tags||'').toLowerCase();
  let palettes={
    synthwave:['#ff2d78','#9b5de5','#00f5d4'],
    jazz:['#e8a838','#d4622a','#f7e0b0'],
    metal:['#444','#888','#cc2200'],
    ambient:['#0a3d62','#1e6a9e','#56cfe1'],
    classical:['#c8a96e','#f5e6c8','#7a4f2e'],
    house:['#f72585','#7209b7','#3a0ca3'],
    reggae:['#00a651','#ffd700','#ce1126'],
    blues:['#1a3a5c','#2e6da4','#7fb3d3'],
    folk:['#8b5e3c','#c4a35a','#e8d5a3'],
  };
  let pal=['#00d4a0','#3ab4f2','#f0b429'];
  for(const [k,v] of Object.entries(palettes)){
    if(tags.includes(k)){ pal=v; break; }
  }

  const hue=Math.floor(rng(0)*360);
  const h2=(hue+120)%360, h3=(hue+240)%360;
  const hsl=(h,s,l)=>`hsl(${h},${s}%,${l}%)`;
  if(pal[0]==='#00d4a0') pal=[hsl(hue,70,55),hsl(h2,60,45),hsl(h3,80,65)];

  const style=Math.floor(rng(1)*5);
  ctx.fillStyle='#0b1018';
  ctx.fillRect(0,0,W,H);

  if(style===0){

    for(let i=4;i>=0;i--){
      ctx.beginPath();
      ctx.arc(W/2,H/2,(i+1)*4,0,Math.PI*2);
      ctx.fillStyle=((p=pal[i%pal.length]).startsWith('#')?p.slice(0,7)+(i%2===0?'cc':'66'):'rgba(128,128,128,'+(i%2===0?'0.8':'0.4')+')');
      ctx.fill();
    }
  } else if(style===1){

    for(let i=-H;i<W+H;i+=6){
      ctx.fillStyle=((p=pal[Math.floor((i/6+100))%pal.length]).startsWith('#')?p.slice(0,7)+'99':'rgba(128,128,128,0.6)');
      ctx.fillRect(i,0,4,H);
      ctx.fillStyle=((p=pal[Math.floor(i/6+101)%pal.length]).startsWith('#')?p.slice(0,7)+'55':'rgba(128,128,128,0.33)');
      ctx.fillRect(i,0,2,H);
    }

    ctx.save();
    ctx.translate(W/2,H/2);
    ctx.rotate(Math.PI/4);
    ctx.translate(-W/2,-H/2);
    ctx.drawImage(canvas,0,0);
    ctx.restore();
  } else if(style===2){

    const sz=6;
    for(let y=0;y<H;y+=sz){
      for(let x=0;x<W;x+=sz){
        const r=rng(x*100+y);
        if(r>0.35){
          ctx.fillStyle=((p=pal[Math.floor(r*pal.length)]).startsWith('#')?p.slice(0,7)+(r>0.7?'ff':'88'):'rgba(128,128,128,'+(r>0.7?'1.0':'0.53')+')');
          ctx.fillRect(x+1,y+1,sz-2,sz-2);
        }
      }
    }
  } else if(style===3){

    const grd=ctx.createRadialGradient(W/2,H/2,2,W/2,H/2,W*0.7);
    grd.addColorStop(0,pal[0]);
    grd.addColorStop(0.5,(pal.length>1?(pal[1].startsWith('#')?pal[1].slice(0,7)+'aa':'rgba(128,128,128,0.67)'):pal[0]));
    grd.addColorStop(1,'rgba(11,16,24,0)');
    ctx.fillStyle=grd;
    ctx.fillRect(0,0,W,H);

    for(let i=0;i<8;i++){
      const angle=rng(i)*Math.PI*2;
      ctx.save();
      ctx.translate(W/2,H/2);
      ctx.rotate(angle);
      ctx.fillStyle=((p=pal[i%pal.length]).startsWith('#')?p.slice(0,7)+'44':'rgba(128,128,128,0.27)');
      ctx.fillRect(-1,0,2,W);
      ctx.restore();
    }
  } else {

    const bars=9;
    const bw=W/bars;
    for(let i=0;i<bars;i++){
      const h=4+rng(i)*24;
      ctx.fillStyle=pal[i%pal.length];
      ctx.fillRect(i*bw+1, H/2-h/2, bw-2, h);
    }
  }

  const initial=(track.song_title||'?').charAt(0).toUpperCase();
  ctx.font='bold 14px Inter,sans-serif';
  ctx.textAlign='center';
  ctx.textBaseline='middle';
  ctx.fillStyle='rgba(0,0,0,.45)';
  ctx.fillRect(W/2-9,H/2-10,18,20);
  ctx.fillStyle='#fff';
  ctx.fillText(initial,W/2,H/2+1);
}

function updateVolDb(deck, v){
  const vol=parseFloat(v);
  const db = vol<=0 ? '-∞' : (20*Math.log10(vol)).toFixed(1)+' dB';
  const el=qs('deck'+deck+'VolDb');
  if(el) el.textContent=db;

  const slider=qs('deck'+deck+'Vol');
  if(slider) slider.style.setProperty('--vol-pct',(vol*100)+'%');
}

function getWaveDecodeContext(){
  if(!waveformDecodeCtx){
    waveformDecodeCtx=new (window.AudioContext||window.webkitAudioContext)();
  }
  return waveformDecodeCtx;
}

function drawWaveform(deck, url){
  const canvasId=deck==='A'?'waveformA':'waveformB';
  const wrapId=deck==='A'?'waveformWrapA':'waveformWrapB';
  const canvas=qs(canvasId);
  const wrap=qs(wrapId);
  if(!canvas||!wrap) return;
  if(deck==='A'){ if(waveformUrlA===url && canvas.width>0) return; waveformUrlA=url||''; }
  else { if(waveformUrlB===url && canvas.width>0) return; waveformUrlB=url||''; }
  const W=wrap.offsetWidth||280;
  const H=48;
  canvas.width=W;
  canvas.height=H;
  const ctx=canvas.getContext('2d');
  ctx.clearRect(0,0,W,H);
  if(!url) return;
  const color=deck==='A'?'rgba(0,212,160,0.65)':'rgba(60,171,242,0.65)';
  const colorDim=deck==='A'?'rgba(0,212,160,0.2)':'rgba(60,171,242,0.2)';
  fetch(url).then(r=>r.arrayBuffer())
    .then(buf=>getWaveDecodeContext().decodeAudioData(buf.slice(0)))
    .then(decoded=>{
      const raw=decoded.getChannelData(0);
      const step=Math.max(1,Math.ceil(raw.length/W));
      ctx.clearRect(0,0,W,H);
      for(let x=0;x<W;x++){
        let max=0;
        const offset=x*step;
        for(let j=0;j<step && offset+j<raw.length;j++){ const v=Math.abs(raw[offset+j]||0); if(v>max) max=v; }
        const h=Math.max(2,max*(H/2)*0.9);
        ctx.fillStyle=colorDim;
        ctx.fillRect(x,H/2-h,1,h*2);
        ctx.fillStyle=color;
        ctx.fillRect(x,H/2-h*0.4,1,h*0.8);
      }
    }).catch(()=>{
      ctx.clearRect(0,0,W,H);
      const bars=Math.floor(W/3);
      ctx.fillStyle=colorDim;
      for(let i=0;i<bars;i++){
        const h=(Math.sin((i+1)*12.345)+1)*0.25*H + H*0.18;
        ctx.fillRect(i*3,H/2-h/2,2,h);
      }
    });
}

player.addEventListener('ended',async()=>{
  if(activeDeck==='B') return;
  if(jfAdminTransitionActive()) return;
  if(deckBLoaded){
    await promoteDeckBToAir(true).catch(()=>{});
    setTimeout(()=>refresh().catch(()=>{}), 900);
  } else {
    try{ await api('/api/radio/track-ended',{method:'POST',body:JSON.stringify({track_id:currentTrackId||''}),headers:{'Content-Type':'application/json'}}); await refresh(); }catch(e){}
    setTimeout(()=>refresh().catch(()=>{}), 900);
  }
});

playerB.addEventListener('ended',async()=>{
  if(activeDeck!=='B') return;
  if(jfAdminTransitionActive()) return;
  if(player.src){
    await promoteDeckAToAir(true).catch(()=>{});
    setTimeout(()=>refresh().catch(()=>{}), 900);
  } else {
    const _endedBId=deckBTrackId; deckBLoaded=false; deckBTrackId=null;
    try{ await api('/api/radio/track-ended',{method:'POST',body:JSON.stringify({track_id:_endedBId||''}),headers:{'Content-Type':'application/json'}}); await refresh(); }catch(e){}
    setTimeout(()=>refresh().catch(()=>{}), 900);
  }
});

async function rescanCache() {
  const btn = qs('rescanCacheBtn');
  if(btn) btn.disabled = true;
  try {
    const data = await api('/api/radio/rescan-cache', {method:'POST'});
    const readyBox = qs('cacheReadyBox');
    if(readyBox) readyBox.textContent = data.ready ? `${data.ready} track${data.ready!==1?'s':''} ready` : '0 tracks';
    const onDiskBox = qs('cacheOnDiskBox');
    if(onDiskBox) onDiskBox.textContent = data.on_disk ? `${data.on_disk} track${data.on_disk!==1?'s':''}` : '0 tracks';
    const cacheInfo = qs('cacheInfo');
    if(cacheInfo) cacheInfo.textContent = data.ready ? `${data.ready} cached` : '0';
    setSettingsStatus(`Cache rebuilt: ${data.ready} ready, ${data.on_disk} on disk, ${Number(data.cleanup?.deleted||0)} incomplete removed`, 'ok');
  } catch(err) {
    setSettingsStatus(`Rebuild cache failed: ${String(err.message||err)}`, 'error');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function clearAllSongs() {
  if (!bootstrapReady) return;
  const btn = qs('clearAllSongsBtn');
  if (!confirm(tr('common.delete_all_confirm'))) return;
  btn.disabled = true;
  setSettingsStatus(tr('common.deleting_song_folders'));
  try {
    const data = await api('/api/radio/clear-all-songs', {method:'POST'});
    setSettingsStatus(tr('common.clear_all_songs_success',{count:data.removed||0}), 'ok');
    await refresh();
  } catch (err) {
    setSettingsStatus(`${tr('common.clear_all_songs_failed_prefix')} ${String(err.message||err)}`, 'error');
  } finally {
    btn.disabled = !bootstrapReady;
  }
}

async function clearCache() {
  if(!confirm(tr('common.delete_cache_confirm_prefix') + (qs('reservoirTarget')?.value||'N') + tr('common.delete_cache_confirm_suffix'))) return;
  const btn = qs('clearCacheBtn');
  if(btn) btn.disabled = true;
  try {
    const data = await api('/api/radio/clear-cache', {method:'POST'});
    const readyBox = qs('cacheReadyBox');
    if(readyBox) readyBox.textContent = data.ready ? `${data.ready} track${data.ready!==1?'s':''} ready` : '0 tracks';
    const onDiskBox = qs('cacheOnDiskBox');
    if(onDiskBox) onDiskBox.textContent = data.on_disk ? `${data.on_disk} track${data.on_disk!==1?'s':''}` : '0 tracks';
    const cacheInfo = qs('cacheInfo');
    if(cacheInfo) cacheInfo.textContent = data.ready ? `${data.ready} cached` : '0';
    setSettingsStatus(`Cache trimmed: kept ${data.kept}, deleted ${data.removed} of ${data.total} folders (protected ${data.protected||0})`, 'ok');
  } catch(err) {
    setSettingsStatus(`Clear cache failed: ${String(err.message||err)}`, 'error');
  } finally {
    if(btn) btn.disabled = false;
  }
}

let streamRunning = false;
let streamPresetApplyInFlight = false;
const STREAM_PRESETS = {
  listen2myradio_free: {
    protocol: 'shoutcast',
    host: '',
    port: 0,
    mount: '',
    username: '',
    bitrate: 128,
    format: 'mp3',
    help: 'Listen2MyRadio Free (Shoutcast v1): enter host/IP, panel port, and broadcasting password only. AceRadio now mirrors BUTT: it keeps the panel port in config and connects to the native source socket on port+1.',
  },
  listen2myradio_shoutcast2_autodj: {
    protocol: 'shoutcast2',
    host: '',
    port: 0,
    mount: '1',
    username: '',
    bitrate: 128,
    format: 'mp3',
    help: 'Listen2MyRadio SHOUTcast2 / AutoDJ ON: use Quick Links host/port, Stream ID (usually 1), and DJ/User credentials required by the panel for live source connections.',
  },
  listen2myradio_live_only: {
    protocol: 'shoutcast',
    host: '',
    port: 0,
    mount: '',
    username: '',
    bitrate: 128,
    format: 'mp3',
    help: 'Listen2MyRadio Live Only / AutoDJ OFF: use the Account Overview panel port and source password. AceRadio mirrors BUTT for Shoutcast v1 by connecting on port+1 as the native source socket.',
  },
  generic_icecast2: {
    protocol: 'icecast',
    host: 'localhost',
    port: 8000,
    mount: '/stream',
    username: 'source',
    bitrate: 128,
    format: 'mp3',
    help: 'Generic Icecast 2 with username, password, and mountpoint.',
  },
  generic_shoutcast2: {
    protocol: 'shoutcast2',
    host: 'localhost',
    port: 8000,
    mount: '1',
    username: '',
    bitrate: 128,
    format: 'mp3',
    help: 'Generic SHOUTcast v2 with user/password and SID.',
  },
  generic_rtmp: {
    protocol: 'rtmp',
    host: 'localhost',
    port: 1935,
    mount: 'live/stream',
    username: '',
    bitrate: 128,
    format: 'mp3',
    help: 'Generic RTMP with stream key / path.',
  },
  generic_srt: {
    protocol: 'srt',
    host: 'localhost',
    port: 9000,
    mount: 'mystream',
    username: '',
    bitrate: 128,
    format: 'mp3',
    help: 'Generic SRT with streamid and passphrase.',
  },
};

function streamPresetValue() {
  return qs('streamPreset')?.value || 'custom';
}

function setStreamPresetCustom() {
  if (streamPresetApplyInFlight) return;
  const presetEl = qs('streamPreset');
  if (presetEl && presetEl.value !== 'custom') presetEl.value = 'custom';
  updateStreamModeUI({ preserveValues: true });
}

function shouldKeepCurrentStreamPreset(fieldId) {
  const preset = streamPresetValue();
  if (!preset || preset === 'custom') return false;
  return true;
}

function updateStreamModeUI(opts = {}) {
  const preserveValues = opts.preserveValues !== false;
  const preset = streamPresetValue();
  const proto = qs('streamProtocol').value;
  const mountRow = qs('streamMountRow');
  const mountLabel = qs('streamMountLabel');
  const userRow = qs('streamUserRow');
  const userLabel = qs('streamUserLabel');
  const passLabel = qs('streamPassLabel');
  const portLabel = qs('streamPortLabel');
  const mountEl = qs('streamMount');
  const userEl = qs('streamUser');
  const helpEl = qs('streamModeHelp');
  const presetMeta = STREAM_PRESETS[preset] || {};

  const applyHelp = (text) => { if (helpEl) helpEl.textContent = text || ''; };

  if (preset === 'listen2myradio_free') {
    userLabel.textContent = 'Username';
    userEl.placeholder = '';
    userEl.disabled = true;
    userEl.value = '';
    if (proto === 'shoutcast2') {
      mountRow.style.display = '';
      userRow.style.display = 'none';
      mountLabel.textContent = 'Stream ID (usually 1)';
      passLabel.textContent = 'Stream password';
      portLabel.textContent = 'Shoutcast2 port';
      mountEl.placeholder = '1';
      if (!preserveValues || !String(mountEl.value || '').trim()) mountEl.value = '1';
    } else {
      if (proto !== 'shoutcast' && proto !== 'shoutcast1') qs('streamProtocol').value = 'shoutcast';
      mountRow.style.display = 'none';
      userRow.style.display = 'none';
      passLabel.textContent = 'Broadcasting password';
      portLabel.textContent = 'Port';
      mountEl.value = '';
    }
    applyHelp(presetMeta.help);
  } else if (preset === 'listen2myradio_shoutcast2_autodj') {
    qs('streamProtocol').value = 'shoutcast2';
    mountRow.style.display = '';
    userRow.style.display = '';
    mountLabel.textContent = 'SID (stream number, usually 1)';
    passLabel.textContent = 'DJ password';
    userLabel.textContent = 'DJ User ID';
    portLabel.textContent = 'Quick Links SHOUTcast v2 port';
    userEl.placeholder = 'dj user id';
    userEl.disabled = false;
    mountEl.placeholder = '1';
    if (!preserveValues || !String(mountEl.value || '').trim()) mountEl.value = '1';
    applyHelp('Listen2MyRadio SHOUTcast v2 AutoDJ ON: use the Quick Links v2 port, DJ User ID, DJ password, and SID.');
  } else if (preset === 'listen2myradio_live_only') {
    qs('streamProtocol').value = 'shoutcast';
    mountRow.style.display = 'none';
    userRow.style.display = 'none';
    userLabel.textContent = 'Username';
    passLabel.textContent = 'Source password';
    portLabel.textContent = 'Account Overview port';
    userEl.disabled = true;
    userEl.value = '';
    mountEl.value = '';
    applyHelp(presetMeta.help);
  } else if (proto === 'icecast' || proto === 'icecast2') {
    mountRow.style.display = '';
    userRow.style.display = '';
    mountLabel.textContent = 'Mountpoint';
    passLabel.textContent = 'Password';
    portLabel.textContent = 'Port';
    userLabel.textContent = 'Username';
    userEl.placeholder = 'source';
    userEl.disabled = false;
    mountEl.placeholder = '/stream';
    if (!preserveValues || !String(mountEl.value || '').trim()) mountEl.value = '/stream';
    applyHelp((STREAM_PRESETS[preset] || {}).help || 'Icecast 2 with username, password, and mountpoint.');
  } else if (proto === 'shoutcast') {
    mountRow.style.display = 'none';
    userRow.style.display = 'none';
    mountLabel.textContent = 'Mountpoint';
    passLabel.textContent = 'Password';
    portLabel.textContent = 'Port';
    userLabel.textContent = 'Username';
    userEl.disabled = true;
    if (!preserveValues || !String(userEl.value || '').trim()) userEl.value = '';
    applyHelp((STREAM_PRESETS[preset] || {}).help || 'SHOUTcast v1 legacy with source password.');
  } else if (proto === 'shoutcast2') {
    mountRow.style.display = '';
    userRow.style.display = '';
    mountLabel.textContent = 'SID (stream number, usually 1)';
    passLabel.textContent = 'Password';
    portLabel.textContent = 'Port';
    userLabel.textContent = 'User ID / Username';
    userEl.placeholder = 'user id';
    userEl.disabled = false;
    mountEl.placeholder = '1';
    if (!preserveValues || !String(mountEl.value || '').trim()) mountEl.value = '1';
    applyHelp((STREAM_PRESETS[preset] || {}).help || 'SHOUTcast v2 with user/password and SID.');
  } else if (proto === 'rtmp') {
    mountRow.style.display = '';
    userRow.style.display = 'none';
    mountLabel.textContent = 'Stream key / path';
    passLabel.textContent = 'Password / token';
    portLabel.textContent = 'Port';
    userEl.disabled = true;
    mountEl.placeholder = 'live/stream';
    if (!preserveValues || !String(mountEl.value || '').trim()) mountEl.value = 'live/stream';
    applyHelp((STREAM_PRESETS[preset] || {}).help || 'Generic RTMP.');
  } else if (proto === 'srt') {
    mountRow.style.display = '';
    userRow.style.display = 'none';
    mountLabel.textContent = 'Stream ID';
    passLabel.textContent = 'Passphrase';
    portLabel.textContent = 'Port';
    userEl.disabled = true;
    mountEl.placeholder = 'mystream';
    if (!preserveValues || !String(mountEl.value || '').trim()) mountEl.value = 'mystream';
    applyHelp((STREAM_PRESETS[preset] || {}).help || 'Generic SRT.');
  }
  updateStreamPreview();
}

function applyStreamPreset(name) {
  const preset = STREAM_PRESETS[name];
  streamPresetApplyInFlight = true;
  try {
    if (preset?.protocol != null) qs('streamProtocol').value = preset.protocol;
    if (preset?.host != null && !qs('streamHost').value.trim()) qs('streamHost').value = preset.host;
    if (preset?.port != null && preset.port && !String(qs('streamPort').value || '').trim()) qs('streamPort').value = preset.port;
    if (preset?.mount != null && !qs('streamMount').value.trim()) qs('streamMount').value = preset.mount;
    if (preset?.username != null && !qs('streamUser').value.trim()) qs('streamUser').value = preset.username;
    if (preset?.bitrate != null && !String(qs('streamBitrate').value || '').trim()) qs('streamBitrate').value = preset.bitrate;
    if (preset?.format != null && !qs('streamFormat').value) qs('streamFormat').value = preset.format;
    updateStreamModeUI({ preserveValues: true });
  } finally {
    streamPresetApplyInFlight = false;
  }
}

function streamConfig() {
  const preset = streamPresetValue();
  const protocol = qs('streamProtocol').value;
  let mount = qs('streamMount').value.trim();
  let username = qs('streamUser').value.trim();
  if (preset === 'listen2myradio_free') {
    username = '';
    mount = protocol === 'shoutcast2' ? (mount || '1') : '';
  } else if (preset === 'listen2myradio_shoutcast2_autodj') {
    mount = mount || '1';
  } else if (preset === 'listen2myradio_live_only') {
    mount = '';
    username = '';
  } else if (!mount) {
    mount = protocol === 'shoutcast2' ? '1' : (protocol === 'icecast' ? '/stream' : '');
  }
  return {
    stream_preset: preset,
    protocol,
    host:        qs('streamHost').value.trim(),
    port:        Number(qs('streamPort').value) || 8000,
    mount,
    username,
    password:    qs('streamPass').value,
    bitrate:     Number(qs('streamBitrate').value) || 128,
    format:      qs('streamFormat').value,
    name:        qs('streamName').value.trim() || 'AceRadio',
    description: qs('streamDesc').value.trim(),
    genre:       qs('streamGenre').value.trim(),
    public:      qs('streamPublic').checked,
  };
}

function sanitizePreviewConfig(cfg) {
  const copy = { ...cfg, password: cfg.password ? '***' : '' };
  return JSON.stringify(copy, null, 2);
}

function updateStreamPreview() {
  renderStreamLog();
}

function updateGenerationSourceUI() {
  const modeSelect = qs('generationMode');
  const catalogRow = qs('catalogSourceRow');
  const catalogSelect = qs('catalogSource');
  const fileChanceRow = qs('generationSourceBothRow');
  const fileChanceInput = qs('generationSourceBothPercent');
  if (!modeSelect) return;
  const customActive = customCatalogOverrideActive();
  const coerced = coerceGenerationControlState(modeSelect.value, catalogSelect?.value);
  const mode = coerced.mode;
  if (modeSelect.value !== mode) modeSelect.value = mode;
  if (catalogSelect && catalogSelect.value !== coerced.catalog) catalogSelect.value = coerced.catalog;
  const hybridOption = modeSelect.querySelector('option[value="hybrid"]');
  if (hybridOption) hybridOption.disabled = !!coerced.hybridBlocked;
  const catalogEnabled = !customActive && mode !== 'ai_generated';
  const hybridEnabled = !customActive && mode === 'hybrid';
  modeSelect.disabled = !!customActive;
  setFieldRowVisible(catalogRow, catalogEnabled);
  setFieldRowVisible(fileChanceRow, hybridEnabled);
  if (catalogSelect) {
    catalogSelect.disabled = !catalogEnabled;
    catalogSelect.tabIndex = catalogEnabled ? 0 : -1;
  }
  if (fileChanceInput) {
    fileChanceInput.disabled = !hybridEnabled;
    fileChanceInput.tabIndex = hybridEnabled ? 0 : -1;
  }
}
const automaticDurationEl = qs('automaticDuration');
if (automaticDurationEl) {
  automaticDurationEl.addEventListener('change', ()=>{ updateAutomaticDurationUI(); scheduleLiveApply(); });
}

function formatStreamLogLines(lines) {
  return (lines || []).filter(Boolean).join('\n');
}

function renderStreamLog() {
  const box = qs('streamLogBox');
  if (!box) return;
  if (!streamLogEntries.length) {
    box.value = tr('common.validate_errors_log');
    box.className = 'stream-log-box empty';
    return;
  }
  box.className = 'stream-log-box';
  box.value = streamLogEntries.map(entry => `[${entry.stamp}] ${entry.title}\n${entry.body}`).join('\n\n------------------------------\n\n');
  box.scrollTop = box.scrollHeight;
}
loadAdminLog();
renderAdminLog();
const clearAdminLogBtn=qs('clearAdminLogBtn');
if(clearAdminLogBtn) clearAdminLogBtn.onclick=()=>clearAdminLog();
window.addEventListener('error', evt=>{
  const msg=String(evt?.message||'Script error').trim();
  const file=evt?.filename?`${evt.filename}:${evt.lineno||0}:${evt.colno||0}`:'';
  appendAdminLog('error','JavaScript error',[msg,file], `js-error|${msg}|${file}`);
});
window.addEventListener('unhandledrejection', evt=>{
  const reason=evt?.reason;
  const msg=reason?.message||String(reason||'Unhandled promise rejection');
  appendAdminLog('error','Unhandled promise rejection',[msg], `unhandled-rejection|${msg}`);
});

function appendStreamLog(kind, title, lines, dedupeKey='') {
  const body = formatStreamLogLines(Array.isArray(lines) ? lines : [lines]);
  const key = dedupeKey || `${kind}|${title}|${body}`;
  if (streamLogSeen.has(key)) return;
  streamLogSeen.add(key);
  streamLogEntries.push({
    kind: kind || 'info',
    title: title || 'Streaming event',
    body,
    stamp: new Date().toLocaleString(),
  });
  if (streamLogEntries.length > 200) streamLogEntries = streamLogEntries.slice(-200);
  renderStreamLog();
}

function logValidationResult(result, source='Check streaming') {
  if (!result || (!result.reason && !result.stderr_tail && !result.target_url)) return;
  const lines = [];
  const blob = `${result.reason || ''}
${result.stderr_tail || ''}`.toLowerCase();
  const isListen2MyRadio = ['listen2myradio_free', 'listen2myradio_live_only', 'listen2myradio_shoutcast2_autodj'].includes(result.mode);
  const is10053 = blob.includes('10053');
  if (result.mode) lines.push(`Mode: ${result.mode}`);
  if (result.login_summary) lines.push(`Login: ${result.login_summary}`);
  if (result.target_url) lines.push(`Target: ${result.target_url}`);
  if (isListen2MyRadio && is10053) {
    lines.push('DIAGNOSTICS -10053');
    if (result.mode === 'listen2myradio_free') {
      lines.push('1) Check first whether the provider-side Free endpoint is currently restricting source sessions');
      lines.push('2) Check next that this is really the correct publish/source port');
      lines.push('3) Check last that the broadcasting or stream password is correct');
    } else if (result.mode === 'listen2myradio_live_only') {
      lines.push('1) Check first the real Account Overview source port');
      lines.push('2) Check next that AutoDJ is truly OFF');
      lines.push('3) Check last that the source password is correct');
    } else if (result.mode === 'listen2myradio_shoutcast2_autodj') {
      lines.push('1) Check first the Quick Links SHOUTcast v2 port');
      lines.push('2) Check next the DJ User ID and DJ password');
      lines.push('3) Check last the SID');
    }
  }
  if (result.reason) lines.push(`Reason: ${result.reason}`);
  if (result.stderr_tail) lines.push(`FFmpeg: ${result.stderr_tail}`);
  appendStreamLog(result.ok ? 'ok' : 'error', source, lines, `validation|${result.ok}|${result.mode||''}|${result.target_url||''}|${result.reason||''}|${result.stderr_tail||''}`);
}

function renderStreamStatus(data) {

  const bar  = qs('streamStatusBar');
  const txt  = qs('streamStatusText');
  const btn  = qs('streamBtn');
  const info = qs('streamQuickInfo');

  streamRunning = !!data?.running;

  const profile = data?.audio_profile || {};
  const bitrate = Number(profile?.bitrate_kbps || data?.bitrate || 0);
  const sampleRateHz = Number(profile?.sample_rate_hz || 0);
  const streamRateKbps = Number(data?.stream_rate_kbps || 0);
  const declaredKbps = Number(data?.stream_declared_kbps || data?.bitrate || 0);
  const declaredFmt  = (data?.stream_declared_format || '').toUpperCase() || 'MP3';
  const sampleRateText = sampleRateHz > 0 ? `${(sampleRateHz / 1000).toFixed(sampleRateHz % 1000 === 0 ? 0 : 1)} kHz` : '';

  const primaryRate = declaredKbps > 0 ? `Stream at ${declaredKbps} kbps · ${declaredFmt}` : '';
  const measuredDiffers = streamRateKbps > 0 && declaredKbps > 0 && Math.abs(streamRateKbps - declaredKbps) > declaredKbps * 0.15;
  const streamRateText = primaryRate || (streamRateKbps > 0 ? `Stream at ${streamRateKbps.toFixed(0)} kbps` : '');

  if (streamRunning) {
    bar.className  = 'stream-status-bar live';
    txt.textContent = `LIVE · ${data.preset || data.protocol?.toUpperCase()}`;
    btn.textContent = tr('common.stop_stream');
    btn.classList.add('live');
    const liveRate = streamRateText || tr('common.stream_at_dash');
    const audioRate = bitrate > 0 ? `${bitrate} kbps` : '—';
    const audioHz = sampleRateText || '—';
    const measuredPill = measuredDiffers
      ? `<span class="stream-stat-pill" title="Measured throughput (internal)">⚠ ${streamRateKbps.toFixed(0)} kbps measured</span>`
      : '';
    info.innerHTML = `
      <span class="stream-live-pill"><span class="stream-live-dot"></span>${liveRate}</span>
      <span class="stream-stat-pill"><strong>Audio</strong> ${audioRate} / ${audioHz}</span>
      ${measuredPill}
    `;
  } else {
    bar.className  = 'stream-status-bar off';
    txt.textContent = data?.error ? `${tr('common.error')}: ${data.error}` : tr('common.not_streaming');
    btn.textContent = tr('common.start_stream');
    btn.classList.remove('live');
    info.innerHTML = '';
  }
}

async function refreshStreamStatus() {
  try {
    const data = await api('/api/stream/status');
    renderStreamStatus(data);
    if (data && data.running === false && data.error) {
      appendStreamLog('error', 'Stream runtime error', [
        data.preset ? `Preset: ${data.preset}` : '',
        data.login_summary ? `Login: ${data.login_summary}` : '',
        data.target_url ? `Target: ${data.target_url}` : '',
        `Reason: ${data.error}`
      ], `status-error|${data.preset||''}|${data.target_url||''}|${data.error||''}`);
      appendAdminLog('error', 'Stream runtime error', [
        data.preset ? `Preset: ${data.preset}` : '',
        data.target_url ? `Target: ${data.target_url}` : '',
        `Reason: ${data.error}`
      ], `admin-status-error|${data.preset||''}|${data.target_url||''}|${data.error||''}`);
    }
  } catch (_) {}
}

qs('streamValidateBtn').onclick = async () => {
  if (!bootstrapReady) return;
  const btn = qs('streamValidateBtn');
  btn.disabled = true;
  setValidateButtonState('working',tr('common.checking'));
  try {
    const result = await api('/api/stream/validate', { method: 'POST', body: JSON.stringify(streamConfig()) });
    logValidationResult(result, tr('common.check_streaming'));
    setValidateButtonState(result.ok ? 'ok' : 'error', result.ok ? tr('common.streaming_ok') : 'ERROR');
    appendAdminLog(result.ok ? 'info' : 'error', 'Stream validation', [result.reason||'Validation completed', result.target_url ? `Target: ${result.target_url}` : ''].filter(Boolean), `stream-validate|${!!result.ok}|${result.reason||''}|${result.target_url||''}`);
    const txt = qs('streamStatusText');
    if (txt) txt.textContent = result.ok ? `${tr('common.validation_ok')} · ${result.reason}` : tr('common.validation_failed_with_reason',{reason:result.reason});
  } catch (err) {
    const msg = String(err.message || err);
    setValidateButtonState('error','ERROR');
    appendStreamLog('error', tr('common.check_streaming'), [`Reason: ${msg}`, `FFmpeg: ${msg}`]);
    appendAdminLog('error', 'Stream validation failed', [msg], `stream-validate-failed|${msg}`);
  } finally {
    btn.disabled = !bootstrapReady;
  }
};

qs('streamBtn').onclick = async () => {
  if (!bootstrapReady) return;
  const btn = qs('streamBtn');
  const stopping = !!streamRunning;
  btn.disabled = true;
  btn.textContent = stopping ? tr('common.stopping_stream') : `🔎 ${tr('common.checking')}`;
  try {
    if (stopping) {
      await api('/api/stream/stop', { method: 'POST' });
      await refreshStreamStatus();
    } else {
      const cfg = streamConfig();
      const validation = await api('/api/stream/validate', { method: 'POST', body: JSON.stringify(cfg) });
      logValidationResult(validation, tr('common.check_streaming'));
      const txt = qs('streamStatusText');
      if (validation && validation.ok) {
        appendAdminLog('info', 'Stream validation', [validation.reason||tr('common.validation_ok'), validation.target_url ? `Target: ${validation.target_url}` : ''].filter(Boolean), `stream-prestart-validate|ok|${validation.reason||''}|${validation.target_url||''}`);
        if (txt) txt.textContent = tr('common.validation_ok_starting');
      } else {
        const msg = (validation && validation.reason) ? validation.reason : tr('common.validation_failed');
        appendAdminLog('error', 'Stream validation failed', [msg, validation?.target_url ? `Target: ${validation.target_url}` : ''].filter(Boolean), `stream-prestart-validate|fail|${msg}|${validation?.target_url||''}`);
        if (txt) txt.textContent = tr('common.validation_failed_with_reason',{reason:msg});
        await refreshStreamStatus();
        return;
      }
      btn.textContent = tr('common.starting_stream');
      await api('/api/stream/start', { method: 'POST', body: JSON.stringify(cfg) });
      await refreshStreamStatus();
    }
  } catch (err) {
    const bar = qs('streamStatusBar');
    const txt = qs('streamStatusText');
    const msg = String(err.message || err);
    if (bar) bar.className = 'stream-status-bar';
    if (txt) txt.textContent = `${tr('common.stream_error_prefix')} ${msg}`;
    appendAdminLog('error', stopping ? tr('common.stop_stream_failed') : tr('common.start_stream_failed'), [msg], `stream-click-admin|${stopping?'stop':'start'}|${msg}`);
    appendStreamLog('error', stopping ? tr('common.stop_stream_failed') : tr('common.start_stream_failed'), [`Reason: ${msg}`], `stream-click|${stopping?'stop':'start'}|${msg}`);
    btn.textContent = stopping ? tr('common.stop_stream') : tr('common.start_stream');
  } finally {
    btn.disabled = !bootstrapReady;
  }
};

setInterval(() => { if (bootstrapReady) refreshStreamStatus(); }, 2500);

const customCatalogBrowseBtn = qs('customCatalogBrowseBtn');
const customCatalogApplyBtn = qs('customCatalogApplyBtn');
const customCatalogRemoveBtn = qs('customCatalogRemoveBtn');
const customCatalogFileInput = qs('customCatalogFileInput');
if (customCatalogBrowseBtn) { customCatalogBrowseBtn.onclick = async () => { if (!bootstrapReady) return; customCatalogBrowseBtn.disabled = true; setSettingsStatus(tr('common.opening_catalog_dialog')); try { const data = await api('/api/custom-catalog/browse', { method:'POST', body: JSON.stringify({}) }); if (data.cancelled || !data.path) { setSettingsStatus(tr('common.catalog_selection_cancelled')); return; } pendingCustomCatalogFile = { path:String(data.path||''), name:String(data.name||'').trim() || String(data.path||'').split(/[\\/]/).pop() || 'selected catalog', songCount:Number(data.song_count||0), ignoredCount:Number(data.ignored_count||0) }; updateCustomCatalogUI(); setSettingsStatus(tr('common.custom_catalog_selected_ok',{name:pendingCustomCatalogFile.name}),'ok'); } catch (err) { setSettingsStatus(`${tr('common.custom_catalog_browse_failed')} ${String(err.message||err)}`, 'error'); } finally { customCatalogBrowseBtn.disabled = !bootstrapReady; updateCustomCatalogUI(); } }; }
if (customCatalogFileInput) { customCatalogFileInput.value = ''; }
if (customCatalogApplyBtn) { customCatalogApplyBtn.onclick = async () => { if (!bootstrapReady || !pendingCustomCatalogFile || !pendingCustomCatalogFile.path) return; customCatalogApplyBtn.disabled = true; setSettingsStatus(tr('common.applying_custom_catalog')); try { const data = await api('/api/custom-catalog/apply-path',{method:'POST', body:JSON.stringify({path:pendingCustomCatalogFile.path})}); pendingCustomCatalogFile = null; if (customCatalogFileInput) customCatalogFileInput.value = ''; await reloadUiFromActiveSettings(); let msg = tr('common.custom_catalog_active_ok',{count:Number(data.song_count||0)}); if (Number(data.ignored_count||0) > 0) msg += tr('common.custom_catalog_ignored_ok',{count:Number(data.ignored_count||0)}); if (lastRunning) { await api('/api/radio/apply-settings', { method:'POST', body: JSON.stringify(currentSettings()) }); msg += ` · ${tr('common.applied_to_radio')}`; } setSettingsStatus(msg, 'ok'); } catch (err) { setSettingsStatus(`${tr('common.custom_catalog_apply_failed')} ${String(err.message||err)}`, 'error'); } finally { updateCustomCatalogUI(); } }; }
if (customCatalogRemoveBtn) { customCatalogRemoveBtn.onclick = async () => { if (!bootstrapReady || !customCatalogOverrideActive()) return; customCatalogRemoveBtn.disabled = true; setSettingsStatus(tr('common.removing_custom_catalog')); try { await api('/api/custom-catalog/remove', { method:'POST', body: JSON.stringify({}) }); pendingCustomCatalogFile = null; if (customCatalogFileInput) customCatalogFileInput.value = ''; await reloadUiFromActiveSettings(); let msg = tr('common.custom_catalog_removed'); if (lastRunning) { await api('/api/radio/apply-settings', { method:'POST', body: JSON.stringify(currentSettings()) }); msg += ` · ${tr('common.applied_to_radio')}`; } setSettingsStatus(msg, 'ok'); } catch (err) { setSettingsStatus(`${tr('common.custom_catalog_remove_failed')} ${String(err.message||err)}`, 'error'); } finally { updateCustomCatalogUI(); } }; }

const generationModeEl = qs('generationMode');
const catalogSourceEl = qs('catalogSource');
const syncGenerationSourceUI = () => {
  state.generationSourceDirty = true;
  const coerced = coerceGenerationControlState(qs('generationMode')?.value, qs('catalogSource')?.value);
  if (qs('generationMode') && qs('generationMode').value !== coerced.mode) qs('generationMode').value = coerced.mode;
  if (qs('catalogSource') && qs('catalogSource').value !== coerced.catalog) qs('catalogSource').value = coerced.catalog;
  hiddenSettingsState = { ...hiddenSettingsState, generation_source: deriveLegacyGenerationSource(coerced.mode), generation_mode: coerced.mode, catalog_source: coerced.catalog };
  updateGenerationSourceUI();
  requestAnimationFrame(updateGenerationSourceUI);
  scheduleLiveApply();
};
if (generationModeEl) {
  generationModeEl.addEventListener('change', syncGenerationSourceUI);
  generationModeEl.addEventListener('input', syncGenerationSourceUI);
}
if (catalogSourceEl) {
  catalogSourceEl.addEventListener('change', syncGenerationSourceUI);
  catalogSourceEl.addEventListener('input', syncGenerationSourceUI);
}

['streamHost','streamPort','streamMount','streamUser','streamPass','streamBitrate','streamFormat','streamName','streamDesc','streamGenre','streamPublic','streamProtocol','streamPreset'].forEach(id=>{
  const el=qs(id);
  if(!el) return;
  const evt=(el.tagName==='SELECT'||el.type==='checkbox'||el.type==='number')?'change':'input';
  el.addEventListener(evt, ()=>{
    resetValidateButtonState();
    if(id==='streamPreset') {
      applyStreamPreset(el.value);
      return;
    }
    if(id==='streamProtocol') {
      updateStreamModeUI({ preserveValues: true });
      const currentPreset = streamPresetValue();
      if (!(currentPreset === 'listen2myradio_free' && ['shoutcast','shoutcast1','shoutcast2'].includes(el.value))) {
        setStreamPresetCustom();
      }
      updateStreamPreview();
      return;
    }
    updateStreamPreview();
  });
});

updateAutomaticDurationUI();
updateGenerationSourceUI();
updateCustomCatalogUI();
updateGenerationSourceUI();
updateStreamModeUI({ preserveValues: true });
renderStreamLog();

async function checkAuth() {
  try {
    const data = await fetch('/api/auth/status').then(r => r.json());
    if (data.auth_enabled) {
      const btn = qs('logoutBtn');
      if (btn) btn.style.display = '';
    }
    if (data.auth_enabled && !data.authenticated) {
      window.location.href = '/login';
    }
  } catch (_) {}
}

async function doLogout() {
  try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (_) {}
  window.location.href = '/login';
}

checkAuth();

let _jingleData   = [];

const _jingleDrafts = {};

function fmtJingleTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

async function jingleReload() {
  try {
    await fetch('/api/jingles/reload', {method:'POST'});
    await refreshJingles();
    await jdRefresh();
  } catch (e) { console.warn('[jingle] reload failed', e); }
}

async function jinglePlayOverlay(filename) {
  try {
    const r = await fetch('/api/jingles/play/overlay', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(filename ? {filename} : {})

    });
    const d = await r.json();
    if (r.status === 409) {
      showJingleToast('⚠ ' + (d.detail || tr('common.jingle_active')));
      return;
    }
    if (d.ok) {
      showJingleToast('🎙 Overlay: ' + (d.event?.filename || '?'));
      await refreshJingles();
      if (typeof jfAdminHandleEvent === 'function' && d.event) jfAdminHandleEvent(d.event);
    }
    else showJingleToast('⚠ ' + (d.detail || 'Error'));
  } catch (e) { showJingleToast('⚠ ' + e.message); }
}

async function jinglePlaySeparator(filename) {
  try {
    const r = await fetch('/api/jingles/play/separator', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(filename ? {filename} : {})

    });
    const d = await r.json();
    if (r.status === 409) {
      showJingleToast('⚠ ' + (d.detail || tr('common.jingle_active')));
      return;
    }
    if (d.ok) {
      showJingleToast('⏩ Separator: ' + (d.event?.filename || '?'));
      await refreshJingles();
      if (typeof jfAdminHandleEvent === 'function' && d.event) jfAdminHandleEvent(d.event);
    }
    else showJingleToast('⚠ ' + (d.detail || 'Error'));
  } catch (e) { showJingleToast('⚠ ' + e.message); }
}

async function jingleSave(filename, mode) {
  const safeId = mode + '_' + filename.replace(/[^a-zA-Z0-9._-]/g, '_');
  const card = document.getElementById('jingle_' + safeId);
  if (!card) return;
  const updates = {
    enabled: card.querySelector('.jingle-enabled-chk').checked,
    every_n_songs: parseInt(card.querySelector('.jingle-every-n').value) || 3,
    volume: parseFloat(card.querySelector('.jingle-vol-input').value) || 1.0,

  };
  try {
    const r = await fetch('/api/jingles/config', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({filename, mode, updates})
    });
    const d = await r.json();
    if (d.ok) {

      delete _jingleDrafts[mode + '::' + filename];
      showJingleToast('✓ Saved: ' + filename);

      const jEntry = _jingleData.find(j => j.filename === filename && j.mode === mode);
      if (jEntry) { jEntry.enabled = updates.enabled; jEntry.every_n_songs = updates.every_n_songs; jEntry.volume = updates.volume; }

      if (_jingleData.length) renderJingles({ jingles: _jingleData, jingle_event: null });
    } else {
      showJingleToast('⚠ ' + (d.detail||'Save failed'));
    }
  } catch (e) { showJingleToast('⚠ ' + e.message); }
}

function showJingleToast(msg) {
  const container = qs('jingleList') || document.body;
  const prev = container.querySelector('.jingle-toast');
  if (prev) prev.remove();
  const t = document.createElement('div');
  t.className = 'jingle-toast';
  t.style.cssText = 'margin-top:6px;padding:5px 10px;border-radius:6px;font-size:.68rem;' +
    "font-family:monospace;background:rgba(0,212,160,.1);border:1px solid rgba(0,212,160,.3);color:#00d4a0";
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

function _jingleMarkDirty(key, field, value) {
  if (!_jingleDrafts[key]) _jingleDrafts[key] = {};
  _jingleDrafts[key][field] = value;

  const [mode, ...fnParts] = key.split('::');
  const fn     = fnParts.join('::');
  const safeId = mode + '_' + fn.replace(/[^a-zA-Z0-9._-]/g, '_');
  const card   = document.getElementById('jingle_' + safeId);
  if (card) {
    card.classList.add('jingle-card-dirty');
    const badge = card.querySelector('.jingle-unsaved');
    if (badge) badge.style.display = '';
  }
}

function renderJingles(data) {

  if (Array.isArray(data.jingles)) _jingleData = data.jingles;

  const sinceOv  = qs('jingleSinceOverlay');
  const sinceSep = qs('jingleSinceSeparator');
  if (sinceOv)  sinceOv.textContent  = (data.songs_since_overlay  ?? '?') + ' songs';
  if (sinceSep) sinceSep.textContent = (data.songs_since_separator ?? '?') + ' songs';

  const activeBar   = qs('jingleActiveBar');
  const activeLabel = qs('jingleActiveLabel');
  const ev = data.jingle_event;
  const activeFilename = (ev && ev.status === 'active') ? ev.filename  : null;
  const activeMode     = (ev && ev.status === 'active') ? ev.mode      : null;
  if (ev && ev.status === 'active' && activeBar && activeLabel) {
    activeBar.style.display = 'flex';
    activeLabel.textContent = ev.filename + ' [' + ev.mode + ']';
  } else if (activeBar) {
    activeBar.style.display = 'none';
  }

  const list = qs('jingleList');
  if (!list) return;
  if (!_jingleData.length) {
    list.innerHTML = `<div class="jingle-empty">
      <div>No jingle files found. Drop audio files into:</div>
      <code>aceradio_jingles/overlay/</code>
      <code>aceradio_jingles/separator/</code>
      <div>Then click ↺ Rescan.</div>
    </div>`;
    return;
  }

  list.innerHTML = '';
  for (const j of _jingleData) {
    const fn     = j.filename;
    const mode   = j.mode || 'overlay';
    const isOv   = mode === 'overlay';
    const draftKey = mode + '::' + fn;
    const draft    = _jingleDrafts[draftKey];
    const isDirty  = !!draft;

    const effEnabled  = draft?.enabled    !== undefined ? draft.enabled    : (j.enabled !== false);
    const effEveryN   = draft?.every_n_songs !== undefined ? draft.every_n_songs : (j.every_n_songs || 3);
    const effVolume   = draft?.volume     !== undefined ? draft.volume     : (j.volume || 1.0);
    const volPct      = Math.round(effVolume * 100);

    const safeId       = mode + '_' + fn.replace(/[^a-zA-Z0-9._-]/g, '_');
    const isNowPlaying = fn === activeFilename && mode === activeMode;
    const escapedFn    = String(fn).replace(/\\/g, '\\\\').replace(/'/g, "\\'");

    const card = document.createElement('div');
    card.className = 'jingle-card'
      + (isNowPlaying ? ' jingle-card-playing' : '')
      + (isDirty      ? ' jingle-card-dirty'   : '');
    card.id = 'jingle_' + safeId;

    card.innerHTML = `
      <div class="jingle-card-head">
        <span class="jingle-fname" title="${fn}">${fn}</span>
        <span class="jingle-mode-badge ${isOv?'jingle-mode-overlay':'jingle-mode-separator'}">${isOv?'overlay':'separator'}</span>
        ${isNowPlaying ? '<span class="jingle-now-playing">▶ NOW PLAYING</span>' : ''}
        <span class="jingle-unsaved" style="${isDirty?'':'display:none'}">● UNSAVED</span>
      </div>
      <div class="jingle-card-body">
        <div class="jingle-field"><label>Every N songs</label>
          <input class="jingle-every-n" type="number" min="1" max="100" value="${effEveryN}" style="width:60px"></div>
        <div class="jingle-field"><label>Volume</label>
          <div style="display:flex;align-items:center;gap:6px">
            <input class="jingle-vol-input" type="range" min="0" max="1" step="0.05" value="${effVolume}"
              oninput="this.nextElementSibling.textContent=Math.round(this.value*100)+'%'">
            <span class="jingle-vol-val">${volPct}%</span>
          </div></div>
        <div class="jingle-stats">
          <span><strong>Plays:</strong> ${j.play_count||0}</span>
          <span><strong>Last:</strong> ${fmtJingleTime(j.last_played_at)}</span>
        </div>
      </div>
      <div class="jingle-card-actions">
        <label class="jingle-enabled-toggle">
          <input type="checkbox" class="jingle-enabled-chk" ${effEnabled?'checked':''}> Enabled
        </label>
        <button class="jingle-play-btn" onclick="jingle${isOv?'PlayOverlay':'PlaySeparator'}('${escapedFn}')">
          ${isOv?'🎙 Play Overlay':'⏩ Play Separator'}
        </button>
        <button class="jingle-save-btn" onclick="jingleSave('${escapedFn}','${mode}')">💾 Save</button>
      </div>`;

    const chk = card.querySelector('.jingle-enabled-chk');
    const evN = card.querySelector('.jingle-every-n');
    const vol = card.querySelector('.jingle-vol-input');
    chk.addEventListener('change', () => _jingleMarkDirty(draftKey, 'enabled',      chk.checked));
    evN.addEventListener('input',  () => _jingleMarkDirty(draftKey, 'every_n_songs', parseInt(evN.value) || 3));
    vol.addEventListener('input',  () => _jingleMarkDirty(draftKey, 'volume',        parseFloat(vol.value) || 1.0));

    list.appendChild(card);
  }
}

async function refreshJingles() {
  try {
    const d = await fetch('/api/jingles/status').then(r => r.json());
    renderJingles(d);
    if (typeof jdRefresh === 'function') await jdRefresh();
  } catch (_) {}
}

let _jingleCycle = 0;
(function patchRefresh() {
  const native = window.refresh;
  if (typeof native !== 'function') return;
  window.refresh = async function() {
    await native();
    _jingleCycle++;
    if (_jingleCycle % 5 === 1) refreshJingles();
  };
})();

refreshJingles();
setInterval(refreshJingles, 20000);

let _jdData = { overlay: [], separator: [], jingle_event: null, queued_separator: null };

async function jdRefresh() {
  try {
    const d = await fetch('/api/jingles/list').then(r => r.json());
    _jdData = d;
    jdRender();
    if (typeof jfAdminHandleEvent === 'function') {
      jfAdminHandleEvent(_jdData.jingle_event);
    }
  } catch (_) {}
}

function jdClearLiveState() {
  const badge    = document.getElementById('jdStatusBadge');
  const liveFile = document.getElementById('jdLiveFile');
  const liveSt   = document.getElementById('jdLiveStatus');
  const modePill = document.getElementById('jdModePill');
  const btnOv    = document.getElementById('jdBtnOverlay');
  const btnSep   = document.getElementById('jdBtnSeparator');
  const btnQ     = document.getElementById('jdBtnQueue');
  const btnStop  = document.getElementById('jdBtnStop');
  const ovSel    = document.getElementById('jdOverlaySelect');
  const sepSel   = document.getElementById('jdSeparatorSelect');
  if (badge)    { badge.textContent = 'IDLE'; badge.className = 'jd-status-badge'; }
  if (liveFile) liveFile.textContent   = '—';
  if (liveSt)   liveSt.textContent     = 'idle';
  if (modePill) modePill.style.display = 'none';
  if (btnOv)  btnOv.disabled  = !(ovSel?.value);
  if (btnSep) btnSep.disabled = !(sepSel?.value);
  if (btnQ)   btnQ.disabled   = !(sepSel?.value);
  if (btnStop) btnStop.disabled = true;
}

let _jdDropdownSig = '';
let _jdPendingRebuild = false;

function _jdListSig(d) {
  const ov  = (d.overlay  || []).map(j => j.filename + (j.enabled === false ? '0' : '1')).join(',');
  const sep = (d.separator || []).map(j => j.filename + (j.enabled === false ? '0' : '1')).join(',');
  return ov + '|' + sep;
}

function _jdIsSelectOpen() {
  const ae = document.activeElement;
  if (!ae) return false;
  return ae.id === 'jdOverlaySelect' || ae.id === 'jdSeparatorSelect';
}

function _jdBuildDropdowns(d, ovSel, sepSel) {
  const prevOv  = ovSel.value;
  const prevSep = sepSel.value;
  ovSel.innerHTML  = '<option value="">— none —</option>' +
    (d.overlay  || []).map(j => {
      const dis = j.enabled === false;
      return `<option value="${j.filename}"${dis ? ' data-disabled="1"' : ''}>${j.filename}${dis ? ' (disabled)' : ''}</option>`;
    }).join('');
  sepSel.innerHTML = '<option value="">— none —</option>' +
    (d.separator || []).map(j => {
      const dis = j.enabled === false;
      return `<option value="${j.filename}"${dis ? ' data-disabled="1"' : ''}>${j.filename}${dis ? ' (disabled)' : ''}</option>`;
    }).join('');
  if (prevOv  && [...ovSel.options].some(o => o.value === prevOv))  ovSel.value  = prevOv;
  if (prevSep && [...sepSel.options].some(o => o.value === prevSep)) sepSel.value = prevSep;
}

(function _jdInstallBlurGuard() {
  function _onSelectBlur() {
    if (!_jdPendingRebuild) return;
    _jdPendingRebuild = false;
    const ovSel  = document.getElementById('jdOverlaySelect');
    const sepSel = document.getElementById('jdSeparatorSelect');
    if (ovSel && sepSel) _jdBuildDropdowns(_jdData, ovSel, sepSel);
  }
  function _attach() {
    const ovSel  = document.getElementById('jdOverlaySelect');
    const sepSel = document.getElementById('jdSeparatorSelect');
    if (ovSel)  ovSel.addEventListener('blur',  _onSelectBlur);
    if (sepSel) sepSel.addEventListener('blur', _onSelectBlur);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _attach);
  else _attach();
})();

function jdRender() {
  const d = _jdData;
  const ovSel  = document.getElementById('jdOverlaySelect');
  const sepSel = document.getElementById('jdSeparatorSelect');
  if (!ovSel || !sepSel) return;

  const sig = _jdListSig(d);
  if (sig !== _jdDropdownSig) {
    _jdDropdownSig = sig;
    if (_jdIsSelectOpen()) {
      _jdPendingRebuild = true;
    } else {
      _jdPendingRebuild = false;
      _jdBuildDropdowns(d, ovSel, sepSel);
    }
  }

  const elOv  = document.getElementById('jdCountOverlay');
  const elSep = document.getElementById('jdCountSep');
  if (elOv)  elOv.textContent  = (d.overlay  || []).length;
  if (elSep) elSep.textContent = (d.separator || []).length;

  const ev = d.jingle_event;
  const isActive = !!(ev && ev.status === 'active');
  const badge      = document.getElementById('jdStatusBadge');
  const liveFile   = document.getElementById('jdLiveFile');
  const liveStatus = document.getElementById('jdLiveStatus');
  const modePill   = document.getElementById('jdModePill');

  if (isActive) {
    badge.textContent  = ev.mode === 'overlay' ? 'OVERLAY ▶' : 'SEPARATOR ▶';
    badge.className    = 'jd-status-badge ' + (ev.mode === 'overlay' ? 'active-overlay' : 'active-separator');
    liveFile.textContent   = ev.filename || '—';
    liveStatus.textContent = 'playing ' + ev.mode;
    modePill.textContent   = ev.mode.toUpperCase();
    modePill.className     = 'jd-mode-pill ' + (ev.mode === 'overlay' ? 'jd-mode-overlay' : 'jd-mode-sep');
    modePill.style.display = '';
  } else {
    badge.textContent  = d.queued_separator ? 'SEP QUEUED' : 'IDLE';
    badge.className    = 'jd-status-badge' + (d.queued_separator ? ' queued' : '');
    liveFile.textContent   = '—';
    liveStatus.textContent = 'idle';
    modePill.style.display = 'none';
  }

  const qRow  = document.getElementById('jdQueuedRow');
  const qName = document.getElementById('jdQueuedName');
  if (d.queued_separator) {
    if (qRow)  qRow.style.display  = '';
    if (qName) qName.textContent   = d.queued_separator.filename || '?';
  } else {
    if (qRow) qRow.style.display = 'none';
  }

  const btnOv      = document.getElementById('jdBtnOverlay');
  const btnSep     = document.getElementById('jdBtnSeparator');
  const btnQ       = document.getElementById('jdBtnQueue');
  const btnStop    = document.getElementById('jdBtnStop');
  const btnClearQ  = document.getElementById('jdBtnClearQueue');
  if (btnOv)      btnOv.disabled      = isActive || !ovSel.value;
  if (btnSep)     btnSep.disabled     = isActive || !sepSel.value;
  if (btnQ)       btnQ.disabled       = isActive || !sepSel.value || !!d.queued_separator;
  if (btnStop)    btnStop.disabled    = !isActive;
  if (btnClearQ)  btnClearQ.disabled  = !d.queued_separator;

  const mutedHint = document.getElementById('jdMonitorMutedHint');
  if (mutedHint) mutedHint.style.display = qs('monitorMuted')?.checked ? '' : 'none';
}

(function() {
  function onSelChange() { jdRender(); }
  function attachOnce() {
    const ovSel  = document.getElementById('jdOverlaySelect');
    const sepSel = document.getElementById('jdSeparatorSelect');
    if (ovSel)  ovSel.addEventListener('change',  onSelChange);
    if (sepSel) sepSel.addEventListener('change', onSelChange);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attachOnce);
  else attachOnce();
})();

async function jdPlayOverlay() {
  document.activeElement?.blur();
  const fn  = document.getElementById('jdOverlaySelect')?.value;
  if (!fn) return;
  const vol = parseFloat(document.getElementById('jdVolumeSlider')?.value ?? 1);
  try {
    const r = await fetch('/api/jingles/play/overlay', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ filename: fn, volume: vol })
    });
    const d = await r.json();
    if (r.status === 409) {
      showJingleToast('⚠ ' + (d.detail || tr('common.jingle_active')));
      await jdRefresh();
      return;
    }
    if (d.ok) await jdRefresh();
  } catch (e) { console.warn('[jd] overlay failed', e); }
}

async function jdPlaySeparator() {
  document.activeElement?.blur();
  const fn  = document.getElementById('jdSeparatorSelect')?.value;
  if (!fn) return;
  const vol = parseFloat(document.getElementById('jdVolumeSlider')?.value ?? 1);
  try {
    const r = await fetch('/api/jingles/play/separator', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ filename: fn, volume: vol })
    });
    const d = await r.json();
    if (r.status === 409) {
      showJingleToast('⚠ ' + (d.detail || tr('common.jingle_active')));
      await jdRefresh();
      return;
    }
    if (d.ok) await jdRefresh();
  } catch (e) { console.warn('[jd] separator failed', e); }
}

async function jdQueueSeparator() {
  document.activeElement?.blur();
  const fn = document.getElementById('jdSeparatorSelect')?.value;
  if (!fn) return;
  try {
    const d = await fetch('/api/jingles/queue-separator', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ filename: fn })
    }).then(r => r.json());
    if (d.ok) await jdRefresh();
  } catch (e) { console.warn('[jd] queue failed', e); }
}

async function jdClearQueue() {
  document.activeElement?.blur();
  try {
    await fetch('/api/jingles/queue-separator', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ filename: '__clear__' })
    });
    await jdRefresh();
  } catch (e) { console.warn('[jd] clear queue failed', e); }
}

async function jdStop() {
  document.activeElement?.blur();
  try {
    await fetch('/api/jingles/stop', { method: 'POST' });
    await jdRefresh();
  } catch (e) { console.warn('[jd] stop failed', e); }
}

(function patchMainForJd() {
  const prev = window.refresh;
  if (typeof prev !== 'function') return;
  window.refresh = async function() {
    await prev();
    const st = window._jdLastStatus;
    if (st) {
      if ('jingle_event'     in st) _jdData.jingle_event     = st.jingle_event;
      if ('queued_separator' in st) _jdData.queued_separator = st.queued_separator;
      const ev = _jdData.jingle_event;
      if (!ev || ev.status !== 'active') jdClearLiveState();
      jdRender();
      jfAdminHandleEvent(_jdData.jingle_event);
    }
  };
})();

const JFA_DUCK_FADE_MS      = 300;
const JFA_DUCK_LEVEL        = 0.35;
const jfTiming = {
  admin_separator_fade_ms: 500,
  admin_overlay_pre_duck_ms: 300,
  admin_overlay_restore_ms: 700,
};
function jfClampNumber(value, fallback, min, max){ const n=Number(value); if(!Number.isFinite(n)) return fallback; return Math.min(max, Math.max(min, n)); }
function jfTimingApply(cfg={}){
  jfTiming.admin_separator_fade_ms = Math.round(jfClampNumber(cfg.admin_separator_fade_ms, 500, 0, 10000));
  jfTiming.admin_overlay_pre_duck_ms = Math.round(jfClampNumber(cfg.admin_overlay_pre_duck_ms, 300, 0, 10000));
  jfTiming.admin_overlay_restore_ms = Math.round(jfClampNumber(cfg.admin_overlay_restore_ms, 700, 0, 10000));
  return jfTiming;
}
function jfOverlayPreDuckMs(){ return jfTiming.admin_overlay_pre_duck_ms; }
function jfOverlayRestoreFadeMs(){ return jfTiming.admin_overlay_restore_ms; }
function jfSeparatorFadeMs(){ return jfTiming.admin_separator_fade_ms; }

const playerJAdmin = document.createElement('audio');
playerJAdmin.id      = 'playerJAdmin';
playerJAdmin.preload = 'auto';
playerJAdmin.muted   = !!qs('monitorMuted')?.checked;
playerJAdmin.style.display = 'none';
document.body.appendChild(playerJAdmin);

(function _unlockPlayerJAdmin() {
  let _unlocked = false;
  function _doUnlock() {
    if (_unlocked) return;
    _unlocked = true;
    playerJAdmin.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
    playerJAdmin.play().then(() => {
      playerJAdmin.pause();
      playerJAdmin.removeAttribute('src');
      playerJAdmin.load();
    }).catch(() => {
      playerJAdmin.removeAttribute('src');
    });
    ['click','keydown','touchstart','pointerdown'].forEach(ev =>
      document.removeEventListener(ev, _doUnlock, true));
  }
  ['click','keydown','touchstart','pointerdown'].forEach(ev =>
    document.addEventListener(ev, _doUnlock, { capture: true, once: false, passive: true }));
})();

let jfAdminActive      = false;
let jfAdminLastEventId = null;
let jfAdminDuckTimer   = null;
let jfAdminFadeIval    = null;
let jfAdminPreDuckVol  = 1.0;
let jfAdminHoldInterval = null;
let jfAdminHeldDeck    = null;

function jfAdminStartHold(deckPlayer) {
  jfAdminHeldDeck = deckPlayer;
  if (jfAdminHoldInterval) clearInterval(jfAdminHoldInterval);
  jfAdminHoldInterval = setInterval(() => {
    if (!jfAdminActive) { clearInterval(jfAdminHoldInterval); jfAdminHoldInterval = null; return; }
    if (jfAdminHeldDeck && getPreviewBaseGain(jfAdminHeldDeck) > JFA_DUCK_LEVEL + 0.01) {
      setPreviewBaseGain(jfAdminHeldDeck, JFA_DUCK_LEVEL);
    }
  }, 80);
}

function jfAdminStopHold() {
  if (jfAdminHoldInterval) { clearInterval(jfAdminHoldInterval); jfAdminHoldInterval = null; }
  jfAdminHeldDeck = null;
}

function jfAdminActiveDeck() {
  return activeDeck === 'B' ? playerB : player;
}

function jfAdminTransitionActive() {
  const ev = _jdData && _jdData.jingle_event;
  return !!(jfAdminActive && ev && ev.status === 'active' && ev.mode === 'separator');
}

function jfAdminCancelFade() {
  if (jfAdminFadeIval)  { clearInterval(jfAdminFadeIval);  jfAdminFadeIval  = null; }
  if (jfAdminDuckTimer) { clearTimeout(jfAdminDuckTimer);  jfAdminDuckTimer = null; }
}

function jfAdminFadeTo(el, target, ms, cb) {
  jfAdminCancelFade();
  const start = getPreviewBaseGain(el), diff = target - start;
  const steps = Math.max(1, Math.round(ms / 30));
  let s = 0;
  jfAdminFadeIval = setInterval(() => {
    s++;
    setPreviewBaseGain(el, Math.max(0, Math.min(1, start + diff * (s / steps))));
    if (s >= steps) {
      clearInterval(jfAdminFadeIval); jfAdminFadeIval = null;
      setPreviewBaseGain(el, target);
      if (cb) cb();
    }
  }, 30);
}

function jfAdminReset() {
  jfAdminActive = false;
  jfAdminStopHold();
}

function jfAdminCommitSeparatorPromotion(promotedTrack) {
  if (!promotedTrack || !promotedTrack.audio_url) return;
  const targetDeck = activeDeck === 'A' ? 'B' : 'A';
  loadDeckAudio(targetDeck, promotedTrack, false, 0);
  manualStopA = false;
  manualStopB = false;
  if (targetDeck === 'B') {
    player.pause();
    setPreviewBaseGain(player, 0);
    if (!(typeof jfAdminActive !== 'undefined' && jfAdminActive)) {
      setPreviewBaseGain(playerB, parseFloat(qs('deckBVol')?.value || 1));
    }
    if (playerB.paused) ensurePreviewAudioPlaying(playerB).catch(() => {});
    setDeckVisualState('B');
    qs('crossfader').value = 1;
    currentTrackId = promotedTrack.id || currentTrackId;
  } else {
    playerB.pause();
    setPreviewBaseGain(playerB, 0);
    if (!(typeof jfAdminActive !== 'undefined' && jfAdminActive)) {
      setPreviewBaseGain(player, parseFloat(qs('deckAVol')?.value || 1));
    }
    if (player.paused && player.src) ensurePreviewAudioPlaying(player).catch(() => {});
    setDeckVisualState('A');
    qs('crossfader').value = 0;
    currentTrackId = promotedTrack.id || currentTrackId;
  }
  suppressCrossfaderAutoplay = true;
  applyCrossfaderVolumes(parseFloat(qs('crossfader').value || 0), { allowAutoplay: false });
  suppressCrossfaderAutoplay = false;
}

function jfAdminConfirmEnded(eid) {
  const endedEvent = (_jdData.jingle_event && _jdData.jingle_event.event_id === eid)
    ? { ..._jdData.jingle_event }
    : null;
  const shouldPromoteTransition = !!(endedEvent && endedEvent.mode === 'separator' && endedEvent.is_transition);

  jdClearLiveState();
  if (_jdData.jingle_event && _jdData.jingle_event.event_id === eid) {
    _jdData.jingle_event = { ..._jdData.jingle_event, status: 'ended' };
  }
  fetch('/api/jingles/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_id: eid, phase: 'ended' })
  }).then(r => r.json()).then(data => {
    const promotedTrack = data?.current_track || null;
    const onAirTrackId = activeDeck === 'B' ? deckBTrackId : currentTrackId;
    const inactiveDeckTrackId = activeDeck === 'A' ? deckBTrackId : currentTrackId;
    const canPromoteLocal = !!(
      shouldPromoteTransition &&
      promotedTrack &&
      promotedTrack.audio_url &&
      promotedTrack.id &&
      inactiveDeckTrackId &&
      promotedTrack.id !== onAirTrackId &&
      promotedTrack.id === inactiveDeckTrackId
    );
    if (canPromoteLocal) {
      jfAdminCommitSeparatorPromotion(promotedTrack);
      setTimeout(() => refresh(), 80);
    } else {
      refresh();
    }
  }).catch(() => { refresh(); });
}

function jfAdminPlayOverlay(url, vol, eid) {
  if (jfAdminActive) return;
  jfAdminActive      = true;
  jfAdminLastEventId = eid;

  const deckPlayer  = jfAdminActiveDeck();
  jfAdminPreDuckVol = getPreviewBaseGain(deckPlayer);

  jfAdminFadeTo(deckPlayer, JFA_DUCK_LEVEL, JFA_DUCK_FADE_MS, () => {
    jfAdminStartHold(deckPlayer);
  });

  jfAdminDuckTimer = setTimeout(() => {
    playerJAdmin.src    = url;
    playerJAdmin.dataset.baseVolume = String(Math.max(0, Math.min(1, vol)));
    setPreviewBaseGain(playerJAdmin, Math.max(0, Math.min(1, vol)));
    playerJAdmin.load();
    playerJAdmin.muted = !!qs('monitorMuted')?.checked;

    playerJAdmin.play().catch(() => {
      jfAdminCancelFade();
      jfAdminStopHold();
      jfAdminFadeTo(deckPlayer, jfAdminPreDuckVol, jfOverlayRestoreFadeMs());
      jfAdminReset();
      jfAdminConfirmEnded(eid);
      playerJAdmin.pause();
      playerJAdmin.removeAttribute('src');
      playerJAdmin.load();
    });

    playerJAdmin.addEventListener('ended', function _ovAdmin() {
      playerJAdmin.removeEventListener('ended', _ovAdmin);
      jfAdminCancelFade();
      jfAdminStopHold();
      playerJAdmin.pause();
      playerJAdmin.removeAttribute('src');
      playerJAdmin.load();
      jfAdminFadeTo(deckPlayer, jfAdminPreDuckVol, jfOverlayRestoreFadeMs(), () => {
        jfAdminReset();
        jfAdminConfirmEnded(eid);
      });
    }, { once: true });

  }, jfOverlayPreDuckMs());
}

function jfAdminPlaySeparator(url, vol, eid) {
  if (jfAdminActive) return;
  jfAdminActive      = true;
  jfAdminLastEventId = eid;

  const deckPlayer  = jfAdminActiveDeck();
  jfAdminPreDuckVol = getPreviewBaseGain(deckPlayer);

  const SEP_TRIGGER_VOL  = 0.5;
  const SEP_FADE_TOTAL_MS = jfSeparatorFadeMs();
  const startVol = getPreviewBaseGain(deckPlayer);
  const diffToZero = startVol;
  const steps = Math.max(1, Math.round(SEP_FADE_TOTAL_MS / 30));
  let s = 0;
  let jingleStarted = false;

  jfAdminFadeIval = setInterval(() => {
    s++;
    const newVol = Math.max(0, startVol - diffToZero * (s / steps));
    setPreviewBaseGain(deckPlayer, newVol);

    if (!jingleStarted && newVol <= SEP_TRIGGER_VOL) {
      jingleStarted = true;
      playerJAdmin.src    = url;
      playerJAdmin.dataset.baseVolume = String(Math.max(0, Math.min(1, vol)));
      setPreviewBaseGain(playerJAdmin, Math.max(0, Math.min(1, vol)));
      playerJAdmin.load();
      playerJAdmin.muted = !!qs('monitorMuted')?.checked;

      playerJAdmin.play().catch(() => {
        clearInterval(jfAdminFadeIval); jfAdminFadeIval = null;
        setPreviewBaseGain(deckPlayer, jfAdminPreDuckVol);
        jfAdminReset();
        jfAdminConfirmEnded(eid);
        playerJAdmin.pause();
        playerJAdmin.removeAttribute('src');
        playerJAdmin.load();
      });

      playerJAdmin.addEventListener('ended', function _sepAdmin() {
        playerJAdmin.removeEventListener('ended', _sepAdmin);
        jfAdminCancelFade();
        playerJAdmin.pause();
        playerJAdmin.removeAttribute('src');
        playerJAdmin.load();
        setPreviewBaseGain(deckPlayer, 0);
        jfAdminReset();
        jfAdminConfirmEnded(eid);
      }, { once: true });
    }

    if (s >= steps) {
      clearInterval(jfAdminFadeIval); jfAdminFadeIval = null;
      setPreviewBaseGain(deckPlayer, 0);
      if (!jingleStarted) {
        jingleStarted = true;
        playerJAdmin.src    = url;
        playerJAdmin.dataset.baseVolume = String(Math.max(0, Math.min(1, vol)));
        setPreviewBaseGain(playerJAdmin, Math.max(0, Math.min(1, vol)));
        playerJAdmin.load();
        playerJAdmin.muted = !!qs('monitorMuted')?.checked;
        playerJAdmin.play().catch(() => {
          setPreviewBaseGain(deckPlayer, jfAdminPreDuckVol);
          jfAdminReset();
          jfAdminConfirmEnded(eid);
          playerJAdmin.pause();
          playerJAdmin.removeAttribute('src');
          playerJAdmin.load();
        });
        playerJAdmin.addEventListener('ended', function _sepAdmin2() {
          playerJAdmin.removeEventListener('ended', _sepAdmin2);
          jfAdminCancelFade();
          playerJAdmin.pause();
          playerJAdmin.removeAttribute('src');
          playerJAdmin.load();
          setPreviewBaseGain(deckPlayer, 0);
          jfAdminReset();
          jfAdminConfirmEnded(eid);
        }, { once: true });
      }
    }
  }, 30);
}

function jfAdminHandleEvent(ev) {
  if (!ev || ev.status !== 'active') {
    if (jfAdminActive) {
      jfAdminCancelFade();
      playerJAdmin.pause();
      playerJAdmin.removeAttribute('src');
      playerJAdmin.load();
      jdClearLiveState();
      jfAdminFadeTo(jfAdminActiveDeck(), jfAdminPreDuckVol, jfOverlayRestoreFadeMs(), () => {
        jfAdminReset();
        jdRender();
      });
    }
    return;
  }
  if (ev.event_id === jfAdminLastEventId) return;
  if (jfAdminActive) return;

  if (ev.mode === 'overlay') {
    jfAdminPlayOverlay(ev.audio_url, ev.volume || 1.0, ev.event_id);
  } else if (ev.mode === 'separator') {
    jfAdminPlaySeparator(ev.audio_url, ev.volume || 1.0, ev.event_id);
  }
}

jdRefresh();
