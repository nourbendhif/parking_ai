/**
 * ParkSense AI – Core JavaScript
 * Handles: sidebar, clock, modals, status polling
 */

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
  const sidebar  = document.getElementById('sidebar');
  const overlay  = document.getElementById('sidebarOverlay');
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

// Close modal on backdrop click
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-backdrop')) {
    e.target.classList.remove('open');
  }
});

// Close modal on Escape
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

    // PC status pill
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

    // Sim badge in topbar
    const simBadge = document.getElementById('simBadge');
    if (simBadge) {
      simBadge.style.display = data.simulation_mode ? 'flex' : 'none';
    }
  } catch (e) {
    // Silently fail
  }
}

// Poll every 15 seconds
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
  const start    = 0;
  const step     = (timestamp) => {
    if (!startTime) startTime = timestamp;
    const elapsed = timestamp - startTime;
    const progress = Math.min(elapsed / duration, 1);
    const eased    = 1 - Math.pow(1 - progress, 3); // ease-out-cubic
    el.textContent = Math.round(start + (target - start) * eased);
    if (progress < 1) requestAnimationFrame(step);
  };
  let startTime = null;
  requestAnimationFrame(step);
}

document.querySelectorAll('.stat-value').forEach(el => {
  const val = parseInt(el.textContent.trim(), 10);
  if (!isNaN(val) && val > 0) animateCounter(el, val);
});

// ── Tooltip (simple title attr) ───────────────────────────────────────────────
// Native title tooltips work fine for our purposes

// ── Theme toggle (future use) ─────────────────────────────────────────────────
// Currently locked to dark theme per design

console.log('%cParkSense AI v2.0', 'color:#00d4b4;font-family:monospace;font-size:16px;font-weight:bold');
console.log('%cSmart Parking Management System', 'color:#8890a8;font-family:monospace');
