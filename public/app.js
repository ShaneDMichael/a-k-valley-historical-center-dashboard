import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const qs = new URLSearchParams(window.location.search);
const MODEL_FILE = qs.get('model') || '';
if (MODEL_FILE) {
  const help = document.getElementById('marker-help');
  if (help) help.style.display = 'block';
}
const MODEL_URL = `/${MODEL_FILE}`;
const POLL_MS = 30000;

const VIEWER_TOKEN = 'akv_viewer_1';
const DEVICE_ID = qs.get('deviceId');

const MARKER_STORAGE_KEY = `switchbot_temp_marker_v1:${MODEL_FILE}:${DEVICE_ID || 'default'}`;
const API_DEVICE_QS = DEVICE_ID ? `?deviceId=${encodeURIComponent(DEVICE_ID)}` : '';
const API_QS = `${API_DEVICE_QS}`;

function apiQsForDeviceId(deviceId) {
  return deviceId ? `?deviceId=${encodeURIComponent(deviceId)}` : '';
}

function cToF(c) {
  return (c * 9) / 5 + 32;
}

const titleEl = document.getElementById('title');
if (titleEl) {
  const urlTitle = qs.get('title');
  const defaultTitle = (MODEL_FILE || 'A-K Valley Heritage Museum - Room Temperature and Humidity Dashboard').replace(/\.glb$/i, '').replace(/_/g, ' ');
  titleEl.textContent = urlTitle && urlTitle.trim().length ? urlTitle.trim() : defaultTitle;
}

const statusEl = document.getElementById('status');
const canvas = document.getElementById('c');

if (DEVICE_ID && !MODEL_FILE) {
  initSensorOnly();
} else if (!MODEL_FILE || !DEVICE_ID) {
  initRoomPicker();
} else {
  initViewer();
}

function initRoomPicker() {
  const pickerEl = document.getElementById('roomPicker');
  if (pickerEl) {
    pickerEl.hidden = false;
  }

  if (statusEl) {
    statusEl.textContent = 'Choose a room to view the floorplan and live temperature and humidity.';
  }

  //const helpEl = document.getElementById('help');
  //if (helpEl) {
  //  helpEl.textContent = 'If a room link shows “unauthorized”, include ?token=... in the URL.';
  //}

  const rooms = [
    { title: 'Basement', model: 'Basement.glb', deviceId: 'D8BFC2863232' },
    { title: 'Big room far side', model: 'Big_room_far_side.glb', deviceId: 'E87606063370' },
    { title: 'Big room near side', model: 'Big_room_near_side.glb', deviceId: 'E6558206264D' },
    { title: 'Entrance room', model: 'Entrance_room.glb', deviceId: 'D8BFC2867661' },
    { title: 'Upstairs office and storage', model: 'Upstairs_office_and_storage.glb', deviceId: 'E65584861767' },
    //{ title: 'Outside Temperature', deviceId: 'D88FC2464478' },
  ];

  if (!pickerEl) return;
  pickerEl.innerHTML = '';

  const pickerTitle = document.createElement('div');
  pickerTitle.className = 'pickerTitle';
  pickerTitle.textContent = 'Rooms';
  pickerEl.appendChild(pickerTitle);

  for (const room of rooms) {
    const link = new URL(window.location.href);
    if (room.model) {
      link.searchParams.set('model', room.model);
    } else {
      link.searchParams.delete('model');
    }
    link.searchParams.set('deviceId', room.deviceId);
    link.searchParams.set('title', room.title);
    //if (VIEWER_TOKEN) link.searchParams.set('token', VIEWER_TOKEN);

    const a = document.createElement('a');
    a.href = link.toString();

    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = room.title;
    a.appendChild(name);

    const live = document.createElement('div');
    live.className = 'live';
    live.textContent = '—';
    a.appendChild(live);

    a.dataset.deviceId = room.deviceId;
    a.dataset.title = room.title;

    pickerEl.appendChild(a);
  }

  startRoomPickerLiveTemps(pickerEl);
}

function startRoomPickerLiveTemps(pickerEl) {
  const items = Array.from(pickerEl.querySelectorAll('a[data-device-id]'));
  if (!items.length) return;

async function updateOnce() {
  for (const a of items) {
    const deviceId = a.dataset.deviceId;
    const liveEl = a.querySelector('.live');
    if (!deviceId || !liveEl) continue;

    try {
      const res = await fetch(`/api/temperature${apiQsForDeviceId(deviceId)}`, {cache: 'no-store',headers: {'x-viewer-token': VIEWER_TOKEN}});
      const data = await res.json();

      if (!res.ok) {
        liveEl.textContent = data && typeof data.error === 'string' ? data.error : 'error';
      } else {
        const tC = data && data.temperature;
        const h = data && data.humidity;
        const tF = typeof tC === 'number' ? cToF(tC) : null;
        const tempStr = typeof tF === 'number' ? `${tF.toFixed(1)}°F` : '—';
        const humStr = typeof h === 'number' ? `${h.toFixed(0)}%` : '—';
        liveEl.textContent = `${tempStr}  •  ${humStr}`;
      }
    } catch (e) {
      liveEl.textContent = 'offline';
    }

    // small delay to avoid burst
    await new Promise(r => setTimeout(r, 250));
  }
}

  updateOnce();
  setInterval(updateOnce, POLL_MS);
}

function initSensorOnly() {
  if (canvas) {
    canvas.style.display = 'none';
  }

  if (statusEl) {
    statusEl.textContent = 'Fetching temperature…';
  }

  async function pollTemperatureSensorOnly() {
    try {
      const res = await fetch(`/api/temperature${API_QS}`, {cache: 'no-store',headers: {'x-viewer-token': VIEWER_TOKEN}});
      const data = await res.json();

      if (!res.ok) {
        statusEl.textContent = `API error: ${data && typeof data.error === 'string' ? data.error : JSON.stringify(data && data.error)}`;
        return;
      }

      const tC = data && data.temperature;
      const h = data && data.humidity;

      const tF = typeof tC === 'number' ? cToF(tC) : null;
      const tempStr = typeof tF === 'number' ? `${tF.toFixed(1)}°F` : `${tC ?? '—'}°F`;
      const humStr = typeof h === 'number' ? `${h.toFixed(0)}%` : `${h ?? '—'}%`;

      statusEl.textContent = `Temperature: ${tempStr}   Humidity: ${humStr}   Updated: ${new Date(data.fetchedAt).toLocaleTimeString()}`;
    } catch (e) {
      statusEl.textContent = `Network error: ${(e && e.message) || e}`;
    }
  }

  setInterval(pollTemperatureSensorOnly, POLL_MS);
  pollTemperatureSensorOnly();
}

function initViewer() {
  if (!titleEl) return;

  const urlTitle = qs.get('title');
  const defaultTitle = (MODEL_FILE || 'A-K Valley Heritage Museum - Room Temperature and Humidity Dashboard').replace(/\.glb$/i, '').replace(/_/g, ' ');
  titleEl.textContent = urlTitle && urlTitle.trim().length ? urlTitle.trim() : defaultTitle;

  // Existing viewer code continues below
}

if (MODEL_FILE && DEVICE_ID) {
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
scene.background = new THREE.Color('#0b1020');

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 1000);
camera.position.set(2.2, 1.6, 2.2);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.target.set(0, 0.8, 0);

const hemi = new THREE.HemisphereLight(0xffffff, 0x223366, 1.0);
scene.add(hemi);

const dir = new THREE.DirectionalLight(0xffffff, 1.0);
dir.position.set(2, 4, 2);
scene.add(dir);

const tempBadge = makeTempBadge('—');
tempBadge.position.set(0, 1.5, 0);
scene.add(tempBadge);

let modelRoot = null;
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();

restoreMarkerPosition();

const loader = new GLTFLoader();
loader.load(
  MODEL_URL,
  (gltf) => {
    modelRoot = gltf.scene;
    scene.add(modelRoot);

    fitCameraToObject(camera, controls, modelRoot, 1.25);
    statusEl.textContent = 'Model loaded. Fetching temperature…';
  },
  undefined,
  (err) => {
    statusEl.textContent = 'Failed to load 3D model. Export your floorplan as GLB and place it at public/model.glb.';
    console.error(err);
  }
);

async function pollTemperature() {
  try {
    const res = await fetch(`/api/temperature${API_QS}`, {cache: 'no-store',headers: {'x-viewer-token': VIEWER_TOKEN}});
    const data = await res.json();

    if (!res.ok) {
      statusEl.textContent = `API error: ${data && typeof data.error === 'string' ? data.error : JSON.stringify(data && data.error)}`;
      setTempText(tempBadge, '—');
      return;
    }

    const tC = data && data.temperature;
    const h = data && data.humidity;

    const tF = typeof tC === 'number' ? cToF(tC) : null;
    const tempStr = typeof tF === 'number' ? `${tF.toFixed(1)}°F` : `${tC ?? '—'}°F`;
    const humStr = typeof h === 'number' ? `${h.toFixed(0)}%` : `${h ?? '—'}%`;

    statusEl.textContent = `Temperature: ${tempStr}   Humidity: ${humStr}   Updated: ${new Date(data.fetchedAt).toLocaleTimeString()}`;

    setTempText(tempBadge, tempStr);
    colorizeBadge(tempBadge, tF);
  } catch (e) {
    statusEl.textContent = `Network error: ${(e && e.message) || e}`;
    setTempText(tempBadge, '—');
  }
}

function cToF(c) {
  return (c * 9) / 5 + 32;
}

setInterval(pollTemperature, POLL_MS);
pollTemperature();

// ===============================
// Marker interaction: place + drag
// ===============================

let pointerDown = null;
let pointerMoved = false;
let draggingMarker = false;

const MARKER_HIT_RADIUS_PX = 28;

function markerHitTest(e) {
  if (!tempBadge) return false;

  const rect = canvas.getBoundingClientRect();

  const markerScreen = tempBadge.position.clone().project(camera);
  const sx = ((markerScreen.x + 1) / 2) * rect.width;
  const sy = ((-markerScreen.y + 1) / 2) * rect.height;

  const dx = e.clientX - sx;
  const dy = e.clientY - sy;

  return Math.hypot(dx, dy) <= MARKER_HIT_RADIUS_PX;
}

function moveMarkerToSurface(e) {
  if (!modelRoot) return false;

  const hit = pickModelPoint(e);
  if (!hit) return false;

tempBadge.position.copy(hit.point);

// Push marker outward from the hit surface
if (hit.face && hit.face.normal) {
  const normal = hit.face.normal.clone();
  normal.transformDirection(hit.object.matrixWorld);
  tempBadge.position.addScaledVector(normal, 0.12);
} else {
  tempBadge.position.y += 0.12;
}

  saveMarkerPosition();
  return true;
}

canvas.addEventListener('pointerdown', (e) => {
  pointerDown = {
    x: e.clientX,
    y: e.clientY,
    t: Date.now(),
    pointerType: e.pointerType
  };
  pointerMoved = false;

  if (markerHitTest(e)) {
    draggingMarker = true;
    if (typeof controls !== 'undefined' && controls) controls.enabled = false;
  }
});

canvas.addEventListener('pointermove', (e) => {
  if (!pointerDown) return;

  const dx = e.clientX - pointerDown.x;
  const dy = e.clientY - pointerDown.y;

  if (Math.hypot(dx, dy) > 8) pointerMoved = true;

  if (draggingMarker) {
    moveMarkerToSurface(e);
  }
});

canvas.addEventListener('pointerup', (e) => {
  if (!pointerDown) return;

  if (draggingMarker) {
    draggingMarker = false;
    if (typeof controls !== 'undefined' && controls) controls.enabled = true;
    pointerDown = null;
    return;
  }

  const dx = e.clientX - pointerDown.x;
  const dy = e.clientY - pointerDown.y;
  const dist = Math.hypot(dx, dy);
  const dt = Date.now() - pointerDown.t;

  const isTouch = pointerDown.pointerType === 'touch';

  if (isTouch && !pointerMoved && dist < 10 && dt < 350) {
    moveMarkerToSurface(e);
  }

  pointerDown = null;
});

canvas.addEventListener('dblclick', (e) => {
  moveMarkerToSurface(e);
});

function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize);
resize();

function animate() {
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
animate();

function makeTempBadge(text) {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 256;
  const ctx = canvas.getContext('2d');

  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;

  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: true });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(0.9, 0.45, 1);

  sprite.userData._badgeCanvas = canvas;
  sprite.userData._badgeCtx = ctx;
  sprite.userData._badgeTex = tex;

  drawBadge(sprite, text, '#2b67ff');
  return sprite;
}

function setTempText(sprite, text) {
  drawBadge(sprite, text, sprite.userData._badgeColor || '#2b67ff');
}

function colorizeBadge(sprite, tempValue) {
  if (typeof tempValue !== 'number') {
    sprite.userData._badgeColor = '#2b67ff';
    return;
  }

  // simple blue->red ramp (60F to 85F)
  const t = Math.min(1, Math.max(0, (tempValue - 60) / (85 - 60)));
  const color = lerpColor('#2b67ff', '#ff3b3b', t);
  sprite.userData._badgeColor = color;
  drawBadge(sprite, `${tempValue.toFixed(1)}°F`, color);
}

function drawBadge(sprite, text, accent) {
  const canvas = sprite.userData._badgeCanvas;
  const ctx = sprite.userData._badgeCtx;
  const tex = sprite.userData._badgeTex;

  ctx.clearRect(0, 0, canvas.width, canvas.height);

  roundRect(ctx, 24, 44, canvas.width - 48, canvas.height - 88, 36);
  ctx.fillStyle = 'rgba(10, 14, 28, 0.82)';
  ctx.fill();

  ctx.strokeStyle = 'rgba(255, 255, 255, 0.14)';
  ctx.lineWidth = 4;
  ctx.stroke();

  // accent bar
  roundRect(ctx, 44, 66, 14, canvas.height - 132, 10);
  ctx.fillStyle = accent;
  ctx.fill();

  ctx.font = '700 86px ui-sans-serif, system-ui, -apple-system';
  ctx.fillStyle = '#eaf0ff';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(String(text), canvas.width / 2 + 8, canvas.height / 2 + 2);

  ctx.font = '600 24px ui-sans-serif, system-ui, -apple-system';
  ctx.fillStyle = 'rgba(232, 238, 255, 0.78)';
  ctx.fillText('Temperature', canvas.width / 2 + 8, canvas.height / 2 + 62);

  tex.needsUpdate = true;
}

function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function fitCameraToObject(camera, controls, object, fitOffset = 1.2) {
  const box = new THREE.Box3().setFromObject(object);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());

  const maxSize = Math.max(size.x, size.y, size.z);
  const fov = (camera.fov * Math.PI) / 180;
  const cameraZ = Math.abs((maxSize / 2) / Math.tan(fov / 2)) * fitOffset;

  camera.position.set(center.x + cameraZ, center.y + cameraZ * 0.6, center.z + cameraZ);
  camera.near = maxSize / 100;
  camera.far = maxSize * 100;
  camera.updateProjectionMatrix();

  controls.target.copy(center);
  controls.update();
}

function lerpColor(a, b, t) {
  const ca = hexToRgb(a);
  const cb = hexToRgb(b);
  const r = Math.round(ca.r + (cb.r - ca.r) * t);
  const g = Math.round(ca.g + (cb.g - ca.g) * t);
  const bl = Math.round(ca.b + (cb.b - ca.b) * t);
  return rgbToHex(r, g, bl);
}

function pickModelPoint(event) {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -(((event.clientY - rect.top) / rect.height) * 2 - 1);

  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObject(modelRoot, true);
  return (hits && hits[0]) || null;
}

function saveMarkerPosition() {
  const p = tempBadge.position;
  const payload = { x: p.x, y: p.y, z: p.z };
  try {
    localStorage.setItem(MARKER_STORAGE_KEY, JSON.stringify(payload));
  } catch (e) {
    // ignore
  }
}

function restoreMarkerPosition() {
  try {
    const raw = localStorage.getItem(MARKER_STORAGE_KEY);
    if (!raw) return;
    const p = JSON.parse(raw);
    if (!p || typeof p.x !== 'number' || typeof p.y !== 'number' || typeof p.z !== 'number') return;
    tempBadge.position.set(p.x, p.y, p.z);
  } catch (e) {
    // ignore
  }
}

function hexToRgb(hex) {
  const h = hex.replace('#', '').trim();
  const v = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
  return { r: (v >> 16) & 255, g: (v >> 8) & 255, b: v & 255 };
}

function rgbToHex(r, g, b) {
  return `#${[r, g, b].map((x) => x.toString(16).padStart(2, '0')).join('')}`;
}


}
