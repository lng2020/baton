// Task detail slide-in panel
const detailOverlay = document.getElementById("detail-overlay");
const detailPanel = document.getElementById("detail-panel");
const detailBody = document.getElementById("detail-body");
const detailClose = document.getElementById("detail-close");

function openTaskDetail(status, filename) {
    detailBody.innerHTML = '<div class="loading">Loading...</div>';
    detailOverlay.classList.add("open");
    detailPanel.classList.add("open");

    const projectId = window.BATON_PROJECT_ID;
    fetch(`/api/projects/${projectId}/tasks/${status}/${filename}`)
        .then(res => {
            if (!res.ok) throw new Error(res.statusText);
            return res.json();
        })
        .then(task => renderDetail(task))
        .catch(err => {
            detailBody.innerHTML = `<div class="empty-state">Failed to load task: ${escHtml(err.message)}</div>`;
        });
}

function closeDetail() {
    detailOverlay.classList.remove("open");
    detailPanel.classList.remove("open");
}

detailClose.addEventListener("click", closeDetail);
detailOverlay.addEventListener("click", closeDetail);
document.addEventListener("keydown", e => {
    if (e.key === "Escape") closeDetail();
});

function renderDetail(task) {
    let html = `
        <h2>${escHtml(task.title)}</h2>
        <span class="detail-status ${task.status}">${task.status.replace("_", " ")}</span>
        <div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.5rem;">
            ${escHtml(task.id)} &middot; Modified: ${new Date(task.modified).toLocaleString()}
        </div>
    `;

    if (task.pr) {
        html += `
            <div class="detail-section">
                <h3>Pull Request</h3>
                <div class="detail-pr">
                    <a href="${escAttr(task.pr.url)}" target="_blank">#${task.pr.number} ${escHtml(task.pr.title)}</a>
                    <div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.3rem;">
                        ${escHtml(task.pr.state)} &middot; ${escHtml(task.pr.branch)}
                    </div>
                </div>
            </div>
        `;
    }

    html += `
        <div class="detail-section">
            <h3>Content</h3>
            <div class="detail-content">${escHtml(task.content)}</div>
        </div>
    `;

    if (task.error_log) {
        html += `
            <div class="detail-section">
                <h3>Error Log</h3>
                <div class="detail-content detail-error-log">${escHtml(task.error_log)}</div>
            </div>
        `;
    }

    if (task.session_log && task.session_log.length) {
        const summary = task.session_log.length + " session log entries";
        html += `
            <div class="detail-section">
                <h3>Session Log</h3>
                <div class="detail-content">${escHtml(summary)}\n\n${escHtml(JSON.stringify(task.session_log.slice(0, 5), null, 2))}</div>
            </div>
        `;
    }

    detailBody.innerHTML = html;
}

function escHtml(s) {
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
}

function escAttr(s) {
    return (s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}
