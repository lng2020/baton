// Baton — Single-layout dashboard controller
(function () {
    // ---- State ----
    let selectedProjectId = null;
    let projects = [];
    const statuses = ["pending", "in_progress", "completed", "failed"];

    // ---- DOM refs ----
    const projectList = document.getElementById("project-list");
    const kanbanTitle = document.getElementById("kanban-title");
    const projectHealth = document.getElementById("project-health");
    const btnNewTask = document.getElementById("btn-new-task");
    const worktreesContent = document.getElementById("worktrees-content");
    const commitsContent = document.getElementById("commits-content");
    const consoleOutput = document.getElementById("console-output");
    const consoleClear = document.getElementById("console-clear");

    // Detail panel refs
    const detailOverlay = document.getElementById("detail-overlay");
    const detailPanel = document.getElementById("detail-panel");
    const detailBody = document.getElementById("detail-body");
    const detailClose = document.getElementById("detail-close");

    // Create task modal refs
    const createOverlay = document.getElementById("create-task-overlay");
    const createForm = document.getElementById("create-task-form");

    // Sidebar toggle refs
    const toggleLeft = document.getElementById("toggle-left");
    const toggleRight = document.getElementById("toggle-right");
    const sidebarLeft = document.getElementById("sidebar-left");
    const sidebarRight = document.getElementById("sidebar-right");

    // ---- Helpers ----
    function escHtml(s) {
        const d = document.createElement("div");
        d.textContent = s || "";
        return d.innerHTML;
    }

    function escAttr(s) {
        return (s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/'/g, "&#39;");
    }

    // ---- Projects ----
    async function loadProjects() {
        try {
            const res = await fetch("/api/projects");
            if (!res.ok) throw new Error(res.statusText);
            projects = await res.json();
            renderProjectList();
            // Auto-select first project if none selected
            if (!selectedProjectId && projects.length) {
                selectProject(projects[0].id);
            }
        } catch (err) {
            projectList.innerHTML = '<div class="empty-state">Failed to load projects</div>';
            console.error(err);
        }
    }

    function renderProjectList() {
        if (!projects.length) {
            projectList.innerHTML = '<div class="empty-state">No projects</div>';
            return;
        }
        projectList.innerHTML = projects.map(p => {
            const c = p.task_counts;
            const selected = p.id === selectedProjectId ? " selected" : "";
            const healthClass = p.healthy ? "healthy" : "unhealthy";
            const counts = statuses
                .filter(s => (c[s] || 0) > 0)
                .map(s => `<span class="project-item-count ${s}">${c[s]}</span>`)
                .join("");
            return `
                <div class="project-item${selected}" data-id="${escAttr(p.id)}" onclick="window._selectProject('${escAttr(p.id)}')">
                    <span class="health-dot ${healthClass}"></span>
                    <span class="project-item-name">${escHtml(p.name)}</span>
                    <span class="project-item-counts">${counts}</span>
                </div>
            `;
        }).join("");
    }

    function selectProject(id) {
        selectedProjectId = id;
        const proj = projects.find(p => p.id === id);

        // Update header
        kanbanTitle.textContent = proj ? proj.name : "Select a project";
        if (proj) {
            projectHealth.className = "health-dot " + (proj.healthy ? "healthy" : "unhealthy");
            projectHealth.style.display = "";
        } else {
            projectHealth.style.display = "none";
        }

        // Enable new task button
        btnNewTask.disabled = !proj;

        // Highlight in sidebar
        renderProjectList();

        // Load data
        loadTasks();
        loadWorktrees();
        loadCommits();

        // Clear console
        consoleOutput.innerHTML = '<div class="empty-state">Select a task to view agent output</div>';
    }

    // Expose to onclick handlers
    window._selectProject = selectProject;

    // ---- Kanban Board ----
    async function loadTasks() {
        if (!selectedProjectId) return;
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/tasks`);
            if (!res.ok) throw new Error(res.statusText);
            const tasks = await res.json();
            renderTasks(tasks);
        } catch (err) {
            console.error("Failed to load tasks:", err);
        }
    }

    function renderTasks(tasks) {
        for (const status of statuses) {
            const list = document.querySelector(`[data-list="${status}"]`);
            const count = document.querySelector(`[data-count="${status}"]`);
            const items = tasks[status] || [];
            count.textContent = items.length;
            if (!items.length) {
                list.innerHTML = '<div class="empty-state">No tasks</div>';
                continue;
            }
            list.innerHTML = items.map(t => {
                const modified = new Date(t.modified).toLocaleDateString();
                const errorBadge = t.has_error_log ? '<span class="error-badge">error log</span>' : "";
                return `
                    <div class="task-card" onclick="window._openTaskDetail('${status}', '${escAttr(t.filename)}')">
                        <h4>${escHtml(t.title)}</h4>
                        <div class="task-meta">${escHtml(t.id)} &middot; ${modified}</div>
                        ${errorBadge}
                    </div>
                `;
            }).join("");
        }
    }

    // ---- Task Detail Panel ----
    function openTaskDetail(status, filename) {
        detailBody.innerHTML = '<div class="loading">Loading...</div>';
        detailOverlay.classList.add("open");
        detailPanel.classList.add("open");

        fetch(`/api/projects/${selectedProjectId}/tasks/${status}/${filename}`)
            .then(res => {
                if (!res.ok) throw new Error(res.statusText);
                return res.json();
            })
            .then(task => {
                renderDetail(task);
                renderConsole(task);
            })
            .catch(err => {
                detailBody.innerHTML = `<div class="empty-state">Failed to load task: ${escHtml(err.message)}</div>`;
            });
    }

    function closeDetail() {
        detailOverlay.classList.remove("open");
        detailPanel.classList.remove("open");
    }

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

    // Expose to onclick handlers
    window._openTaskDetail = openTaskDetail;

    detailClose.addEventListener("click", closeDetail);
    detailOverlay.addEventListener("click", closeDetail);
    document.addEventListener("keydown", e => {
        if (e.key === "Escape") {
            closeDetail();
            closeCreateModal();
        }
    });

    // ---- Console Panel ----
    function renderConsole(task) {
        if (!task.session_log || !task.session_log.length) {
            consoleOutput.innerHTML = `<div class="empty-state">No agent output for ${escHtml(task.id)}</div>`;
            return;
        }

        consoleOutput.innerHTML = task.session_log.map(entry => {
            const type = entry.type || "info";
            const text = typeof entry === "string" ? entry : (entry.message || entry.content || JSON.stringify(entry));
            return `<div class="console-entry"><span class="console-type ${escAttr(type)}">[${escHtml(type)}]</span>${escHtml(text)}</div>`;
        }).join("");

        // Auto-scroll to bottom
        consoleOutput.scrollTop = consoleOutput.scrollHeight;
    }

    consoleClear.addEventListener("click", () => {
        consoleOutput.innerHTML = '<div class="empty-state">Console cleared</div>';
    });

    // ---- Worktrees ----
    async function loadWorktrees() {
        if (!selectedProjectId) return;
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/worktrees`);
            if (!res.ok) return;
            const worktrees = await res.json();
            if (!worktrees.length) {
                worktreesContent.innerHTML = '<div class="empty-state">No worktrees</div>';
                return;
            }
            worktreesContent.innerHTML = worktrees.map(w => `
                <div class="info-item">
                    <strong>${escHtml(w.branch || "(detached)")}</strong><br>
                    <code>${escHtml(w.commit ? w.commit.substring(0, 8) : "")}</code>
                    &middot; ${escHtml(w.path)}
                    ${w.is_bare ? " (bare)" : ""}
                </div>
            `).join("");
        } catch (err) {
            console.error("Failed to load worktrees:", err);
        }
    }

    // ---- Recent Commits ----
    async function loadCommits() {
        if (!selectedProjectId) return;
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/commits`);
            if (!res.ok) return;
            const commits = await res.json();
            if (!commits.length) {
                commitsContent.innerHTML = '<div class="empty-state">No commits</div>';
                return;
            }
            commitsContent.innerHTML = commits.map(c => `
                <div class="info-item">
                    <code>${escHtml(c.sha.substring(0, 8))}</code>
                    <strong>${escHtml(c.message)}</strong><br>
                    ${escHtml(c.author)} &middot; ${escHtml(c.date)}
                </div>
            `).join("");
        } catch (err) {
            console.error("Failed to load commits:", err);
        }
    }

    // ---- Create Task Modal ----
    function openCreateModal() {
        createOverlay.classList.add("open");
        document.getElementById("task-title").focus();
    }

    function closeCreateModal() {
        createOverlay.classList.remove("open");
    }

    function clearCreateModal() {
        document.getElementById("task-title").value = "";
        document.getElementById("task-content").value = "";
    }

    btnNewTask.addEventListener("click", openCreateModal);
    document.getElementById("create-task-close").addEventListener("click", closeCreateModal);
    document.getElementById("create-task-cancel").addEventListener("click", () => {
        clearCreateModal();
        closeCreateModal();
    });
    createOverlay.addEventListener("click", e => {
        if (e.target === createOverlay) closeCreateModal();
    });

    createForm.addEventListener("submit", async e => {
        e.preventDefault();
        const title = document.getElementById("task-title").value.trim();
        const content = document.getElementById("task-content").value;
        if (!title || !selectedProjectId) return;
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/tasks`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title, content }),
            });
            if (!res.ok) throw new Error(res.statusText);
            clearCreateModal();
            closeCreateModal();
            loadTasks();
        } catch (err) {
            console.error("Failed to create task:", err);
            alert("Failed to create task: " + err.message);
        }
    });

    // ---- Sidebar Toggles (responsive) ----
    toggleLeft.addEventListener("click", () => sidebarLeft.classList.toggle("open"));
    toggleRight.addEventListener("click", () => sidebarRight.classList.toggle("open"));

    // ---- Collapsible panel toggle ----
    window.togglePanel = function (header) {
        header.classList.toggle("open");
        const content = header.nextElementSibling;
        content.classList.toggle("open");
    };

    // ---- Init & Polling ----
    loadProjects();
    setInterval(loadProjects, 30000);
    setInterval(() => {
        if (selectedProjectId) {
            loadTasks();
            loadWorktrees();
            loadCommits();
        }
    }, 15000);
})();
