import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const stage = document.getElementById('stage');
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const systemDark = matchMedia('(prefers-color-scheme: dark)');

// ---------------------------------------------------------------- theme (@dgit/use-theme)

const THEME_KEY = 'dgit-theme';
const themeListeners = [];
const isDark = () => {
  const t = document.documentElement.dataset.theme;
  return t ? t === 'dark' : systemDark.matches;
};
function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === 'system') delete root.dataset.theme; else root.dataset.theme = choice;
  try {
    if (choice === 'system') localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, choice);
  } catch {}
  document.querySelectorAll('[data-theme-choice]').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.themeChoice === choice)));
  themeListeners.forEach((fn) => fn());
}
let stored = 'system';
try { stored = localStorage.getItem(THEME_KEY) || 'system'; } catch {}
applyTheme(stored === 'light' || stored === 'dark' ? stored : 'system');
document.querySelectorAll('[data-theme-choice]').forEach((b) => b.addEventListener('click', () => applyTheme(b.dataset.themeChoice)));
systemDark.addEventListener('change', () => themeListeners.forEach((fn) => fn()));

// ---------------------------------------------------------------- copy button (@dgit/copy)

document.querySelectorAll('[data-copy]').forEach((button) => {
  button.addEventListener('click', () => {
    const text = button.parentElement.querySelector('pre').textContent;
    navigator.clipboard.writeText(text).then(() => {
      button.querySelector('use').setAttribute('href', '#i-check');
      button.setAttribute('aria-label', 'Copied');
      setTimeout(() => {
        button.querySelector('use').setAttribute('href', '#i-copy');
        button.setAttribute('aria-label', 'Copy');
      }, 1500);
    });
  });
});

// ---------------------------------------------------------------- the loop
// Attached, then the network drops (the ring goes gray and slips), velcro reconnects,
// and it snaps back. Told by the 3D mark alone.

const STATES = { attached: 4.6, dropped: 1.8, retry: 1.3 };
const ORDER = ['attached', 'dropped', 'retry'];
let state = 'attached';
let stateAt = 0;
const listeners = [];

function advance(now) {
  if (reduceMotion || now - stateAt <= STATES[state]) return;
  state = ORDER[(ORDER.indexOf(state) + 1) % ORDER.length];
  stateAt = now;
  listeners.forEach((fn) => fn(state));
}

// ---------------------------------------------------------------- 3D

let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
} catch {
  stage.classList.add('no-webgl');
}

if (renderer) {
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 0.95;
  stage.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.55;

  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100);
  camera.position.set(0, 0, 13);

  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(4, 6, 5);
  const rim = new THREE.PointLight(0x34d399, 40, 20);
  rim.position.set(-3, 2, -4);
  scene.add(key, rim);

  const emerald = new THREE.Color('#1fbf85');
  const slate = new THREE.Color('#4f5d57');
  const glow = new THREE.Color('#0b5c40');

  const material = () => new THREE.MeshPhysicalMaterial({
    color: emerald.clone(), metalness: 0.05, roughness: 0.16,
    clearcoat: 1, clearcoatRoughness: 0.06,
    iridescence: 0.3, iridescenceIOR: 1.3,
    emissive: glow.clone(), emissiveIntensity: 0.5,
  });
  const hookMat = material();
  const ringMat = material();

  // The logo in its own units (the icon is 64 across), y up. The hook is a tube
  // along the mark's path; the ring hangs through its U like a chain link.
  const R = 3; // stroke width 6
  class Arc extends THREE.Curve {
    getPoint(t, target = new THREE.Vector3()) {
      const a = -Math.PI * t;
      return target.set(8 * Math.cos(a), 3 + 8 * Math.sin(a), 0);
    }
  }
  const path = new THREE.CurvePath();
  path.add(new THREE.LineCurve3(new THREE.Vector3(8, 25, 0), new THREE.Vector3(8, 3, 0)));
  path.add(new Arc());
  path.add(new THREE.LineCurve3(new THREE.Vector3(-8, 3, 0), new THREE.Vector3(-8, 8, 0)));

  const hook = new THREE.Group();
  hook.add(new THREE.Mesh(new THREE.TubeGeometry(path, 260, R, 48, false), hookMat));
  for (const [x, y] of [[8, 25], [-8, 8]]) {
    const cap = new THREE.Mesh(new THREE.SphereGeometry(R, 48, 24), hookMat);
    cap.position.set(x, y, 0);
    hook.add(cap);
  }

  // The ring rests on the bottom of the U (y = -5 + R + R), so it swings from there.
  const pivot = new THREE.Group();
  pivot.position.set(0, 1 - R, 0);
  const ring = new THREE.Mesh(new THREE.TorusGeometry(12, R, 64, 220), ringMat);
  ring.rotation.y = Math.PI / 2;
  ring.position.set(0, -12 + R, 0);
  pivot.add(ring);

  const logo = new THREE.Group();
  logo.add(hook, pivot);
  logo.scale.setScalar(0.1);
  logo.position.y = -0.05;
  const turntable = new THREE.Group();
  turntable.add(logo);
  scene.add(turntable);

  // Springs: the ring's swing and slack, and how "on" everything looks.
  const swing = { x: 0, v: 0 };
  const slack = { x: 0, v: 0, to: 0 };
  const power = { x: 1, v: 0, to: 1 };
  let flash = 0;

  listeners.push((next) => {
    if (next === 'dropped') { slack.to = -1.8; power.to = 0; swing.v += 2.4; }
    if (next === 'retry') { swing.v -= 1.2; }
    if (next === 'attached') { slack.to = 0; power.to = 1; flash = 1; swing.v -= 1.6; }
  });

  const spring = (s, to, k, c, dt) => { s.v += (-(s.x - to) * k - s.v * c) * dt; s.x += s.v * dt; };

  // Pointer: tilt toward the cursor, drag to spin with inertia.
  const pointer = { x: 0, y: 0 };
  let yaw = 0, yawV = 0, dragging = false, lastX = 0;
  addEventListener('pointermove', (e) => {
    pointer.x = (e.clientX / innerWidth) * 2 - 1;
    pointer.y = (e.clientY / innerHeight) * 2 - 1;
    if (dragging) { yawV = (e.clientX - lastX) * 0.012; yaw += yawV; lastX = e.clientX; }
  });
  renderer.domElement.addEventListener('pointerdown', (e) => {
    dragging = true; lastX = e.clientX; swing.v += (Math.random() - 0.5) * 2;
  });
  addEventListener('pointerup', () => { dragging = false; });

  const resize = () => {
    const { clientWidth: w, clientHeight: h } = stage;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // Keep the whole mark in frame on narrow screens.
    camera.position.z = w / h < 0.8 ? 13 / Math.max(w / h / 0.8, 0.62) : 13;
    camera.updateProjectionMatrix();
  };
  new ResizeObserver(resize).observe(stage);
  resize();

  let visible = true;
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; }).observe(stage);

  // White page: less glow, a bit more light; black page: the other way round.
  const onTheme = () => {
    const dark = isDark();
    renderer.toneMappingExposure = dark ? 0.95 : 1.05;
    scene.environmentIntensity = dark ? 0.55 : 0.8;
    glowScale = dark ? 1 : 0.35;
  };
  let glowScale = 1;
  themeListeners.push(onTheme);
  onTheme();

  const clock = new THREE.Clock();
  let t = 0;
  renderer.setAnimationLoop(() => {
    const dt = Math.min(clock.getDelta(), 1 / 30);
    if (!visible || document.hidden) return;
    t += dt;
    advance(t);

    spring(swing, 0, 14, 1.1, dt);
    spring(slack, slack.to, 30, 7, dt);
    spring(power, power.to, 10, 5, dt);
    flash = Math.max(0, flash - dt * 1.6);
    if (state === 'retry' && !reduceMotion) swing.x += Math.sin(t * 38) * 0.004;

    pivot.rotation.z = swing.x * 0.22;
    pivot.rotation.x = swing.x * 0.08;
    pivot.position.y = 1 - R + slack.x;

    const p = THREE.MathUtils.clamp(power.x, 0, 1);
    ringMat.color.copy(slate).lerp(emerald, p);
    hookMat.color.copy(slate).lerp(emerald, 0.55 + 0.45 * p);
    ringMat.emissiveIntensity = (0.12 + 0.38 * p + flash * 2.2) * glowScale;
    hookMat.emissiveIntensity = (0.25 + 0.25 * p + flash * 1.4) * glowScale;
    rim.intensity = (12 + 28 * p + flash * 60) * glowScale;

    if (!dragging) { yaw += yawV; yawV *= 0.94; }
    const drift = reduceMotion ? 0 : Math.sin(t * 0.35) * 0.35;
    turntable.rotation.y += ((-0.62 + drift + yaw + pointer.x * 0.35) - turntable.rotation.y) * 0.06;
    turntable.rotation.x += ((0.08 + pointer.y * 0.18) - turntable.rotation.x) * 0.06;
    turntable.position.y = reduceMotion ? 0 : Math.sin(t * 0.9) * 0.08;

    renderer.render(scene, camera);
  });
}
