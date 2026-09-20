// ---------------------------------------------------------------------
// GENDER AUTO-DETECTION (student_form.html)
// ---------------------------------------------------------------------
function initGenderDetect() {
    const nameInput = document.getElementById("full_name");
    const genderSelect = document.getElementById("gender");
    const confirmCheckbox = document.getElementById("gender_confirmed");
    const hint = document.getElementById("gender-hint");
    if (!nameInput || !genderSelect) return;

    let debounceTimer;
    nameInput.addEventListener("input", function () {
        clearTimeout(debounceTimer);
        // Any manual name edit means previous confirmation no longer applies
        if (confirmCheckbox) confirmCheckbox.checked = false;
        debounceTimer = setTimeout(async () => {
            const name = nameInput.value.trim();
            if (!name) {
                if (hint) hint.textContent = "";
                return;
            }
            try {
                const res = await fetch(`/api/detect-gender?name=${encodeURIComponent(name)}`);
                const data = await res.json();
                if (data.gender) {
                    genderSelect.value = data.gender;
                    if (hint) {
                        hint.textContent = `Detected gender: ${data.gender} (please confirm or change if incorrect)`;
                        hint.className = "form-text text-success";
                    }
                } else {
                    if (hint) {
                        hint.textContent = "Could not automatically detect gender from this name. Please select and confirm it manually.";
                        hint.className = "form-text text-warning";
                    }
                }
            } catch (e) {
                console.error("Gender detect failed", e);
            }
        }, 450);
    });

    // Manually changing the dropdown counts as a confirmation
    genderSelect.addEventListener("change", function () {
        if (confirmCheckbox) confirmCheckbox.checked = true;
    });
}

// ---------------------------------------------------------------------
// LIVE TOTAL / AVERAGE / GRADE PREVIEW (marks.html)
// ---------------------------------------------------------------------
// Ordered highest-to-lowest, using ">= low" only (no upper bound) so every
// decimal average (e.g. 20.5, 40.9, 60.1, 80.99) maps to exactly one grade
// with no gaps - matches the same logic used server-side in config.py.
const GRADE_BANDS = [
    { letter: "A", low: 81, cls: "grade-A" },
    { letter: "B", low: 61, cls: "grade-B" },
    { letter: "C", low: 41, cls: "grade-C" },
    { letter: "D", low: 21, cls: "grade-D" },
    { letter: "E", low: 0, cls: "grade-E" },
];

function gradeFor(avg) {
    for (const b of GRADE_BANDS) {
        if (avg >= b.low) return b;
    }
    return { letter: "-", cls: "grade--" };
}

function recalcRow(studentId) {
    const inputs = document.querySelectorAll(`.score-input[data-student="${studentId}"]`);
    let total = 0, count = 0;
    inputs.forEach((inp) => {
        const v = parseFloat(inp.value);
        if (!isNaN(v)) { total += v; count += 1; }
    });
    const avg = count ? total / count : null;
    const totalCell = document.getElementById(`total-${studentId}`);
    const avgCell = document.getElementById(`avg-${studentId}`);
    const gradeCell = document.getElementById(`grade-${studentId}`);
    if (totalCell) totalCell.textContent = count ? total.toFixed(0) : "-";
    if (avgCell) avgCell.textContent = avg !== null ? avg.toFixed(1) : "-";
    if (gradeCell) {
        const g = avg !== null ? gradeFor(avg) : { letter: "-", cls: "grade--" };
        gradeCell.innerHTML = `<span class="grade-badge ${g.cls}">${g.letter}</span>`;
    }
}

function initMarksEntry() {
    const inputs = document.querySelectorAll(".score-input");
    if (!inputs.length) return;
    inputs.forEach((inp) => {
        inp.addEventListener("input", () => recalcRow(inp.dataset.student));
        recalcRow(inp.dataset.student);
    });
}

document.addEventListener("DOMContentLoaded", function () {
    initGenderDetect();
    initMarksEntry();
    idleTimeoutInit();

    // show/hide password toggle (eye icon) - login form and anywhere else
    // that pairs an <input type="password"> with a .password-toggle-btn
    document.querySelectorAll(".password-toggle-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            const input = document.getElementById(btn.dataset.target);
            if (!input) return;
            const nowShowing = input.type === "password";
            input.type = nowShowing ? "text" : "password";
            const icon = btn.querySelector("i");
            icon.classList.toggle("bi-eye", !nowShowing);
            icon.classList.toggle("bi-eye-slash", nowShowing);
            btn.setAttribute("aria-label", nowShowing ? "Hide password" : "Show password");
        });
    });

    // Show a "Signing in..." loading state on submit buttons that opt in via
    // [data-loading-text], so someone on a slow connection can see their
    // click registered instead of wondering whether to click again.
    document.querySelectorAll("form").forEach((form) => {
        const submitBtn = form.querySelector("button[data-loading-text]");
        if (!submitBtn) return;
        form.addEventListener("submit", () => {
            submitBtn.disabled = true;
            submitBtn.innerHTML =
                '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>' +
                submitBtn.dataset.loadingText;
        });
    });

    // sidebar toggle for small screens (slide-in drawer)
    const toggleBtn = document.getElementById("sidebarToggle");
    const sidebarEl = document.querySelector(".sidebar");
    const backdropEl = document.getElementById("sidebarBackdrop");
    function closeSidebar() {
        sidebarEl?.classList.remove("show");
        backdropEl?.classList.remove("show");
    }
    if (toggleBtn && sidebarEl) {
        toggleBtn.addEventListener("click", () => {
            sidebarEl.classList.toggle("show");
            backdropEl?.classList.toggle("show");
        });
    }
    backdropEl?.addEventListener("click", closeSidebar);
    // close the drawer automatically when a nav link is tapped
    document.querySelectorAll(".sidebar a.nav-link").forEach((link) => {
        link.addEventListener("click", closeSidebar);
    });

    // auto-dismiss toast notifications (slide out, then remove)
    document.querySelectorAll(".app-toast").forEach((el) => {
        setTimeout(() => {
            el.classList.add("toast-leaving");
            setTimeout(() => el.remove(), 250);
        }, 4000);
    });

    // dark / light mode toggle
    const themeBtn = document.getElementById("themeToggle");
    function refreshThemeIcon() {
        const isDark = document.documentElement.getAttribute("data-theme") === "dark";
        if (themeBtn) themeBtn.innerHTML = isDark ? '<i class="bi bi-sun"></i>' : '<i class="bi bi-moon-stars"></i>';
    }
    refreshThemeIcon();
    themeBtn?.addEventListener("click", () => {
        const isDark = document.documentElement.getAttribute("data-theme") === "dark";
        if (isDark) {
            document.documentElement.removeAttribute("data-theme");
            try { localStorage.setItem("kps-theme", "light"); } catch (e) {}
        } else {
            document.documentElement.setAttribute("data-theme", "dark");
            try { localStorage.setItem("kps-theme", "dark"); } catch (e) {}
        }
        refreshThemeIcon();
    });
});

function printReport() {
    window.print();
}

// ---------------------------------------------------------------------
// STYLED CONFIRMATION MODAL (replaces the native browser confirm() popup)
// ---------------------------------------------------------------------
// Usage on a <form onsubmit="return appConfirm(this, 'Delete this?');">
// or on a submit <button onclick="return appConfirm(this.form, 'Delete this?');">
// Optional 3rd argument for less severe actions, e.g.:
//   appConfirm(this, 'Restore this record?', {okClass: 'btn-primary', okText: 'Restore', icon: 'bi-arrow-counterclockwise'})
function appConfirm(formEl, message, opts) {
    opts = opts || {};
    const modalEl = document.getElementById("appConfirmModal");
    if (!modalEl || !window.bootstrap) {
        // Fallback (bootstrap JS not loaded for some reason) - old behaviour.
        return window.confirm(message);
    }
    document.getElementById("appConfirmBody").textContent = message;
    const titleEl = document.getElementById("appConfirmTitle");
    titleEl.querySelector("i").className = "bi " + (opts.icon || "bi-exclamation-triangle-fill");
    titleEl.querySelector("span").textContent = opts.title || "Confirm";

    const okBtn = document.getElementById("appConfirmOkBtn");
    okBtn.className = "btn " + (opts.okClass || "btn-danger");
    okBtn.textContent = opts.okText || "Yes, continue";

    // Replace the button so we never stack multiple click handlers from
    // previous confirmations on the same page.
    const freshOkBtn = okBtn.cloneNode(true);
    okBtn.parentNode.replaceChild(freshOkBtn, okBtn);
    freshOkBtn.addEventListener("click", function () {
        bootstrap.Modal.getOrCreateInstance(modalEl).hide();
        formEl.submit();
    });

    bootstrap.Modal.getOrCreateInstance(modalEl).show();
    return false; // stop the native submit/click - the modal takes over
}

// ---------------------------------------------------------------------
// IDLE-TIMEOUT AUTO-LOGOUT (shared office computer, multiple staff)
// ---------------------------------------------------------------------
// After IDLE_TIMEOUT_MINUTES (data-idle-timeout-minutes on #appShell,
// from config.py) of no mouse/keyboard/touch activity, the person is
// warned on-screen for the last 20 seconds, then sent to /logout
// automatically - they don't need to click anything for it to happen.
// While they ARE genuinely active (typing a long form, for example) a
// throttled ping to /api/keep-alive keeps the SERVER-SIDE session from
// separately expiring out from under them mid-entry.
function idleTimeoutInit() {
    const shell = document.getElementById("appShell");
    if (!shell) return; // not logged in - nothing to guard
    const minutes = parseFloat(shell.dataset.idleTimeoutMinutes || "0");
    if (!minutes || minutes <= 0) return;

    const IDLE_MS = minutes * 60 * 1000;
    const WARN_MS = Math.min(20000, IDLE_MS / 3); // warn for the last 20s (or less on a very short timeout)
    const PING_MIN_GAP_MS = 15000; // never ping the server more than once per 15s

    const warningModalEl = document.getElementById("idleWarningModal");
    const countdownEl = document.getElementById("idleCountdownSeconds");
    const stayBtn = document.getElementById("idleStayBtn");
    if (!warningModalEl) return;
    const warningModal = bootstrap.Modal.getOrCreateInstance(warningModalEl);

    let lastActivity = Date.now();
    let lastPing = 0;
    let warningShown = false;

    function pingServer() {
        const now = Date.now();
        if (now - lastPing < PING_MIN_GAP_MS) return;
        lastPing = now;
        fetch("/api/keep-alive", { credentials: "same-origin" }).catch(() => {});
    }

    function registerActivity() {
        lastActivity = Date.now();
        if (warningShown) {
            warningShown = false;
            warningModal.hide();
        }
        pingServer();
    }

    ["mousemove", "mousedown", "keydown", "scroll", "touchstart"].forEach((evt) => {
        document.addEventListener(evt, registerActivity, { passive: true });
    });

    stayBtn?.addEventListener("click", registerActivity);

    setInterval(function () {
        const idleFor = Date.now() - lastActivity;
        if (idleFor >= IDLE_MS) {
            window.location.href = "/logout";
        } else if (idleFor >= IDLE_MS - WARN_MS) {
            warningShown = true;
            const secondsLeft = Math.ceil((IDLE_MS - idleFor) / 1000);
            if (countdownEl) countdownEl.textContent = String(secondsLeft);
            if (!warningModalEl.classList.contains("show")) warningModal.show();
        }
    }, 1000);
}
