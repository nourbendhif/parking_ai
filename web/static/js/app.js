/**
 * ParkSense AI – Core JavaScript
 * Theme toggle, sidebar, clock, modals, status polling, auto-detect, GPIO
 */

// ── Theme Toggle ──────────────────────────────────────────────────────────────
(function initTheme() {
  const saved = localStorage.getItem('ps-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  updateThemeIcon(saved);
})();

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  const next    = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('ps-theme', next);
  updateThemeIcon(next);
}

function updateThemeIcon(theme) {
  const btn = document.getElementById('themeToggleBtn');
  if (btn) btn.textContent = theme === 'dark' ? '☀️' : '🌙';
}

// ── Clock ─────────────────────────────────────────────────────────────────────
function updateClock() {
  const el = document.getElementById('topbarTime');
  if (!el) return;
  const now = new Date();
  el.textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
setInterval(updateClock, 1000);
updateClock();

// ── Sidebar toggle (mobile) ───────────────────────────────────────────────────
function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebarOverlay');
  if (!sidebar) return;
  sidebar.classList.toggle('open');
  overlay.classList.toggle('open');
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('open');
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('open');
}

document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-backdrop')) {
    e.target.classList.remove('open');
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open'));
  }
});

// ── Flash auto-dismiss ────────────────────────────────────────────────────────
document.querySelectorAll('.flash').forEach(el => {
  setTimeout(() => {
    el.style.transition = 'opacity .4s ease';
    el.style.opacity    = '0';
    setTimeout(() => el.remove(), 400);
  }, 5000);
});

// ── Status polling (topbar PC indicator) ─────────────────────────────────────
async function pollTopbarStatus() {
  try {
    const res  = await fetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();

    const pill = document.getElementById('pcStatus');
    if (pill) {
      const dot = pill.querySelector('.dot');
      if (dot) {
        dot.className = `dot dot-${data.pc_online ? 'green' : data.simulation_mode ? 'yellow' : 'red'}`;
      }
      const text = pill.childNodes[pill.childNodes.length - 1];
      if (text && text.nodeType === 3) {
        text.textContent = data.simulation_mode ? ' SIM' : data.pc_online ? ' PC' : ' OFFLINE';
      }
    }

    const simBadge = document.getElementById('simBadge');
    if (simBadge) simBadge.style.display = data.simulation_mode ? 'flex' : 'none';

    // Update auto-detect indicator
    const adBadge = document.getElementById('autoDetectBadge');
    if (adBadge) {
      adBadge.style.display = data.auto_detect ? 'flex' : 'none';
    }
  } catch (e) {}
}

setInterval(pollTopbarStatus, 15000);
pollTopbarStatus();

// ── Plate input auto-uppercase ────────────────────────────────────────────────
document.querySelectorAll('input[name="license_plate"]').forEach(input => {
  input.addEventListener('input', () => {
    const pos = input.selectionStart;
    input.value = input.value.toUpperCase();
    input.setSelectionRange(pos, pos);
  });
});

// ── Confirm dangerous actions ─────────────────────────────────────────────────
document.querySelectorAll('[data-confirm]').forEach(el => {
  el.addEventListener('click', (e) => {
    if (!confirm(el.dataset.confirm)) e.preventDefault();
  });
});

// ── Number formatting ─────────────────────────────────────────────────────────
function formatNumber(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return n.toString();
}

// ── Animate stat counters on load ─────────────────────────────────────────────
function animateCounter(el, target, duration = 800) {
  const step = (timestamp) => {
    if (!startTime) startTime = timestamp;
    const elapsed  = timestamp - startTime;
    const progress = Math.min(elapsed / duration, 1);
    const eased    = 1 - Math.pow(1 - progress, 3);
    el.textContent = Math.round(target * eased);
    if (progress < 1) requestAnimationFrame(step);
  };
  let startTime = null;
  requestAnimationFrame(step);
}

document.querySelectorAll('.stat-value').forEach(el => {
  const val = parseInt(el.textContent.trim(), 10);
  if (!isNaN(val) && val > 0) animateCounter(el, val);
});

// ── Earnings visibility toggle ────────────────────────────────────────────────
let earningsVisible = localStorage.getItem('ps-earnings-visible') !== 'false';

function toggleEarnings() {
  earningsVisible = !earningsVisible;
  localStorage.setItem('ps-earnings-visible', earningsVisible);
  _applyEarningsVisibility();
}

function _applyEarningsVisibility() {
  const content = document.getElementById('earningsContent');
  const btn     = document.getElementById('earningsToggleBtn');
  if (!content) return;
  content.classList.toggle('earnings-hidden', !earningsVisible);
  if (btn) {
    btn.innerHTML = earningsVisible
      ? '<i class="fa-solid fa-eye-slash"></i> Hide'
      : '<i class="fa-solid fa-eye"></i> Show';
  }
}

document.addEventListener('DOMContentLoaded', _applyEarningsVisibility);

// ── GPIO physical button (RPi) ────────────────────────────────────────────────
// This endpoint is called by RPi GPIO button handler (Python side can POST to /api/gpio/capture)
// From the browser, we also expose a keyboard shortcut (Space) when on dashboard
document.addEventListener('keydown', (e) => {
  if (e.code === 'Space' && document.getElementById('detectBtn') &&
      document.activeElement.tagName !== 'INPUT' &&
      document.activeElement.tagName !== 'TEXTAREA') {
    e.preventDefault();
    if (typeof triggerDetection === 'function') triggerDetection();
  }
});

console.log('%cParkSense AI v2.1', 'color:#00d4b4;font-family:monospace;font-size:16px;font-weight:bold');
console.log('%cSmart Parking Management System', 'color:#8890a8;font-family:monospace');
console.log('%cPress [Space] on dashboard to trigger detection', 'color:#4e5568;font-family:monospace;font-size:11px');
