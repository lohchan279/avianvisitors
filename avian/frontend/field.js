/* The Map view - where birds have been caught, and how to catch one.
 *
 * Self-mounting on purpose: this file fills the fourth view sheet and
 * asks apt.js for nothing beyond the empty section to live in. apt.js is
 * the file upstream rewrites most, so every line this feature does not
 * add there is a merge conflict it cannot cause. The whole cost in that
 * file is two lines: a view title and a clamp.
 *
 * Three things happen here.
 *
 * A map. Singapore's 55 planning areas, shaded by how many birds have
 * been caught in each. Areas rather than pins because the station only
 * ever learns a name - avian/api/places.php turns the fix into a place on
 * the way in and the coordinate never comes back out - and because a
 * neighbourhood is the honest resolution for a phone recording anyway.
 *
 * A recorder. Record a few seconds wherever you are and the station's own
 * BirdNET names it. It does not ask which of five candidates it was: the
 * person holding the phone almost never knows, and asking turns a guess
 * into a recorded fact. The model either clears the bar on its own or the
 * station says it could not make that one out.
 *
 * A list. What has been caught, newest first, with the audio - so a
 * morning's walk can be found again in the evening.
 *
 * Recording needs a secure context, so on http://ghlyms.local the button
 * explains itself rather than failing when pressed. The map and the list
 * work everywhere.
 */
(function () {
  'use strict';

  // Captured at load: document.currentScript is only meaningful while the
  // script is executing, and everything below runs later.
  var OWN_SRC = (document.currentScript && document.currentScript.src) || '';

  var API = './avian/api/submissions.php';
  var MAX_SECONDS = 15;      // BirdNET works in 3s windows; 15 gives it five
  var POLL_MS = 1500;
  var POLL_LIMIT = 60;       // ~90s before we stop waiting on the worker

  /* The drawing window, not the data's own extent. The generated bbox
   * reaches down to the industrial specks off Semakau, which is a third
   * of the height for water nobody records in. This crop keeps every
   * inhabited island and loses the empty sea. */
  var VIEW = { lon0: 103.598, lon1: 104.095, lat0: 1.180, lat1: 1.478 };
  var MAP_W = 1000;

  /* Heat steps. Discrete rather than continuous so the legend can say
   * what each shade means - a smooth ramp looks better and tells you
   * less. */
  var HEAT_STOPS = [1, 2, 4, 8, 16];
  var HEAT_OPACITY = [0.18, 0.34, 0.52, 0.72, 0.92];

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function svgEl(tag, cls) {
    var n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    if (cls) n.setAttribute('class', cls);
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
    var status = 0;
    return fetch(API + '?action=' + action, Object.assign(
      { credentials: 'same-origin', cache: 'no-store' }, opts || {}
    )).then(function (r) {
      status = r.status;
      return r.json().catch(function () {
        throw new Error('the station returned something unreadable (' + r.status + ')');
      });
    }).then(function (j) {
      if (!j || j.ok === false) {
        var failure = new Error((j && j.error) || 'request failed');
        // Carried so callers can tell "you are not signed in" - which has
        // something to do about it - from "that went wrong", which does not.
        failure.status = status;
        throw failure;
      }
      return j;
    });
  }

  // --------------------------------------------------------------- state
  var view, mapHost, mapSvg, mapLabels, legendEl, listEl, countEl, emptyEl;
  var sheet, sheetBody;
  var areaPaths = {};        // area name -> the land path drawing it
  var areaAt = {};           // area name -> [x, y] in viewBox units
  var selected = null;
  var catches = [];
  var station = null;   // the station itself: its area and what it has heard
  var mediaRecorder, chunks = [], stopTimer, tickTimer, stream;

  // ----------------------------------------------------------- the shape
  /* The boundary data is 50 KB and most visits never open this view, so
   * it is fetched on first sight rather than shipped with the page. The
   * cache token rides along from our own script tag, which is how the
   * rest of the frontend versions its assets. */
  var mapLoading = null;
  function loadShape() {
    if (window.AVIAN_SG_MAP) return Promise.resolve(window.AVIAN_SG_MAP);
    if (mapLoading) return mapLoading;
    mapLoading = new Promise(function (resolve, reject) {
      var version = /[?&]v=([^&]*)/.exec(OWN_SRC);
      var tag = document.createElement('script');
      tag.src = './sg-map.js' + (version ? '?v=' + version[1] : '');
      tag.onload = function () {
        window.AVIAN_SG_MAP ? resolve(window.AVIAN_SG_MAP)
                            : reject(new Error('the map data did not load'));
      };
      tag.onerror = function () { reject(new Error('the map data did not load')); };
      document.head.appendChild(tag);
    });
    return mapLoading;
  }

  function projector() {
    // Equirectangular. Over a city the cosine term is a rounding error,
    // but leaving it out is the kind of shortcut that is wrong somewhere
    // else later.
    var cos = Math.cos((VIEW.lat0 + VIEW.lat1) / 2 * Math.PI / 180);
    var k = MAP_W / ((VIEW.lon1 - VIEW.lon0) * cos);
    var height = (VIEW.lat1 - VIEW.lat0) * k;
    return {
      height: height,
      at: function (lon, lat) {
        return [(lon - VIEW.lon0) * cos * k, (VIEW.lat1 - lat) * k];
      }
    };
  }

  function ringPath(ring, project) {
    var out = '';
    for (var i = 0; i < ring.length; i += 2) {
      var p = project.at(ring[i], ring[i + 1]);
      out += (i ? ' ' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1);
    }
    return out + 'Z';
  }

  function drawShape(shape) {
    var project = projector();
    mapSvg.setAttribute('viewBox', '0 0 ' + MAP_W + ' ' + Math.round(project.height));

    var land = svgEl('g', 'field-land');
    var borders = '';
    areaPaths = {};
    areaAt = {};

    shape.areas.forEach(function (area) {
      var d = '';
      area.rings.forEach(function (ring) { d += ringPath(ring, project); });
      var path = svgEl('path', 'field-area');
      path.setAttribute('d', d);
      path.setAttribute('data-area', area.name);
      // Every area is a target, so tapping the sea deselects and tapping
      // a quiet district still tells you it is quiet.
      path.addEventListener('click', function () { select(area.name); });
      land.appendChild(path);
      borders += d;
      areaPaths[area.name] = path;
      areaAt[area.name] = project.at(area.at[0], area.at[1]);
    });

    var edge = svgEl('path', 'field-borders');
    edge.setAttribute('d', borders);

    mapSvg.textContent = '';
    mapSvg.appendChild(land);
    mapSvg.appendChild(svgEl('g', 'field-heat'));
    mapSvg.appendChild(edge);
    mapSvg.appendChild(svgEl('g', 'field-marks'));
  }

  function heatLevel(count) {
    var level = 0;
    for (var i = 0; i < HEAT_STOPS.length; i++) if (count >= HEAT_STOPS[i]) level = i + 1;
    return level;
  }

  /* Shading is a second path over the land rather than a different fill
   * on it: fill-opacity works in every browser that can draw an SVG,
   * where mixing two custom properties into one colour does not. */
  function paintHeat(areas, station) {
    var heat = mapSvg.querySelector('.field-heat');
    var marks = mapSvg.querySelector('.field-marks');
    if (!heat || !marks) return;
    heat.textContent = '';
    marks.textContent = '';
    mapLabels.textContent = '';

    var homeArea = station && station.area && areaAt[station.area] ? station.area : null;
    var specs = [];

    areas.forEach(function (row) {
      var base = areaPaths[row.area];
      if (!base || !row.count) return;
      var wash = svgEl('path', 'field-wash');
      wash.setAttribute('d', base.getAttribute('d'));
      wash.setAttribute('fill-opacity', String(HEAT_OPACITY[heatLevel(row.count) - 1]));
      heat.appendChild(wash);

      // Sentosa and the offshore islands are a few pixels across, so the
      // shading alone is invisible at exactly the districts most likely
      // to be worth a trip. Give a small area a dot as well.
      try {
        var span = wash.getBBox();
        if (Math.max(span.width, span.height) < 16) {
          var dot = svgEl('circle', 'field-wash field-dot');
          dot.setAttribute('cx', String(areaAt[row.area][0]));
          dot.setAttribute('cy', String(areaAt[row.area][1]));
          dot.setAttribute('r', '6');
          dot.setAttribute('fill-opacity',
            String(HEAT_OPACITY[heatLevel(row.count) - 1]));
          heat.appendChild(dot);
        }
      } catch (e) { /* getBBox throws on a detached SVG; the dot is a nicety */ }

      var isHome = row.area === homeArea;
      var note = row.count + ' caught';
      // Two labels on one district would sit exactly on top of each
      // other, so the station's own district says both things at once.
      // A station with no detections yet says nothing rather than
      // "0 species", which reads as a fault.
      if (isHome && station.species) note = station.species + ' species · ' + note;
      specs.push({
        area: row.area,
        name: isHome ? 'Home' : row.area,
        note: note,
        rank: row.count + (isHome ? 1e6 : 0)
      });
    });

    if (homeArea) {
      var mark = svgEl('circle', 'field-home');
      mark.setAttribute('cx', String(areaAt[homeArea][0]));
      mark.setAttribute('cy', String(areaAt[homeArea][1]));
      mark.setAttribute('r', '6');
      marks.appendChild(mark);

      var already = specs.some(function (s) { return s.area === homeArea; });
      if (!already) {
        specs.push({
          area: homeArea,
          name: 'Home',
          note: station.species ? station.species + ' species' : '',
          rank: 1e6
        });
      }
    }

    var selectedPath = svgEl('path', 'field-selected');
    marks.appendChild(selectedPath);

    placeLabels(specs);
    markSelection();
  }

  /* Districts are small and their names are not, so labels collide. Place
   * the busiest first and nudge each later one clear; anything with
   * nowhere to go is dropped rather than stacked, since the shading and
   * the list still say what it would have said. */
  function placeLabels(specs) {
    var box = mapSvg.viewBox.baseVal;
    var taken = [];
    specs.sort(function (a, b) { return b.rank - a.rank; });

    specs.forEach(function (spec) {
      var node = el('button', 'field-label' + (spec.name === 'Home' ? ' field-label-home' : ''));
      node.type = 'button';
      node.dataset.area = spec.area;
      node.appendChild(el('span', 'field-label-name', spec.name));
      if (spec.note) node.appendChild(el('span', 'field-label-count', spec.note));
      node.addEventListener('click', function () { select(spec.area); });
      mapLabels.appendChild(node);

      var anchor = areaAt[spec.area] || [0, 0];
      var left = anchor[0] / box.width * 100;
      var top = anchor[1] / box.height * 100;
      node.style.left = left + '%';

      var size = node.getBoundingClientRect();
      var host = mapLabels.getBoundingClientRect();
      var halfWidth = size.width / 2;
      var halfHeight = size.height / 2;
      var centreX = host.width * left / 100;

      var offsets = [0, -1, 1, -2, 2, -3, 3];
      var placed = false;
      for (var i = 0; i < offsets.length && !placed; i++) {
        var centreY = host.height * top / 100 + offsets[i] * (size.height + 3);
        var rect = [centreX - halfWidth, centreY - halfHeight,
                    centreX + halfWidth, centreY + halfHeight];
        if (rect[1] < 0 || rect[3] > host.height) continue;
        var clash = taken.some(function (other) {
          return rect[0] < other[2] && rect[2] > other[0]
              && rect[1] < other[3] && rect[3] > other[1];
        });
        if (clash) continue;
        taken.push(rect);
        node.style.top = (centreY / host.height * 100) + '%';
        placed = true;
      }
      if (!placed) node.remove();
    });
  }

  function markSelection() {
    Object.keys(areaPaths).forEach(function (name) {
      areaPaths[name].setAttribute('data-on', name === selected ? 'true' : 'false');
    });
    var outline = mapSvg.querySelector('.field-selected');
    if (outline) {
      var path = selected && areaPaths[selected];
      outline.setAttribute('d', path ? path.getAttribute('d') : '');
    }
    mapLabels.querySelectorAll('.field-label').forEach(function (node) {
      node.setAttribute('data-on', node.dataset.area === selected ? 'true' : 'false');
    });
  }

  function select(name) {
    selected = (name && name !== selected) ? name : null;
    markSelection();
    renderList();
  }

  // ---------------------------------------------------------- the legend
  function buildLegend() {
    var wrap = el('div', 'field-legend');
    wrap.appendChild(el('span', 'field-legend-label', 'fewer'));
    HEAT_OPACITY.forEach(function (opacity, index) {
      var chip = el('i', 'field-legend-chip');
      chip.style.opacity = String(opacity);
      chip.title = HEAT_STOPS[index]
        + (index + 1 < HEAT_STOPS.length ? '-' + (HEAT_STOPS[index + 1] - 1) : '+')
        + ' caught';
      wrap.appendChild(chip);
    });
    wrap.appendChild(el('span', 'field-legend-label', 'more'));
    return wrap;
  }

  // ------------------------------------------------------------ the list
  function timeText(iso) {
    if (!iso) return '';
    var when = new Date(iso);
    if (isNaN(when)) return '';
    return when.toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
    });
  }

  function atHome() {
    return !!(station && station.area && selected === station.area);
  }

  /* Home is a place on this map like any other, and the station is the
   * one that hears the most. Selecting it lists what the station itself
   * has picked up rather than the handful of clips somebody carried a
   * phone to - a map that showed only the second would be a map of the
   * exception. */
  function renderList() {
    if (atHome()) return renderStation();

    var rows = selected
      ? catches.filter(function (c) { return c.area === selected; })
      : catches;

    countEl.textContent = selected
      ? selected + ' - ' + rows.length + (rows.length === 1 ? ' catch' : ' catches')
      : rows.length + (rows.length === 1 ? ' catch' : ' catches') + ' in the field';

    listEl.textContent = '';
    if (!rows.length) {
      listEl.appendChild(el('p', 'field-note', selected
        ? 'Nothing caught in ' + selected + ' yet.'
        : 'Nothing recorded in the field yet.'));
      // An empty field list is the common case, and "nothing" reads as
      // an empty feature unless it says where the birds actually are.
      if (!selected && station && station.area) {
        var pointer = el('button', 'field-link', 'what the station has heard at home');
        pointer.type = 'button';
        pointer.addEventListener('click', function () { select(station.area); });
        listEl.appendChild(pointer);
      }
      return;
    }

    rows.forEach(function (row) { listEl.appendChild(catchRow(row)); });
  }

  /* One field catch as a row. The name opens the same species page the
   * Atlas opens, by setting the hash apt.js already routes on - no new
   * coupling to that file. */
  function catchRow(row) {
    var item = el('div', 'field-row');
    var head = el('div', 'field-row-head');

    var name = el('button', 'field-row-name', row.com || row.sci);
    name.type = 'button';
    name.addEventListener('click', function () {
      if (row.sci) location.hash = '#sci=' + encodeURIComponent(row.sci);
    });
    head.appendChild(name);
    head.appendChild(el('span', 'field-row-conf',
      row.conf == null ? '' : Math.round(row.conf * 100) + '%'));
    item.appendChild(head);

    var meta = [];
    if (row.place) meta.push(row.place);
    var when = timeText(row.at);
    if (when) meta.push(when);
    if (row.who) meta.push(row.who);
    item.appendChild(el('span', 'field-row-meta', meta.join('  ·  ')));

    if (row.audio) {
      var audio = document.createElement('audio');
      audio.controls = true;
      audio.preload = 'none';
      audio.src = API + '?action=audio&id=' + encodeURIComponent(row.id);
      item.appendChild(audio);
    }
    return item;
  }

  function renderStation() {
    var heard = (station && station.heard) || [];
    countEl.textContent = 'Home - '
      + (station.species != null ? station.species + ' species heard here' : 'the station');

    listEl.textContent = '';
    if (!heard.length) {
      listEl.appendChild(el('p', 'field-note',
        'The station has not written anything down yet.'));
      return;
    }
    listEl.appendChild(el('p', 'field-note',
      'Most recent first, from the station itself - not field recordings, '
      + 'so there is no clip to play here. The Collage and Stats views have '
      + 'the whole record.'));

    heard.forEach(function (row) {
      var item = el('div', 'field-row');
      var head = el('div', 'field-row-head');
      var name = el('button', 'field-row-name', row.com || row.sci);
      name.type = 'button';
      name.addEventListener('click', function () {
        if (row.sci) location.hash = '#sci=' + encodeURIComponent(row.sci);
      });
      head.appendChild(name);
      item.appendChild(head);
      var when = timeText(row.at);
      if (when) item.appendChild(el('span', 'field-row-meta', when));
      listEl.appendChild(item);
    });
  }

  function showProblem(error) {
    emptyEl.textContent = '';
    emptyEl.hidden = false;
    var locked = error && error.status === 401;
    emptyEl.appendChild(el('span', null, locked
      ? 'Unlock with the station admin password in the menu to see what has been caught.'
      : String((error && error.message) || error)));
    var again = el('button', 'field-link', locked ? 'i have unlocked' : 'try again');
    again.type = 'button';
    again.addEventListener('click', function () { refresh(true); });
    emptyEl.appendChild(again);
  }

  /* The map and the catches are fetched apart on purpose. The districts
   * are geography - they need no permission and no data - so a list that
   * comes back unauthorized should leave you looking at Singapore with
   * nothing shaded, not at an empty box. Loading both in one Promise.all
   * meant one 401 hid the whole map. */
  var loading = null;
  function refresh(force) {
    if (loading && !force) return loading;

    var drawn = loadShape().then(function (shape) {
      if (!mapSvg.querySelector('.field-land')) drawShape(shape);
    }).catch(function (e) {
      showProblem(e);
      throw e;
    });

    var listed = api('list').then(function (data) {
      catches = data.submissions || [];
      return data;
    });
    // Claim the rejection now. The real handling is below, but it does not
    // attach until the shape has drawn, and an unclaimed rejection in the
    // meantime surfaces as an uncaught error in the console.
    listed.catch(function () { /* handled below */ });

    loading = drawn.then(function () {
      return listed.then(function (data) {
        station = data.station || null;
        paintHeat(data.areas || [], data.station);
        emptyEl.hidden = true;
        renderList();
      });
    }).catch(function (e) {
      // The map is already on screen if the shape arrived; only the
      // catches are missing, so say so beside the list rather than
      // blanking everything.
      showProblem(e);
      renderList();
    }).then(function () { loading = null; });
    return loading;
  }

  // ------------------------------------------------------- the recorder
  function openSheet() {
    sheet.hidden = false;
    document.body.classList.add('field-recording-open');
    showIntro();
  }

  function closeSheet() {
    stopRecording(true);
    sheet.hidden = true;
    document.body.classList.remove('field-recording-open');
  }

  function setSheet(nodes) {
    sheetBody.textContent = '';
    (Array.isArray(nodes) ? nodes : [nodes]).forEach(function (n) { sheetBody.appendChild(n); });
  }

  function showIntro() {
    var wrap = el('div', 'field-step');
    wrap.appendChild(el('h3', 'field-step-title', 'Record a bird'));
    wrap.appendChild(el('p', 'field-lead',
      'Point your phone at the bird and hold still. The station listens to '
      + 'the clip with the same model it uses at home, and tells you what it '
      + 'heard.'));

    if (!canRecord()) {
      wrap.appendChild(el('p', 'field-note', window.isSecureContext
        ? 'This browser has no microphone recording.'
        : 'Recording needs a secure connection. Open the site over https to record here.'));
    } else {
      var btn = el('button', 'field-primary', 'start recording');
      btn.type = 'button';
      btn.addEventListener('click', startRecording);
      wrap.appendChild(btn);
    }
    setSheet(wrap);
  }

  function startRecording() {
    var constraints = { audio: {
      // Phone browsers assume speech and apply processing that is actively
      // wrong for birdsong. Ask for it off - Chrome obeys, Safari partly.
      echoCancellation: false, noiseSuppression: false, autoGainControl: false,
      channelCount: 1
    } };

    var wrap = el('div', 'field-step');
    wrap.appendChild(el('h3', 'field-step-title', 'Listening'));
    var count = el('p', 'field-count', MAX_SECONDS + 's');
    var meter = el('div', 'field-meter');
    var bar = el('div', 'field-meter-bar');
    meter.appendChild(bar);
    var stopBtn = el('button', 'field-primary field-stop', 'stop and send');
    stopBtn.type = 'button';
    stopBtn.addEventListener('click', function () { stopRecording(false); });
    wrap.appendChild(count);
    wrap.appendChild(meter);
    wrap.appendChild(stopBtn);
    setSheet(wrap);

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
        count.textContent = left > 0 ? left + 's' : 'finishing';
      }, 1000);
      stopTimer = setTimeout(function () { stopRecording(false); }, MAX_SECONDS * 1000);
    }).catch(function (e) {
      showError((e && e.name === 'NotAllowedError')
        ? 'Microphone access was declined.'
        : 'Could not open the microphone: ' + ((e && e.message) || e));
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

  function working(message) {
    var wrap = el('div', 'field-step');
    wrap.appendChild(el('div', 'field-spinner'));
    wrap.appendChild(el('p', 'field-note', message));
    setSheet(wrap);
  }

  function upload(mime) {
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    if (!chunks.length) return showError('nothing was recorded');

    working('sending');
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
      working('the station is listening to it');
      poll(j.id, 0);
    }).catch(function (e) { showError(String(e.message || e)); });
  }

  function poll(id, attempt) {
    if (attempt > POLL_LIMIT) {
      return showError('the station is taking too long. It may still finish - '
        + 'close this and look at the list in a minute.');
    }
    api('result&id=' + encodeURIComponent(id)).then(function (j) {
      if (j.status === 'pending' || j.status === 'analysing') {
        setTimeout(function () { poll(id, attempt + 1); }, POLL_MS);
        return;
      }
      if (j.status === 'confirmed') return showCaught(j);
      if (j.status === 'unsure' || j.status === 'rejected') return showUnsure();
      showError(j.error || 'the recording could not be analysed');
    }).catch(function (e) { showError(String(e.message || e)); });
  }

  function showCaught(result) {
    var wrap = el('div', 'field-step');
    wrap.appendChild(el('p', 'field-eyebrow', 'caught'
      + (result.place ? ' at ' + result.place : '')));
    wrap.appendChild(el('h3', 'field-caught', result.com || result.sci));
    if (result.sci && result.com && result.sci !== result.com) {
      wrap.appendChild(el('p', 'field-caught-sci', result.sci));
    }
    if (result.conf != null) {
      wrap.appendChild(el('p', 'field-note',
        Math.round(result.conf * 100) + '% sure'));
    }

    var read = el('button', 'field-primary', 'read about it');
    read.type = 'button';
    read.addEventListener('click', function () {
      closeSheet();
      if (result.sci) location.hash = '#sci=' + encodeURIComponent(result.sci);
    });
    wrap.appendChild(read);

    var again = el('button', 'field-secondary', 'record another');
    again.type = 'button';
    again.addEventListener('click', showIntro);
    wrap.appendChild(again);

    var wrong = el('button', 'field-link', 'that was not it');
    wrong.type = 'button';
    wrong.addEventListener('click', function () {
      api('reject', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: result.id })
      }).then(function () { refresh(true); showIntro(); })
        .catch(function (e) { showError(String(e.message || e)); });
    });
    wrap.appendChild(wrong);

    setSheet(wrap);
    refresh(true);
  }

  function showUnsure() {
    var wrap = el('div', 'field-step');
    wrap.appendChild(el('h3', 'field-step-title', 'Could not make that one out'));
    wrap.appendChild(el('p', 'field-lead',
      'Nothing in that clip scored high enough to put a name to. Get closer '
      + 'if you can, or wait for the bird to call again.'));
    var again = el('button', 'field-primary', 'try again');
    again.type = 'button';
    again.addEventListener('click', startRecording);
    wrap.appendChild(again);
    setSheet(wrap);
  }

  function showError(message) {
    var wrap = el('div', 'field-step');
    wrap.appendChild(el('h3', 'field-step-title', 'That did not work'));
    wrap.appendChild(el('p', 'field-note', message));
    var again = el('button', 'field-secondary', 'back');
    again.type = 'button';
    again.addEventListener('click', showIntro);
    wrap.appendChild(again);
    setSheet(wrap);
  }

  // --------------------------------------------------------------- mount
  function buildSheet() {
    sheet = el('div', 'field-sheet');
    sheet.id = 'fieldSheet';
    sheet.hidden = true;
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    sheet.setAttribute('aria-label', 'record a bird');

    var card = el('div', 'field-sheet-card');
    var close = el('button', 'field-sheet-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'close');
    close.addEventListener('click', closeSheet);
    sheetBody = el('div', 'field-sheet-body');
    card.appendChild(close);
    card.appendChild(sheetBody);
    sheet.appendChild(card);
    document.body.appendChild(sheet);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !sheet.hidden) closeSheet();
    });
  }

  function mount() {
    view = document.getElementById('v3');
    if (!view || view.dataset.fieldMounted === 'true') return;
    view.dataset.fieldMounted = 'true';

    var wrap = el('div', 'field-wrap');

    var record = el('button', 'field-cta', 'record a bird');
    record.type = 'button';
    record.addEventListener('click', openSheet);

    var head = el('div', 'field-headrow');
    head.appendChild(el('p', 'field-lead',
      'Where the birds have been heard: the station at home, and anything '
      + 'recorded out in the field. Tap a district to see just those.'));
    head.appendChild(record);
    wrap.appendChild(head);

    mapHost = el('div', 'field-map');
    mapSvg = svgEl('svg', 'field-map-svg');
    mapSvg.setAttribute('role', 'img');
    mapSvg.setAttribute('aria-label', 'Singapore, shaded by birds caught in each district');
    mapLabels = el('div', 'field-map-labels');
    mapHost.appendChild(mapSvg);
    mapHost.appendChild(mapLabels);
    wrap.appendChild(mapHost);

    legendEl = buildLegend();
    wrap.appendChild(legendEl);

    emptyEl = el('p', 'field-note field-problem');
    emptyEl.hidden = true;
    wrap.appendChild(emptyEl);

    countEl = el('p', 'field-count-line');
    wrap.appendChild(countEl);
    listEl = el('div', 'field-list');
    wrap.appendChild(listEl);

    wrap.appendChild(el('p', 'field-credit',
      'District boundaries from geoBoundaries, CC BY 4.0.'));

    view.appendChild(wrap);
    buildSheet();

    // Load when the view is first reached rather than on every page load.
    // The slider button is the only way in besides a refresh landing back
    // on the view somebody left, which apt.js records under this key.
    var tab = document.querySelector('#slider button[data-i="3"]');
    if (tab) tab.addEventListener('click', function () { refresh(); });
    var restored = null;
    try { restored = localStorage.getItem('bird:view'); } catch (e) { /* private mode */ }
    if (restored === '3') refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
