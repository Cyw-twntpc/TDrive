/**
 * @fileoverview Manages the entire UI and state for file transfers (uploads and downloads).
 *
 * This object tracks all active, queued, and completed transfers, renders the
 * sidebar status indicator and the detailed transfer dashboard, and handles user interactions.
 * * [Updated] Implements "Pulse Detection" & "Cosine Interpolation" for smooth chart visualization.
 * * [Updated] Supports Resume/Pause functionality and State Restoration on startup.
 */
const TransferManager = {
    uploads: new Map(),
    downloads: new Map(),
    updateInterval: null,
    concurrencyLimit: 3,
    currentDownloadDestination: '',
    AppState: null,
    ApiService: null,
    UIManager: null,
    refreshCallback: null,
    
    // [State]
    currentTab: 'uploads', // 'uploads', 'downloads', 'completed'
    completedSort: { key: 'time', order: 'desc' }, // Sorting state for history
    completedFilter: 'all', // 'all', 'upload', 'download'
    
    // --- Initialization ---
    initialize(AppState, ApiService, UIManager, refreshCallback) {
        this.AppState = AppState;
        this.ApiService = ApiService;
        this.UIManager = UIManager;
        this.refreshCallback = refreshCallback;
        this.setupEventListeners();
        
        if (window.tdrive_bridge) {
            if (window.tdrive_bridge.transfer_progress_updated) {
                window.tdrive_bridge.transfer_progress_updated.connect(this.updateTask.bind(this));
            }
            
            // Get initial traffic stats - just update the hero daily traffic text
            if (this.ApiService.getInitialTrafficStats) {
                this.ApiService.getInitialTrafficStats().then(data => {
                    if (data && data.todayTraffic !== undefined) {
                        const trafficEl = document.getElementById('hero-daily-traffic');
                        if (trafficEl) trafficEl.textContent = this.UIManager.formatBytes(data.todayTraffic);
                    }
                });
            }

            // [New] Restore incomplete transfers from backend state
            this.ApiService.getIncompleteTransfers().then(data => {
                if (data) {
                    this.restoreTasks(data.uploads, 'upload');
                    this.restoreTasks(data.downloads, 'download');
                    this.updateAllUI();
                }
            });
        }
    },

    // [New] Restore tasks from backend state
    restoreTasks(taskMap, type) {
        if (!taskMap) return;
        const CHUNK_SIZE = 33554432; // 32MB (1024*1024*32)

        for (const [taskId, info] of Object.entries(taskMap)) {
            // Estimate progress based on completed parts for both uploads and downloads
            let estimatedProgress = 0;
            if (info.transferred_parts && Array.isArray(info.transferred_parts)) {
                estimatedProgress = info.transferred_parts.length * CHUNK_SIZE;
                if (estimatedProgress > info.total_size) estimatedProgress = info.total_size;
            }

            const task = {
                id: taskId,
                name: (type === 'upload') ? info.file_path.split(/[\\/]/).pop() : info.file_details.name,
                size: info.total_size || 0,
                progress: estimatedProgress,
                speed: 0,
                status: info.status || 'paused', // Default to paused if undefined
                isFolder: false, // Controller mostly handles flat files for now
                localPath: (type === 'upload') ? info.file_path : info.save_path,
                parentFolderId: info.parent_id || null, // Only relevant for uploads
                feedbackShown: false,
                alertShown: false,
                startTime: info.created_at * 1000,
                completedAt: null
            };

            // If backend says transferring (zombie), force pause
            if (task.status === 'transferring') task.status = 'paused';

            if (type === 'upload') {
                this.uploads.set(taskId, task);
            } else {
                task.db_id = info.db_id;
                this.downloads.set(taskId, task);
            }
        }
    },
    
    // --- Chart & Hero Update (Direct Mode) ---
    // Removed onChartUpdate and renderSvgChart

    // --- Task Management ---
    addDownload(item) {
        if (this.downloads.has(item.task_id)) return;
        const task = { 
            id: item.task_id, db_id: item.db_id, name: item.name, size: item.size || 0, 
            progress: 0, speed: 0, status: 'queued', isFolder: item.type === 'folder',
            parentFolderId: this.AppState.currentFolderId, children: item.type === 'folder' ? new Map() : null,
            total_files: item.type === 'folder' ? 0 : 1, completed_files: 0, expanded: true, 
            feedbackShown: false, alertShown: false, itemData: item,
            startTime: Date.now(), completedAt: null
        };
        this.downloads.set(item.task_id, task);
        this.startUpdater();
    },

    addUpload(fileData) {
        const task_id = fileData.task_id;
        if (this.uploads.has(task_id)) return;
        const task = { 
            id: task_id, name: fileData.name, size: fileData.size || 0, 
            progress: 0, speed: 0, status: 'queued', 
            isFolder: fileData.isFolder || false, // Accept isFolder from fileData
            localPath: fileData.localPath, parentFolderId: fileData.parentFolderId,
            feedbackShown: false, alertShown: false,
            startTime: Date.now(), completedAt: null
        };
        this.uploads.set(task_id, task);
        this.startUpdater();
    },
    
    updateTask(data) {
        let task;
        let parentTask = null;

        // 1. Try to find existing parent task if parent_id is provided
        if (data.parent_id) {
            parentTask = this.downloads.get(data.parent_id) || this.uploads.get(data.parent_id);
            if (parentTask && parentTask.children) {
                task = parentTask.children.get(data.id);
                
                // [New] Dynamic Child Creation for Uploads
                if (!task && (parentTask === this.uploads.get(data.parent_id))) {
                    // Create placeholder child task for upload
                    task = {
                        id: data.id,
                        name: data.name,
                        size: data.size || data.total || 0, // 'total' is passed by adapter as size
                        progress: 0,
                        speed: 0,
                        status: 'queued',
                        isFolder: false,
                        parentFolderId: parentTask.parentFolderId, // Inherit logical parent
                        feedbackShown: false,
                        alertShown: false,
                        startTime: Date.now()
                    };
                    parentTask.children.set(data.id, task);
                }
            }
        } else {
            task = this.downloads.get(data.id) || this.uploads.get(data.id);
        }

        // [New] Dynamic Parent Creation for Upload Folder
        if (!task && data.status === 'starting_folder' && data.type === 'upload') {
            task = {
                id: data.id,
                name: data.name,
                size: data.size || 0, // total_size passed from backend
                progress: 0,
                speed: 0,
                status: 'starting_folder',
                isFolder: true,
                children: new Map(),
                total_files: data.total_files || 0,
                completed_files: 0,
                expanded: true,
                feedbackShown: false,
                alertShown: false,
                startTime: Date.now(),
                completedAt: null
            };
            this.uploads.set(data.id, task);
            this.startUpdater();
        }

        if (!task) return;

        // Record completion time
        if (data.status === 'completed' && !task.completedAt) {
            task.completedAt = Date.now();
        }

        // [New] Update global traffic stats from progress event
        if (data.todayTraffic !== undefined) {
            const trafficEl = document.getElementById('hero-daily-traffic');
            if (trafficEl) trafficEl.textContent = this.UIManager.formatBytes(data.todayTraffic);
        }

        if (data.status === 'starting_folder' && task.isFolder) {
            Object.assign(task, data);
            // If children map doesn't exist (should be created above), create it
            if (!task.children) task.children = new Map();
            
            // Handle pre-populated children (mostly for downloads, uploads use dynamic addition)
            if (data.children && Array.isArray(data.children)) {
                const folderNodes = new Map(); folderNodes.set('', task);
                const sortedChildren = (data.children || []).sort((a, b) => a.relative_path.replace(/\\/g, '/').length - b.relative_path.replace(/\\/g, '/').length);
                sortedChildren.forEach(childInfo => {
                    const pathParts = childInfo.relative_path.replace(/\\/g, '/').split('/');
                    const parentPath = pathParts.join('/');
                    const parentNode = folderNodes.get(parentPath);
                    if (!parentNode) return;
                    const isFolder = childInfo.type === 'folder';
                    const newNode = {
                        ...childInfo, isFolder, status: isFolder ? 'pending' : 'queued',
                        progress: 0, size: childInfo.size || 0, children: isFolder ? new Map() : null,
                        expanded: true, alertShown: false
                    };
                    parentNode.children.set(childInfo.id, newNode);
                    if (isFolder) folderNodes.set(childInfo.relative_path, newNode);
                });
            }
        } else {
            Object.assign(task, data);
        }

        if (parentTask) this.updateParentProgress(parentTask);

        if (task.status === 'failed' && !task.alertShown) {
            this.UIManager.handleBackendError({
                error_code: 'TRANSFER_FAILED',
                message: `"${task.name}" 傳輸失敗。<br><b>原因：</b> ${task.message || '未知錯誤'}`
            });
            task.alertShown = true;
        }
        this.startUpdater();
    },

    updateParentProgress(parentTask) {
        // ... (Same as before) ...
        if (!parentTask || !parentTask.isFolder) return;
        const _calculateFolderStats = (folderNode) => {
            let totalProgress = 0; let completedFiles = 0; let isTransferring = false;
            let hasFailures = false; let totalFiles = 0;
            for (const child of folderNode.children.values()) {
                if (child.isFolder) {
                    const stats = _calculateFolderStats(child);
                    totalProgress += stats.totalProgress; completedFiles += stats.completedFiles;
                    if (stats.isTransferring) isTransferring = true;
                    if (stats.hasFailures) hasFailures = true;
                    totalFiles += stats.totalFiles;
                } else {
                    totalFiles++; totalProgress += child.progress || 0;
                    if (child.status === 'completed') completedFiles++;
                    else if (child.status === 'failed' || child.status === 'cancelled') hasFailures = true;
                    else if (child.status === 'transferring' || child.status === 'queued') isTransferring = true;
                }
            }
            return { totalProgress, completedFiles, isTransferring, hasFailures, totalFiles };
        };
        const stats = _calculateFolderStats(parentTask);
        parentTask.progress = stats.totalProgress;
        parentTask.completed_files = stats.completedFiles;
        if (stats.hasFailures) parentTask.status = 'failed';
        else if (stats.completedFiles === parentTask.total_files && parentTask.total_files > 0) parentTask.status = 'completed';
        else if (stats.isTransferring) parentTask.status = 'transferring';
    },
    
    startUpdater() {
        if (!this.updateInterval) this.updateInterval = setInterval(() => this.tick(), 50);
    },
    
    tick() {
        this.updateAllUI();
        const allTransfers = [...this.uploads.values(), ...this.downloads.values()];
        // Check if all are strictly 'completed' or removed.
        // Paused/Failed tasks should keep the ticker alive to show them in list?
        // Actually, we can stop ticker if no *active* changes are expected, 
        // but 'starting_folder' or 'queued' might need polling if backend delays.
        // Simple logic: stop if map is empty.
        if (allTransfers.length === 0) {
            clearInterval(this.updateInterval);
            this.updateInterval = null;
            this.setPanelToReadyState();
        }
        
        const completedOrFailedTasks = allTransfers.filter(t => (t.status === 'completed' || t.status === 'failed') && !t.feedbackShown);
        if(completedOrFailedTasks.length > 0) {
            this.refreshCallback().then(() => {
                completedOrFailedTasks.forEach(task => {
                    if (!task.isFolder) this.showFileFeedback(task.name, task.status);
                    task.feedbackShown = true;
                });
            });
        }
    },

    updateAllUI() {
        this.updateSummaryPanel();
        if (this.AppState.currentPage === 'transfer') {
            this.renderDashboard();
        }
        this.updateMainFileListUI();
    },

    // --- Dashboard & Sidebar Rendering ---

    updateSummaryPanel() {
        const panel = document.getElementById('sidebar-transfer-status');
        const titleEl = document.getElementById('sidebar-transfer-title');
        const speedEl = document.getElementById('sidebar-transfer-speed');
        const barEl = document.getElementById('sidebar-transfer-bar');
        
        let totalSize = 0; let totalProgress = 0; let currentSpeed = 0; let activeCount = 0; let failedCount = 0;
        let activeUploads = 0; let activeDownloads = 0;
        const allTasks = [...this.uploads.values(), ...this.downloads.values()];

        if (allTasks.length === 0) { this.setPanelToReadyState(); return; }

        allTasks.forEach(task => {
            if (task.status !== 'cancelled') {
                totalSize += task.size;
                totalProgress += (task.status === 'completed') ? task.size : task.progress;
                // [Modified] Paused tasks count towards total size/progress but don't count as 'active' for speed
                if (['transferring', 'starting_folder', 'queued'].includes(task.status)) {
                    activeCount++;
                    currentSpeed += (task.speed || 0);
                    if (this.uploads.has(task.id)) activeUploads++;
                    else if (this.downloads.has(task.id)) activeDownloads++;
                } else if (task.status === 'failed') failedCount++;
            }
        });

        panel.classList.remove('status-completed', 'status-failed', 'transfer-active', 'upload-active', 'download-active', 'mixed-active');
        if (activeCount > 0) {
            panel.classList.add('transfer-active');
            if (activeUploads > 0 && activeDownloads === 0) panel.classList.add('upload-active');
            else if (activeDownloads > 0 && activeUploads === 0) panel.classList.add('download-active');
            else panel.classList.add('mixed-active');

            titleEl.textContent = `正在傳輸 ${activeCount} 個項目`;
            const formattedSpeed = this.UIManager.formatBytes(currentSpeed);
            speedEl.textContent = (currentSpeed > 0) ? `${formattedSpeed}/s` : '-- B/s';
        } else if (failedCount > 0) {
            panel.classList.add('status-failed');
            titleEl.textContent = `${failedCount} 個項目失敗`;
            speedEl.textContent = '';
        } else if (allTasks.some(t => t.status === 'paused')) {
            // New State: Paused
            panel.classList.add('transfer-active'); // Keep bar visible
            titleEl.textContent = '傳輸已暫停';
            speedEl.textContent = '-- B/s';
        } else {
            panel.classList.add('status-completed');
            titleEl.textContent = '傳輸完成';
            speedEl.textContent = '-- B/s'; 
        }
        const percent = totalSize > 0 ? (totalProgress / totalSize) : 0;
        barEl.style.transform = `scaleX(${percent})`;

        this.updateDashboardHero(currentSpeed, activeCount, allTasks);
    },

    setPanelToReadyState() {
        const panel = document.getElementById('sidebar-transfer-status');
        const titleEl = document.getElementById('sidebar-transfer-title');
        const speedEl = document.getElementById('sidebar-transfer-speed');
        const barEl = document.getElementById('sidebar-transfer-bar');
        panel.classList.remove('status-completed', 'status-failed', 'transfer-active', 'upload-active', 'download-active', 'mixed-active');
        titleEl.innerHTML = '&nbsp;'; speedEl.textContent = '-- B/s'; barEl.style.transform = 'scaleX(0)';
    },

    updateDashboardHero(currentSpeed, activeCount, allTasks) {
        const speedEl = document.getElementById('hero-total-speed');
        if (speedEl) {
            const formattedSpeed = this.UIManager.formatBytes(currentSpeed);
            speedEl.textContent = (currentSpeed > 0) ? `${formattedSpeed}/s` : '-- B/s';
        }
        
        const etaEl = document.getElementById('hero-eta');
        if (etaEl) {
            let totalRemaining = 0;
            allTasks.forEach(t => { 
                if(['transferring', 'queued', 'starting_folder'].includes(t.status)) 
                    totalRemaining += (t.size - t.progress);
            });
            
            if (currentSpeed > 0 && totalRemaining > 0) {
                const seconds = Math.ceil(totalRemaining / currentSpeed);
                if (!isFinite(seconds) || isNaN(seconds)) {
                    etaEl.textContent = '--:--:--';
                } else if (seconds > 86400) { 
                    etaEl.textContent = '> 1 天';
                } else {
                    etaEl.textContent = new Date(seconds * 1000).toISOString().substr(11, 8);
                }
            } else {
                etaEl.textContent = '--:--:--';
            }
        }
    },

    renderDashboard() {
        const container = document.getElementById('page-transfer');
        if (!container || container.classList.contains('hidden')) return;

        const liveView = document.getElementById('transfer-live-view');
        const completedView = document.getElementById('transfer-completed-view');
        
        if (this.currentTab === 'completed') {
            liveView.classList.add('hidden');
            completedView.classList.remove('hidden');
            this._renderCompletedList();
            return;
        }

        liveView.classList.remove('hidden');
        completedView.classList.add('hidden');

        const targetMap = (this.currentTab === 'uploads') ? this.uploads : this.downloads;
        const activeTasks = [];
        const failedTasks = [];
        const queuedTasks = [];

        for (const task of targetMap.values()) {
            if (task.status === 'completed' || task.status === 'cancelled') continue; 
            
            if (task.status === 'failed') {
                failedTasks.push(task);
            } else if (['transferring', 'starting_folder', 'paused'].includes(task.status)) { // Include paused in active list
                activeTasks.push(task);
            } else if (['queued', 'pending'].includes(task.status)) {
                queuedTasks.push(task);
            }
        }

        this._renderSection('active', activeTasks, this._createActiveCard.bind(this));
        this._renderSection('failed', failedTasks, this._createFailedCard.bind(this));
        this._renderSection('queued', queuedTasks, this._createQueuedCard.bind(this));
        
        const activeSection = document.getElementById('section-active');
        const failedSection = document.getElementById('section-failed');
        const queuedSection = document.getElementById('section-queued');

        if (activeSection) activeSection.classList.toggle('hidden', activeTasks.length === 0);
        if (failedSection) failedSection.classList.toggle('hidden', failedTasks.length === 0);
        if (queuedSection) queuedSection.classList.toggle('hidden', queuedTasks.length === 0);

        const countActive = document.getElementById('count-active');
        const countFailed = document.getElementById('count-failed');
        const countQueued = document.getElementById('count-queued');

        if (countActive) countActive.textContent = activeTasks.length;
        if (countFailed) countFailed.textContent = failedTasks.length;
        if (countQueued) countQueued.textContent = queuedTasks.length;
    },

    _renderSection(type, tasks, createCardFn) {
        const listEl = document.getElementById(`list-${type}`);
        if (!listEl) return; 
        
        const existingCards = new Map();
        listEl.querySelectorAll('.task-card-lg, .task-card-failed, .task-card-queued').forEach(el => {
            existingCards.set(el.dataset.id, el);
        });

        const toRemove = new Set(existingCards.keys());

        tasks.forEach(task => {
            if (existingCards.has(task.id)) {
                this._updateCard(type, existingCards.get(task.id), task);
                toRemove.delete(task.id);
            } else {
                const newCard = createCardFn(task);
                listEl.appendChild(newCard);
            }
        });

        toRemove.forEach(id => existingCards.get(id).remove());
    },

    toggleTaskState(id) {
        const result = this.findTask(id);
        if (!result) return;
        const { status } = result.task;
        
        if (['transferring', 'queued', 'starting_folder'].includes(status)) {
            this.pauseTask(id);
        } else if (['paused', 'failed'].includes(status)) {
            this.resumeTask(id);
        }
    },

    _createActiveCard(task) {
        const el = document.createElement('div');
        el.className = 'task-card-lg';
        el.dataset.id = task.id;
        
        const pathInfo = task.localPath ? 
            `<i class="fas fa-file-upload" style="color:#9ca3af;"></i> ${task.localPath}` : 
            `<i class="fas fa-cloud-download-alt" style="color:#9ca3af;"></i> 下載至 ${this.currentDownloadDestination || '預設路徑'}`;

        // [Modified] Toggle Pause/Resume icon based on status
        const isPaused = task.status === 'paused';
        const pauseIcon = isPaused ? 'fa-play' : 'fa-pause';
        const pauseTitle = isPaused ? '繼續' : '暫停';
        const pauseClass = isPaused ? 'btn-resume' : 'btn-pause';

        el.innerHTML = `
            <div class="card-row-main">
                <div class="file-icon-lg"><i class="${this.UIManager.getFileTypeIcon(task.name)}"></i></div>
                <div class="card-content">
                    <div class="file-title">${task.name}</div>
                    <div class="file-path">${pathInfo}</div>
                    <div class="progress-track"><div class="progress-fill" style="width: 0%;"></div></div>
                    <div class="meta-row">
                        <span class="meta-size">-- / --</span>
                        <span class="meta-speed">-- B/s • 計算中...</span>
                    </div>
                </div>
                <div class="card-actions">
                    <button class="icon-btn btn-toggle ${pauseClass}" title="${pauseTitle}"><i class="fas ${pauseIcon}"></i></button>
                    <button class="icon-btn btn-cancel" title="取消"><i class="fas fa-times"></i></button>
                    ${task.isFolder ? '<button class="icon-btn btn-expand"><i class="fas fa-chevron-down"></i></button>' : ''}
                </div>
            </div>
        `;
        
        this._bindCardActions(el, task);
        this._updateCard('active', el, task);
        return el;
    },

    _createFailedCard(task) {
        const el = document.createElement('div');
        el.className = 'task-card-failed';
        el.dataset.id = task.id;
        el.innerHTML = `
            <div style="display:flex; align-items:center; gap:15px;">
                <div style="color:var(--danger-color); font-size:20px;"><i class="fas fa-exclamation-circle"></i></div>
                <div class="failed-info">
                    <span style="font-weight:600; font-size:14px;">${task.name}</span>
                    <span class="failed-msg">${task.message || '未知錯誤'}</span>
                </div>
            </div>
            <div class="card-actions">
                <button class="icon-btn btn-retry" style="color:var(--primary-color);" title="重試"><i class="fas fa-redo"></i></button>
                <button class="icon-btn btn-cancel" title="取消"><i class="fas fa-times"></i></button>
            </div>
        `;
        this._bindCardActions(el, task);
        return el;
    },

    _createQueuedCard(task) {
        const el = document.createElement('div');
        el.className = 'task-card-queued';
        el.dataset.id = task.id;
        el.innerHTML = `
            <div class="drag-handle"><i class="fas fa-grip-vertical"></i></div>
            <div class="file-icon" style="width:30px; font-size:20px; text-align:center;"><i class="${this.UIManager.getFileTypeIcon(task.name)}"></i></div>
            <div class="queued-content">
                <div class="queued-name">${task.name}</div>
                <div class="queued-size">${this.UIManager.formatBytes(task.size)}</div>
            </div>
            <button class="icon-btn btn-cancel" title="取消"><i class="fas fa-times"></i></button>
        `;
        this._bindCardActions(el, task);
        return el;
    },

    _bindCardActions(el, task) {
        el.querySelector('.btn-cancel')?.addEventListener('click', () => this.cancelItem(task.id));
        
        // [New] Toggle Handler for Active Card (replaces separate pause/resume bindings)
        el.querySelector('.btn-toggle')?.addEventListener('click', () => this.toggleTaskState(task.id));
        
        // [New] Retry Handler for Failed Card
        el.querySelector('.btn-retry')?.addEventListener('click', () => this.resumeTask(task.id));
    },

    _updateCard(type, el, task) {
        if (type === 'active') {
            const percent = task.size > 0 ? (task.progress / task.size * 100) : 0;
            const fill = el.querySelector('.progress-fill');
            if(fill) fill.style.width = `${percent}%`;
            
            const sizeStr = `${this.UIManager.formatBytes(task.progress)} / ${this.UIManager.formatBytes(task.size)}`;
            el.querySelector('.meta-size').textContent = sizeStr;
            
            // [Modified] Display status text when paused/queued
            const metaSpeed = el.querySelector('.meta-speed');
            if (task.status === 'paused') {
                metaSpeed.textContent = '已暫停';
                metaSpeed.style.color = '#f59e0b';
                // Update Button State if needed (toggle icon)
                const btn = el.querySelector('.btn-pause');
                if (btn) {
                    btn.className = 'icon-btn btn-resume';
                    btn.innerHTML = '<i class="fas fa-play"></i>';
                    btn.title = '繼續';
                    // Rebind logic is hard here without recreating, simpler to recreate card in render loop
                }
            } else if (['queued', 'pending'].includes(task.status)) {
                metaSpeed.textContent = '等待中...';
            } else {
                metaSpeed.style.color = ''; // Reset color
                const speed = this.UIManager.formatBytes(task.speed);
                const speedStr = (task.speed > 0 && speed !== '0 B') ? `${speed}/s` : '-- B/s';
                let eta = '';
                if (task.speed > 0 && (task.size - task.progress) > 0) {
                    const sec = Math.ceil((task.size - task.progress) / task.speed);
                    eta = ` • 剩餘 ${sec > 60 ? Math.ceil(sec/60)+' 分' : sec+' 秒'}`;
                }
                metaSpeed.textContent = speedStr + eta;
                
                // Ensure button is Pause
                const btn = el.querySelector('.btn-resume');
                if (btn) {
                    btn.className = 'icon-btn btn-pause';
                    btn.innerHTML = '<i class="fas fa-pause"></i>';
                    btn.title = '暫停';
                }
            }
        }
    },

    _renderCompletedList() {
        const listEl = document.getElementById('list-completed');
        if (!listEl) return; 
        listEl.innerHTML = ''; 

        let candidates = [];
        if (this.completedFilter === 'all') {
            candidates = [...this.uploads.values(), ...this.downloads.values()];
        } else if (this.completedFilter === 'upload') {
            candidates = [...this.uploads.values()];
        } else if (this.completedFilter === 'download') {
            candidates = [...this.downloads.values()];
        }

        const allCompleted = [];
        candidates.forEach(t => {
            if (t.status === 'completed') allCompleted.push(t);
        });

        const { key, order } = this.completedSort;
        allCompleted.sort((a, b) => {
            let valA, valB;
            if (key === 'time') {
                valA = a.completedAt || 0;
                valB = b.completedAt || 0;
            } else if (key === 'name') {
                valA = a.name;
                valB = b.name;
            } else if (key === 'type') {
                valA = a.name.split('.').pop();
                valB = b.name.split('.').pop();
            }
            
            if (valA < valB) return order === 'asc' ? -1 : 1;
            if (valA > valB) return order === 'asc' ? 1 : -1;
            return 0;
        });

        allCompleted.forEach(task => {
            const row = document.createElement('div');
            row.className = 'history-item';
            const isUp = this.uploads.has(task.id);
            const badgeClass = 'sm-badge'; 
            const badgeText = isUp ? '上傳成功' : '下載成功';
            
            const pathInfo = isUp ? 
                `上傳至: TDrive` :
                `下載至: ${task.localPath || this.currentDownloadDestination || '預設路徑'}`;

            row.innerHTML = `
                <div class="sm-icon"><i class="${this.UIManager.getFileTypeIcon(task.name)}"></i></div>
                <div class="sm-name">
                    ${task.name}
                    <div style="font-size:12px; color:#9ca3af; margin-top:2px;">${pathInfo}</div>
                </div>
                <div class="${badgeClass}">${badgeText}</div>
                <a class="sm-action">${isUp ? '開啟' : '顯示位置'}</a>
            `;
            listEl.appendChild(row);
        });
    },
    
    sortCompleted(key) {
        if (this.completedSort.key === key) {
            this.completedSort.order = (this.completedSort.order === 'asc') ? 'desc' : 'asc';
        } else {
            this.completedSort.key = key;
            this.completedSort.order = 'desc'; 
        }
        
        document.querySelectorAll('.sort-item').forEach(el => {
            el.classList.toggle('active', el.dataset.sort === key);
            const icon = el.querySelector('i');
            if (icon) {
                if (el.dataset.sort === key) {
                    icon.className = this.completedSort.order === 'asc' ? 'fas fa-sort-up' : 'fas fa-sort-down';
                } else {
                    icon.className = 'fas fa-sort'; 
                }
            }
        });

        this._renderCompletedList();
    },

    updateMainFileListUI() {
        document.querySelectorAll('.file-item:not(.is-uploading)').forEach(el => {
            const name = el.dataset.name;
            let task = null;
            const findTask = (map) => { for (const t of map.values()) { if (t.name === name && t.parentFolderId === this.AppState.currentFolderId) return t; } return null; };
            task = findTask(this.uploads) || findTask(this.downloads);
            el.classList.remove('in-transfer');
            if(task && ['transferring', 'paused', 'queued', 'starting_folder'].includes(task.status)) el.classList.add('in-transfer');
        });
    },
    
    showFileFeedback(name, status) {
        const el = document.querySelector(`.file-item[data-name="${CSS.escape(name)}"]`);
        if (!el) return;
        el.classList.remove('in-transfer');
        const flashClass = status === 'completed' ? 'flash-success' : 'flash-fail';
        el.classList.add(flashClass);
        setTimeout(() => el.classList.remove(flashClass), 1000);
    },
    
    // --- Control Actions ---

    pauseTask(id) {
        const result = this.findTask(id);
        if (result && ['transferring', 'queued', 'starting_folder'].includes(result.task.status)) {
            // Optimistic update
            result.task.status = 'paused';
            result.task.speed = 0;
            this.tick(); // Force UI update
            
            this.ApiService.pauseTransfer(id).then(res => {
                if (!res.success) console.warn(`Pause failed: ${res.message}`);
            });
        }
    },

    resumeTask(id) {
        const result = this.findTask(id);
        if (result && ['paused', 'failed'].includes(result.task.status)) {
            // Optimistic update
            result.task.status = 'queued'; // Waiting for backend
            result.task.message = null; // Clear error msg
            this.tick();
            
            this.ApiService.resumeTransfer(id).then(res => {
                if (!res.success) {
                    result.task.status = 'failed'; // Revert if call fails
                    result.task.message = res.message;
                    this.tick();
                }
            });
            this.startUpdater(); // Ensure ticker is running
        }
    },

    cancelItem(id) {
        const result = this.findTask(id);
        if (result) {
            // Call backend to cancel/remove
            this.ApiService.cancelTransfer(id).then(res => { if (!res.success) console.warn(`Failed to cancel ${id}`); });
            
            // Remove from local map
            const map = result.parent ? result.parent.children : result.map;
            map.delete(result.task.id);
            this.tick();
        }
    },

    cancelAll() {
        const _cancelRecursively = (map) => {
            map.forEach(task => {
                if (task.isFolder && task.children) {
                    _cancelRecursively(task.children);
                }
                // Call cancel for each task
                // (Optimized: Could add 'cancelAll' endpoint in backend, but iteration works)
                this.cancelItem(task.id);
            });
        };
        _cancelRecursively(this.uploads);
        _cancelRecursively(this.downloads);
    },

    resumeAll() {
        // Renamed from retryAll to cover both paused and failed
        const _resumeLoop = (map) => {
            map.forEach(task => {
                if (['paused', 'failed'].includes(task.status)) {
                    this.resumeTask(task.id);
                }
            });
        };
        _resumeLoop(this.uploads);
        _resumeLoop(this.downloads);
    },

    pauseAll() {
        const _pauseLoop = (map) => {
            map.forEach(task => {
                if (['transferring', 'starting_folder', 'queued'].includes(task.status)) {
                    this.pauseTask(task.id);
                }
            });
        };
        _pauseLoop(this.uploads);
        _pauseLoop(this.downloads);
    },
    
    findTask(id) {
        const _findRecursive = (searchId, map, parent = null) => {
            for (const task of map.values()) {
                if (task.id === searchId) return { task, map, parent };
                if (task.isFolder && task.children) {
                    const result = _findRecursive(searchId, task.children, task);
                    if (result) return result;
                }
            }
            return null;
        };
        return _findRecursive(id, this.uploads) || _findRecursive(id, this.downloads);
    },

    clearCompleted() {
        const _filterRecursively = (map) => {
            for (let [key, task] of map.entries()) {
                if (task.isFolder && task.children) _filterRecursively(task.children);
                if (['completed', 'failed', 'cancelled'].includes(task.status)) map.delete(key);
            }
        };
        _filterRecursively(this.uploads); _filterRecursively(this.downloads);
        this.tick();
        if (this.currentTab === 'completed') this._renderCompletedList();
    },

    setConcurrencyLimit(limit) { this.concurrencyLimit = limit; },
    getConcurrencyLimit() { return this.concurrencyLimit; },
    setDownloadDestination(path) { this.currentDownloadDestination = path; },
    
    setupEventListeners() {
        const sidebarStatus = document.getElementById('sidebar-transfer-status');
        if (sidebarStatus) {
            sidebarStatus.addEventListener('click', () => {
                if(window.switchPage) window.switchPage('transfer');
            });
        }

        document.querySelectorAll('.tabs-container .tab-item').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelector('.tabs-container .tab-item.active').classList.remove('active');
                btn.classList.add('active');
                this.currentTab = btn.dataset.tab;
                this.renderDashboard();
            });
        });

        // Completed Filter Handlers
        document.querySelectorAll('.filter-segment').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.filter-segment').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.completedFilter = btn.dataset.filter;
                this._renderCompletedList();
            });
        });

        // Completed Sort Handlers
        document.querySelectorAll('.sort-item').forEach(btn => {
            btn.addEventListener('click', () => {
                this.sortCompleted(btn.dataset.sort);
            });
        });

        document.getElementById('global-cancel-btn')?.addEventListener('click', () => this.cancelAll());
        document.getElementById('global-pause-btn')?.addEventListener('click', () => this.pauseAll());
        
        // Updated: 'retry-all' now maps to resumeAll
        document.getElementById('retry-all-btn')?.addEventListener('click', () => this.resumeAll());
    }
};