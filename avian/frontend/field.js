/* Field recordings - record a bird wherever you are, let the station name it.
 *
 * Self-mounting on purpose: this file injects its own button, panel and
 * list, and asks apt.js for nothing. apt.js is the file upstream rewrites
 * most, so every line this feature does not add there is a merge conflict
 * it cannot cause.
 *
 * The flow is deliberately record -> analyse -> CONFIRM -> keep. The
 * station offers candidates and a person picks; nothing reaches the site
 * on the model's word alone. That is what keeps guesses out of the
 * collage, and it gates the expensive side effects, since only a
 * confirmed species should ever trigger an illustration.
 *
 * Recording needs a secure context, so on http://ghlyms.local the button
 * explains itself rather than failing when pressed. The list works
 * everywhere.
 */
(function () {
  'use strict';

  var API = './avian/api/submissions.php';
  var MAX_SECONDS = 15;      // BirdNET works in 3s windows; 15 gives it five
  var POLL_MS = 1500;
  var POLL_LIMIT = 60;       // ~90s before we stop waiting on the worker

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function canRecord() {
    return !!(window.isSecureContext
      && navigator.mediaDevices && navigator.mediaDevices.getUserMedia
      && window.MediaRecorder);
  }

  /* MediaRecorder speaks different containers per browser: Chrome gives
   * webm/opus, Safari mp4/aac. Pick what the browser admits to, and let
   * the worker normalise with ffmpeg. */
  function pickMime() {
    var wanted = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/aac', 'audio/ogg'];
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
    for (var i = 0; i < wanted.length; i++) {
      if (MediaRecorder.isTypeSupported(wanted[i])) return wanted[i];
    }
    return '';
  }

  /* Position is optional and must never hold up a recording: the bird is
   * not going to wait. Resolves to null rather than rejecting. */
  function position() {
    return new Promise(function (resolve) {
      if (!navigator.geolocation) return resolve(null);
      var settled = false;
      var done = function (v) { if (!settled) { settled = true; resolve(v); } };
      setTimeout(function () { done(null); }, 8000);
      navigator.geolocation.getCurrentPosition(
        function (p) {
          done({ lat: p.coords.latitude, lon: p.coords.longitude,
                 acc: p.coords.accuracy });
        },
        function () { done(null); },
        { enableHighAccuracy: true, timeout: 7500, maximumAge: 60000 }
      );
    });
  }

  function api(action, opts) {
    return fetch(API + '?action=' + action, Object.assign(
      { credentials: 'same-origin', cache: 'no-store' }, opts || {}
    )).then(function (r) {
      return r.json().catch(function () {
        throw new Error('the station returned something unreadable (' + r.status + ')');
      });
    }).then(function (j) {
      if (!j || j.ok === false) throw new Error((j && j.error) || 'request failed');
      return j;
    });
  }

  // ---------------------------------------------------------------- UI
  var panel, body, fab, mediaRecorder, chunks = [], stopTimer, tickTimer, stream;

  function open() {
    panel.hidden = false;
    document.body.classList.add('field-open');
    showIntro();
  }

  function close() {
    stopRecording(true);
    panel.hidden = true;
    document.body.classList.remove('field-open');
  }

  function setBody(nodes) {
    body.textContent = '';
    (Array.isArray(nodes) ? nodes : [nodes]).forEach(function (n) { body.appendChild(n); });
  }

  function showIntro() {
    var wrap = el('div', 'field-intro');
    wrap.appendChild(el('p', 'field-lead',
      'Point your phone at the bird and record a few seconds. The station '
      + 'will listen and offer what it thinks it heard.'));

    if (!canRecord()) {
      var why = window.isSecureContext
        ? 'This browser has no microphone recording.'
        : 'Recording needs a secure connection. Open the site over https to record here.';
      wrap.appendChild(el('p', 'field-note', why));
    } else {
      var btn = el('button', 'field-record', 'start recording');
      btn.type = 'button';
      btn.addEventListener('click', startRecording);
      wrap.appendChild(btn);
    }

    var listBtn = el('button', 'field-link', 'see what has been caught');
    listBtn.type = 'button';
    listBtn.addEventListener('click', showList);
    wrap.appendChild(listBtn);
    setBody(wrap);
  }

  // ----------------------------------------------------------- recording
  function startRecording() {
    var constraints = { audio: {
      // Phone browsers assume speech and apply processing that is actively
      // wrong for birdsong. Ask for it off - Chrome obeys, Safari partly.
      echoCancellation: false, noiseSuppression: false, autoGainControl: false,
      channelCount: 1
    } };

    var wrap = el('div', 'field-recording');
    var meter = el('div', 'field-meter');
    var bar = el('div', 'field-meter-bar');
    meter.appendChild(bar);
    var count = el('p', 'field-count', 'listening… ' + MAX_SECONDS + 's');
    var stopBtn = el('button', 'field-stop', 'stop and send');
    stopBtn.type = 'button';
    stopBtn.addEventListener('click', function () { stopRecording(false); });
    wrap.appendChild(count);
    wrap.appendChild(meter);
    wrap.appendChild(stopBtn);
    setBody(wrap);

    navigator.mediaDevices.getUserMedia(constraints).then(function (s) {
      stream = s;
      var mime = pickMime();
      mediaRecorder = mime ? new MediaRecorder(s, { mimeType: mime }) : new MediaRecorder(s);
      chunks = [];
      mediaRecorder.addEventListener('dataavailable', function (e) {
        if (e.data && e.data.size) chunks.push(e.data);
      });
      mediaRecorder.addEventListener('stop', function () { upload(mediaRecorder.mimeType); });
      mediaRecorder.start();

      levelMeter(s, bar);

      var left = MAX_SECONDS;
      tickTimer = setInterval(function () {
        left -= 1;
        count.textContent = left > 0 ? 'listening… ' + left + 's' : 'finishing…';
      }, 1000);
      stopTimer = setTimeout(function () { stopRecording(false); }, MAX_SECONDS * 1000);
    }).catch(function (e) {
      var msg = (e && e.name === 'NotAllowedError')
        ? 'Microphone access was declined.'
        : 'Could not open the microphone: ' + ((e && e.message) || e);
      showError(msg);
    });
  }

  /* A live level bar, so it is obvious the microphone is actually hearing
   * something before you spend fifteen seconds on silence. */
  function levelMeter(s, bar) {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    try {
      var ctx = new Ctx();
      var src = ctx.createMediaStreamSource(s);
      var analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      src.connect(analyser);
      var data = new Uint8Array(analyser.frequencyBinCount);
      (function draw() {
        if (!mediaRecorder || mediaRecorder.state !== 'recording') {
          try { ctx.close(); } catch (e) { /* already gone */ }
          return;
        }
        analyser.getByteTimeDomainData(data);
        var peak = 0;
        for (var i = 0; i < data.length; i++) {
          var v = Math.abs(data[i] - 128) / 128;
          if (v > peak) peak = v;
        }
        bar.style.width = Math.min(100, Math.round(peak * 140)) + '%';
        requestAnimationFrame(draw);
      })();
    } catch (e) { /* a meter is a nicety, never a reason to fail */ }
  }

  function stopRecording(silent) {
    clearTimeout(stopTimer); clearInterval(tickTimer);
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      if (silent) mediaRecorder.onstop = null;
      try { mediaRecorder.stop(); } catch (e) { /* already stopped */ }
    }
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
  }

  // -------------------------------------------------------------- upload
  function upload(mime) {
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    if (!chunks.length) return showError('nothing was recorded');

    setBody(el('p', 'field-note', 'sending…'));
    var blob = new Blob(chunks, { type: mime || 'audio/webm' });
    chunks = [];

    position().then(function (pos) {
      var form = new FormData();
      form.append('audio', blob, 'field' + (mime && mime.indexOf('mp4') >= 0 ? '.m4a' : '.webm'));
      if (pos) {
        form.append('lat', String(pos.lat));
        form.append('lon', String(pos.lon));
        form.append('accuracy', String(Math.round(pos.acc || 0)));
      }
      return api('submit', { method: 'POST', body: form });
    }).then(function (j) {
      setBody(el('p', 'field-note', 'the station is listening to it…'));
      poll(j.id, 0);
    }).catch(function (e) { showError(String(e.message || e)); });
  }

  function poll(id, attempt) {
    if (attempt > POLL_LIMIT) {
      return showError('the station is taking too long. It may still finish - '
        + 'check the list in a minute.');
    }
    api('result&id=' + encodeURIComponent(id)).then(function (j) {
      if (j.status === 'pending' || j.status === 'analysing') {
        setTimeout(function () { poll(id, attempt + 1); }, POLL_MS);
        return;
      }
      if (j.status === 'failed') return showError(j.error || 'the recording could not be analysed');
      showCandidates(id, j.candidates || [], j.error);
    }).catch(function (e) { showError(String(e.message || e)); });
  }

  // ---------------------------------------------------------- confirming
  function showCandidates(id, candidates, note) {
    var wrap = el('div', 'field-candidates');
    if (!candidates.length) {
      wrap.appendChild(el('p', 'field-note',
        note || 'Nothing recognisable in that one. Try again closer to the bird.'));
      var again = el('button', 'field-record', 'record another');
      again.type = 'button';
      again.addEventListener('click', showIntro);
      wrap.appendChild(again);
      setBody(wrap);
      return;
    }

    wrap.appendChild(el('p', 'field-lead', 'Which one was it?'));
    candidates.forEach(function (c) {
      var row = el('button', 'field-candidate');
      row.type = 'button';
      row.appendChild(el('span', 'field-cand-com', c.com || c.sci));
      row.appendChild(el('span', 'field-cand-sci', c.sci));
      row.appendChild(el('span', 'field-cand-conf', Math.round((c.conf || 0) * 100) + '%'));
      row.addEventListener('click', function () { confirm(id, c); });
      wrap.appendChild(row);
    });

    var none = el('button', 'field-link', 'none of these - discard it');
    none.type = 'button';
    none.addEventListener('click', function () { reject(id); });
    wrap.appendChild(none);
    setBody(wrap);
  }

  function confirm(id, candidate) {
    setBody(el('p', 'field-note', 'saving…'));
    api('confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id, sci: candidate.sci })
    }).then(function (j) {
      var wrap = el('div', 'field-done');
      wrap.appendChild(el('p', 'field-lead', (j.com || j.sci) + ' - caught.'));
      var again = el('button', 'field-record', 'record another');
      again.type = 'button';
      again.addEventListener('click', showIntro);
      wrap.appendChild(again);
      var list = el('button', 'field-link', 'see everything caught');
      list.type = 'button';
      list.addEventListener('click', showList);
      wrap.appendChild(list);
      setBody(wrap);
    }).catch(function (e) { showError(String(e.message || e)); });
  }

  function reject(id) {
    api('reject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id })
    }).then(showIntro).catch(function (e) { showError(String(e.message || e)); });
  }

  // ------------------------------------------------------------- listing
  function showList() {
    setBody(el('p', 'field-note', 'loading…'));
    api('list').then(function (j) {
      var subs = j.submissions || [];
      var wrap = el('div', 'field-list');
      wrap.appendChild(el('p', 'field-lead', 'Caught in the field'));
      if (!subs.length) {
        wrap.appendChild(el('p', 'field-note', 'Nothing yet. Go and find something.'));
      }
      subs.forEach(function (s) {
        var row = el('div', 'field-row');
        var head = el('div', 'field-row-head');
        head.appendChild(el('span', 'field-cand-com', s.com || s.sci));
        head.appendChild(el('span', 'field-cand-conf',
          s.conf == null ? '' : Math.round(s.conf * 100) + '%'));
        row.appendChild(head);

        var meta = [];
        if (s.at) meta.push(new Date(s.at).toLocaleString());
        if (s.lat != null && s.lon != null) {
          meta.push(s.lat.toFixed(4) + ', ' + s.lon.toFixed(4));
        }
        if (s.who) meta.push(s.who);
        row.appendChild(el('span', 'field-row-meta', meta.join('  ·  ')));

        if (s.audio) {
          var audio = document.createElement('audio');
          audio.controls = true;
          audio.preload = 'none';
          audio.src = './' + s.audio;
          row.appendChild(audio);
        }
        wrap.appendChild(row);
      });
      var back = el('button', 'field-link', 'back');
      back.type = 'button';
      back.addEventListener('click', showIntro);
      wrap.appendChild(back);
      setBody(wrap);
    }).catch(function (e) { showError(String(e.message || e)); });
  }

  function showError(message) {
    var wrap = el('div', 'field-error');
    wrap.appendChild(el('p', 'field-note', message));
    var again = el('button', 'field-link', 'back');
    again.type = 'button';
    again.addEventListener('click', showIntro);
    wrap.appendChild(again);
    setBody(wrap);
  }

  // --------------------------------------------------------------- mount
  function mount() {
    if (document.getElementById('fieldFab')) return;

    fab = el('button', 'field-fab');
    fab.id = 'fieldFab';
    fab.type = 'button';
    fab.setAttribute('aria-label', 'record a bird in the field');
    fab.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" '
      + 'stroke="currentColor" stroke-width="1.8" stroke-linecap="round">'
      + '<rect x="9" y="2.5" width="6" height="11" rx="3"/>'
      + '<path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3.5"/></svg>';
    fab.addEventListener('click', open);

    panel = el('div', 'field-panel');
    panel.id = 'fieldPanel';
    panel.hidden = true;
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'field recording');

    var head = el('div', 'field-head');
    head.appendChild(el('span', 'field-title', 'Field recording'));
    var x = el('button', 'field-close', '×');
    x.type = 'button';
    x.setAttribute('aria-label', 'close');
    x.addEventListener('click', close);
    head.appendChild(x);

    body = el('div', 'field-body');
    panel.appendChild(head);
    panel.appendChild(body);

    document.body.appendChild(fab);
    document.body.appendChild(panel);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !panel.hidden) close();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
