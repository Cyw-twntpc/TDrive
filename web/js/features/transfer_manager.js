/**
 * @fileoverview Manages the entire UI and state for file transfers (uploads and downloads).
 * Optimized for local rendering and simplified data structure.
 */
const TransferManager = {
    uploads: new Map(),
    downloads: new Map(),
    updateInterval: null,
    currentDownloadDestination: '',
    AppState: null,
    ApiService: null,
    UIManager: null,
    refreshCallback: null,
    
    // [State]
    currentTab: 'uploads', 
    completedSort: { key: 'time', order: 'desc' },
    completedFilter: 'all',
    _completedListDirty: true,
    
    // --- Initialization ---
    initialize(AppState, ApiService, UIManager, refreshCallback) {
        this.AppState = AppState;
        this.ApiService = ApiService;
        this.UIManager = UIManager;
        this.refreshCallback = refreshCallback;
        this.setupEventListeners();
        
        if (window.tdrive_bridge && window.tdrive_bridge.transfer_progress_updated) {
            window.tdrive_bridge.transfer_progress_updated.connect(this.updateTask.bind(this));
        }

        // Restore tasks from backend state
        this.ApiService.getIncompleteTransfers().then(data => {
            if (data) {
                this.restoreTasks(data.uploads, 'upload');
                this.restoreTasks(data.downloads, 'download');
                this.updateAllUI();
            }
        });

        // Get initial traffic stats
        if (this.ApiService.getInitialTrafficStats) {
            this.ApiService.getInitialTrafficStats().then(data => {
                if (data && data.todayTraffic !== undefined) {
                    const trafficEl = document.getElementById('hero-daily-traffic');
                    if (trafficEl) trafficEl.textContent = this.UIManager.formatBytes(data.todayTraffic);
                }
            });
        }
    },

    // Restore tasks from backend state (32MB Chunk Size)
    restoreTasks(taskMap, type) {
        if (!taskMap) return;
        const CHUNK_SIZE = 33554432; // 32MB

        for (const [taskId, info] of Object.entries(taskMap)) {
            const calculateProgress = (taskInfo) => {
                let p = 0;
                if (taskInfo.transferred_parts && Array.isArray(taskInfo.transferred_parts)) {
                    p = taskInfo.transferred_parts.length * CHUNK_SIZE;
                    if (p > taskInfo.total_size) p = taskInfo.total_size;
                    if (taskInfo.status === 'completed') p = taskInfo.total_size;
                }
                return p;
            };

            const isFolder = info.is_folder || false;
            let estimatedProgress = 0;
            
            if (isFolder && info.child_tasks) {
                for (const childInfo of Object.values(info.child_tasks)) {
                    estimatedProgress += calculateProgress(childInfo);
                }
            } else {
                estimatedProgress = calculateProgress(info);
            }

            const task = {
                id: taskId,
                name: (type === 'upload') ? info.file_path.split(/[\\/]/).pop() : (info.file_details?.name || 'Unknown'),
                size: info.total_size || 0,
                progress: estimatedProgress,
                status: info.status === 'transferring' ? 'paused' : info.status,
                parentFolderId: info.parent_id || null, 
                localPath: (type === 'upload') ? info.file_path : info.save_path,
                db_id: info.db_id,
                feedbackShown: false,
                alertShown: false,
                startTime: info.created_at * 1000 || Date.now(),
                completedAt: (info.status === 'completed' && info.updated_at) ? info.updated_at * 1000 : null,
                type: type
            };

            if (isFolder && type === 'download' && !task.name) {
                task.name = info.save_path ? info.save_path.split(/[\\/]/).pop() : 'Unknown Folder';
            }

            if (type === 'upload') this.uploads.set(taskId, task);
            else this.downloads.set(taskId, task);
        }
    },

    // --- Task Management ---
    addDownload(item) {
        if (this.downloads.has(item.task_id)) return;
        const task = { 
            id: item.task_id, db_id: item.db_id, name: item.name, size: item.size || 0, 
            progress: 0, status: 'queued', localPath: item.save_path || this.currentDownloadDestination, 
            parentFolderId: this.AppState.currentFolderId, feedbackShown: false, 
            alertShown: false, startTime: Date.now(), completedAt: null,
            type: 'download'
        };
        this.downloads.set(item.task_id, task);
        this.startUpdater();
    },

    addUpload(fileData) {
        if (this.uploads.has(fileData.task_id)) return;
        const task = { 
            id: fileData.task_id, name: fileData.name, size: fileData.size || 0, 
            progress: 0, status: 'queued', localPath: fileData.localPath, 
            parentFolderId: fileData.parentFolderId, feedbackShown: false, 
            alertShown: false, startTime: Date.now(), completedAt: null,
            type: 'upload'
        };
        this.uploads.set(fileData.task_id, task);
        this.startUpdater();
    },
    
    updateTask(data) {
        if (data.parent_id) return; // Only track main tasks

        let task = this.downloads.get(data.id) || this.uploads.get(data.id);

        if (!task && data.status === 'starting_folder' && data.type === 'upload') {
             task = {
                id: data.id, name: data.name, size: data.size || 0, progress: 0,
                status: 'transferring', feedbackShown: false, alertShown: false,
                startTime: Date.now(), completedAt: null,
                type: 'upload'
            };
            this.uploads.set(data.id, task);
            this.startUpdater();
            return;
        }

        if (!task) return;

        if (data.status === 'completed' && !task.completedAt) task.completedAt = Date.now();

        if (data.todayTraffic !== undefined) {
            const trafficEl = document.getElementById('hero-daily-traffic');
            if (trafficEl) trafficEl.textContent = this.UIManager.formatBytes(data.todayTraffic);
        }

        // Handle Delta vs Absolute Progress
        if (data.delta !== undefined && data.delta !== null) {
            task.progress += data.delta;
            if (task.size > 0 && task.progress > task.size) task.progress = task.size;
        } else if (data.transferred !== undefined) {
            task.progress = data.transferred;
        }

        let statusChanged = false;
        if (data.status) {
            const newStatus = (data.status === 'starting_folder') ? 'transferring' : data.status;
            if (task.status !== newStatus) {
                task.status = newStatus;
                statusChanged = true;
                // Mark completed list dirty if a task finishes or fails
                if (['completed', 'failed'].includes(newStatus)) {
                    this._completedListDirty = true;
                }
            }
        }
        
        if (data.speed !== undefined) task.speed = data.speed;
        if (data.total !== undefined && data.total > 0) task.size = data.total;
        if (data.error_message) task.message = data.error_message;

        if (task.status === 'failed' && !task.alertShown) {
            this.UIManager.handleBackendError({
                error_code: 'TRANSFER_FAILED',
                message: `"${task.name}" 傳輸失敗。<br><b>原因：</b> ${task.message || '未知錯誤'}`
            });
            task.alertShown = true;
        }

        // Local Rendering Optimization
        if (statusChanged) {
            if (this.AppState.currentPage === 'transfer') this.renderDashboard();
        } else {
            this.renderTaskCard(task);
        }
        this.updateSummaryPanel();
        this.startUpdater();
    },

    startUpdater() {
        if (!this.updateInterval) this.updateInterval = setInterval(() => this.tick(), 50);
    },
    
    tick() {
        this.updateAllUI();
        const allTransfers = [...this.uploads.values(), ...this.downloads.values()];
        if (allTransfers.length === 0) {
            clearInterval(this.updateInterval);
            this.updateInterval = null;
            this.setPanelToReadyState();
        }
        
        const completedOrFailedTasks = allTransfers.filter(t => (t.status === 'completed' || t.status === 'failed') && !t.feedbackShown);
        if(completedOrFailedTasks.length > 0) {
            this.refreshCallback().then(() => {
                completedOrFailedTasks.forEach(task => {
                    this.showFileFeedback(task.name, task.status);
                    task.feedbackShown = true;
                });
            });
        }
    },

    updateAllUI() {
        this.updateSummaryPanel();
        if (this.AppState.currentPage === 'transfer') this.renderDashboard();
        this.updateMainFileListUI();
    },

    // --- Dashboard & Sidebar Rendering ---
    updateSummaryPanel() {
        const panel = document.getElementById('sidebar-transfer-status');
        const titleEl = document.getElementById('sidebar-transfer-title');
        const speedEl = document.getElementById('sidebar-transfer-speed');
        const barEl = document.getElementById('sidebar-transfer-bar');
        
        let totalSize = 0, totalProgress = 0, currentSpeed = 0, activeCount = 0, failedCount = 0;
        let activeUploads = 0, activeDownloads = 0;
        const allTasks = [...this.uploads.values(), ...this.downloads.values()];

        if (allTasks.length === 0) { this.setPanelToReadyState(); return; }

        allTasks.forEach(task => {
            if (task.status !== 'cancelled') {
                totalSize += task.size;
                const currentProgress = (task.status === 'completed') ? task.size : task.progress;
                totalProgress += currentProgress;
                
                if (['transferring', 'queued', 'starting_folder'].includes(task.status)) {
                    activeCount++;
                    currentSpeed += (task.speed || 0);
                    if (this.uploads.has(task.id)) activeUploads++;
                    else activeDownloads++;
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
            speedEl.textContent = (currentSpeed > 0) ? `${this.UIManager.formatBytes(currentSpeed)}/s` : '-- B/s';
        } else if (failedCount > 0) {
            panel.classList.add('status-failed');
            titleEl.textContent = `${failedCount} 個項目失敗`;
            speedEl.textContent = '';
        } else if (allTasks.some(t => t.status === 'paused')) {
            panel.classList.add('transfer-active');
            titleEl.textContent = '傳輸已暫停';
            speedEl.textContent = '-- B/s';
        } else {
            panel.classList.add('status-completed');
            titleEl.textContent = '傳輸完成';
            speedEl.textContent = '-- B/s'; 
        }
        barEl.style.transform = `scaleX(${totalSize > 0 ? totalProgress / totalSize : 0})`;
        this.updateDashboardHero(currentSpeed, activeCount, allTasks);
    },

    setPanelToReadyState() {
        const panel = document.getElementById('sidebar-transfer-status');
        panel.classList.remove('status-completed', 'status-failed', 'transfer-active', 'upload-active', 'download-active', 'mixed-active');
        document.getElementById('sidebar-transfer-title').innerHTML = '&nbsp;';
        document.getElementById('sidebar-transfer-speed').textContent = '-- B/s';
        document.getElementById('sidebar-transfer-bar').style.transform = 'scaleX(0)';
    },

    updateDashboardHero(currentSpeed, activeCount, allTasks) {
        const speedEl = document.getElementById('hero-total-speed');
        if (speedEl) speedEl.textContent = currentSpeed > 0 ? `${this.UIManager.formatBytes(currentSpeed)}/s` : '-- B/s';
        
        const etaEl = document.getElementById('hero-eta');
        if (etaEl) {
            let totalRemaining = 0;
            allTasks.forEach(t => { if(['transferring', 'queued'].includes(t.status)) totalRemaining += (t.size - t.progress); });
            if (currentSpeed > 0 && totalRemaining > 0) {
                const seconds = Math.ceil(totalRemaining / currentSpeed);
                etaEl.textContent = seconds > 86400 ? '> 1 天' : new Date(seconds * 1000).toISOString().substr(11, 8);
            } else etaEl.textContent = '--:--:--';
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
            if (this._completedListDirty) {
                this._renderCompletedList();
            }
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
            } else if (['transferring', 'paused', 'starting_folder'].includes(task.status)) {
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

        // Empty State Handling
        if (activeTasks.length === 0 && failedTasks.length === 0 && queuedTasks.length === 0) {
            if (activeSection) {
                activeSection.classList.remove('hidden');
                const listActive = document.getElementById('list-active');
                if (listActive) this.renderEmptyState(listActive);
            }
        }
    },

    renderEmptyState(container) {
        container.innerHTML = `
            <div class="empty-state" style="text-align:center; padding:40px; color:#9ca3af;">
                <i class="fas fa-tasks" style="font-size:48px; margin-bottom:15px; display:block;"></i>
                <p>暫無傳輸任務</p>
            </div>
        `;
    },

    checkEmptyState() {
        // Optional helper if needed for explicit checks
        const activeCount = [...this.uploads.values(), ...this.downloads.values()].filter(t => !['completed', 'cancelled'].includes(t.status)).length;
        if (activeCount === 0) {
             this.renderDashboard();
        }
    },

    _renderSection(type, tasks, createCardFn) {
        const listEl = document.getElementById(`list-${type}`);
        if (!listEl) return; 

        // Remove empty state if we have tasks to render
        if (tasks.length > 0) {
            const emptyState = listEl.querySelector('.empty-state');
            if (emptyState) emptyState.remove();
        }

        const existingCards = new Map();
        listEl.querySelectorAll('[data-id]').forEach(el => existingCards.set(el.dataset.id, el));
        const toRemove = new Set(existingCards.keys());

        tasks.forEach(task => {
            if (existingCards.has(task.id)) {
                this.renderTaskCard(task);
                toRemove.delete(task.id);
            } else listEl.appendChild(createCardFn(task));
        });
        toRemove.forEach(id => existingCards.get(id).remove());
    },

    renderTaskCard(task, targetElement = null) {
        const el = targetElement || document.querySelector(`[data-id="${task.id}"]`);
        if (!el) return;

        const fill = el.querySelector('.progress-fill');
        if (fill) fill.style.width = `${task.size > 0 ? (task.progress / task.size * 100) : 0}%`;

        const sizeEl = el.querySelector('.meta-size');
        if (sizeEl) sizeEl.textContent = `${this.UIManager.formatBytes(task.progress)} / ${this.UIManager.formatBytes(task.size)}`;

        const speedEl = el.querySelector('.meta-speed');
        if (speedEl) {
            if (task.status === 'paused') {
                speedEl.textContent = '已暫停';
                speedEl.style.color = '#f59e0b';
                const btn = el.querySelector('.btn-toggle');
                if (btn) { btn.innerHTML = '<i class="fas fa-play"></i>'; btn.title = '繼續'; }
            } else if (['queued', 'pending'].includes(task.status)) {
                speedEl.textContent = '等待中...';
                speedEl.style.color = '';
            } else {
                speedEl.style.color = '';
                const speed = this.UIManager.formatBytes(task.speed || 0);
                let eta = '';
                if (task.speed > 0 && (task.size - task.progress) > 0) {
                    const sec = Math.ceil((task.size - task.progress) / task.speed);
                    eta = ` • 剩餘 ${sec > 60 ? Math.ceil(sec / 60) + ' 分' : sec + ' 秒'}`;
                }
                speedEl.textContent = `${speed}/s${eta}`;
                const btn = el.querySelector('.btn-toggle');
                if (btn) { btn.innerHTML = '<i class="fas fa-pause"></i>'; btn.title = '暫停'; }
            }
        }
        const failedMsg = el.querySelector('.failed-msg');
        if (failedMsg && task.message) failedMsg.textContent = task.message;
    },

    toggleTaskState(id) {
        const result = this.findTask(id);
        if (!result) return;
        if (['transferring', 'queued'].includes(result.task.status)) this.pauseTask(id);
        else if (['paused', 'failed'].includes(result.task.status)) this.resumeTask(id);
    },

    _createActiveCard(task) {
        const el = document.createElement('div');
        el.className = 'task-card-lg';
        el.dataset.id = task.id;
        el.dataset.type = task.type;
        const pathInfo = (task.type === 'upload') 
            ? `<i class="fas fa-file-upload"></i> ${task.localPath || '未知路徑'}` 
            : `<i class="fas fa-cloud-download-alt"></i> 下載至 ${task.localPath || '預設路徑'}`;
        el.innerHTML = `
            <div class="card-row-main">
                <div class="file-icon-lg"><i class="${this.UIManager.getFileTypeIcon(task.name)}"></i></div>
                <div class="card-content">
                    <div class="file-title">${task.name}</div>
                    <div class="file-path">${pathInfo}</div>
                    <div class="progress-track"><div class="progress-fill"></div></div>
                    <div class="meta-row"><span class="meta-size"></span><span class="meta-speed"></span></div>
                </div>
                <div class="card-actions">
                    <button class="icon-btn btn-toggle"><i class="fas fa-pause"></i></button>
                    <button class="icon-btn btn-cancel" title="取消"><i class="fas fa-times"></i></button>
                </div>
            </div>`;
        this._bindCardActions(el, task);
        this.renderTaskCard(task, el);
        return el;
    },

    _createFailedCard(task) {
        const el = document.createElement('div');
        el.className = 'task-card-failed';
        el.dataset.id = task.id;
        el.dataset.type = task.type;
        el.innerHTML = `
            <div style="display:flex; align-items:center; gap:15px;">
                <div style="color:var(--danger-color); font-size:20px;"><i class="fas fa-exclamation-circle"></i></div>
                <div class="failed-info"><span style="font-weight:600; font-size:14px;">${task.name}</span><span class="failed-msg">${task.message || '未知錯誤'}</span></div>
            </div>
            <div class="card-actions">
                <button class="icon-btn btn-retry" style="color:var(--primary-color);" title="重試"><i class="fas fa-redo"></i></button>
                <button class="icon-btn btn-cancel" title="取消"><i class="fas fa-times"></i></button>
            </div>`;
        this._bindCardActions(el, task);
        this.renderTaskCard(task, el);
        return el;
    },

    _createQueuedCard(task) {
        const el = document.createElement('div');
        el.className = 'task-card-queued';
        el.dataset.id = task.id;
        el.dataset.type = task.type;
        el.innerHTML = `
            <div class="drag-handle"><i class="fas fa-grip-vertical"></i></div>
            <div class="queued-content"><div class="queued-name">${task.name}</div><div class="queued-size">${this.UIManager.formatBytes(task.size)}</div></div>
            <button class="icon-btn btn-cancel" title="取消"><i class="fas fa-times"></i></button>`;
        this._bindCardActions(el, task);
        this.renderTaskCard(task, el);
        return el;
    },

    _bindCardActions(el, task) {
        el.querySelector('.btn-cancel')?.addEventListener('click', () => this.cancelItem(task.id));
        el.querySelector('.btn-toggle')?.addEventListener('click', () => this.toggleTaskState(task.id));
        el.querySelector('.btn-retry')?.addEventListener('click', () => this.resumeTask(task.id));
    },

    _getCloudPath(folderId) {
        if (!this.AppState || !this.AppState.folderMap) return '路徑不存在';
        
        const path = [];
        let current = this.AppState.folderMap.get(folderId);

        // 情況 1: 起始 ID 就不存在
        if (!current) return '路徑不存在';

        while (current) {
            path.unshift(current.name);
            
            // 成功到達根目錄 (parent_id 為 null)
            if (current.parent_id === null) {
                return path.join(' / ');
            }

            // 向上移動
            const next = this.AppState.folderMap.get(current.parent_id);
            
            // 情況 2: 追溯鏈條斷裂 (找不到父資料夾)
            if (!next) return '路徑不存在';
            
            current = next;
        }

        return '路徑不存在';
    },

    _renderCompletedList() {
        const listEl = document.getElementById('list-completed');
        if (!listEl) return; 
        listEl.innerHTML = ''; 
        let candidates = this.completedFilter === 'all' ? [...this.uploads.values(), ...this.downloads.values()] : (this.completedFilter === 'upload' ? [...this.uploads.values()] : [...this.downloads.values()]);
        const allCompleted = candidates.filter(t => t.status === 'completed');
        const { key, order } = this.completedSort;
        allCompleted.sort((a, b) => {
            if (key === 'time') {
                const valA = a.completedAt || 0;
                const valB = b.completedAt || 0;
                return order === 'asc' ? valA - valB : valB - valA;
            } else {
                // Name sort: Case-insensitive and natural numeric sorting
                return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }) * (order === 'asc' ? 1 : -1);
            }
        });
        allCompleted.forEach(task => {
            const row = document.createElement('div');
            row.className = 'history-item';
            const isUp = this.uploads.has(task.id);
            row.dataset.type = isUp ? 'upload' : 'download';
            const cloudPath = isUp ? this._getCloudPath(task.parentFolderId) : '';
            
            // Validity Check (Synchronous for Uploads)
            let isValid = true;
            if (isUp) {
                isValid = this.AppState.folderMap.has(task.parentFolderId);
                if (!isValid) row.classList.add('item-invalid');
            }

            row.innerHTML = `
                <div class="sm-icon"><i class="${this.UIManager.getFileTypeIcon(task.name)}"></i></div>
                <div class="sm-name">
                    ${task.name}
                    <div style="font-size:12px; color:#9ca3af; margin-top:2px;">
                        ${isUp ? `上傳至: ${cloudPath}` : `下載至: ${task.localPath || '預設路徑'}`}
                    </div>
                </div>
                <div class="sm-badge">${isUp ? '上傳成功' : '下載成功'}</div>
                <div class="history-actions">
                    ${isUp 
                        ? `<button class="icon-btn btn-go-cloud" title="${isValid ? '前往雲端位置' : '資料夾已不存在'}"><i class="fas fa-external-link-alt"></i></button>`
                        : `<button class="icon-btn btn-reveal-local" title="在檔案總管中顯示"><i class="fas fa-folder-open"></i></button>`
                    }
                </div>`;
            
            listEl.appendChild(row);

            if (isUp) {
                if (isValid) {
                    row.querySelector('.btn-go-cloud').onclick = () => {
                        if (window.switchPage) window.switchPage('files');
                        if (window.navigateTo) window.navigateTo(task.parentFolderId);
                    };
                }
            } else {
                const btnReveal = row.querySelector('.btn-reveal-local');
                btnReveal.onclick = () => {
                    this.ApiService.showItemInFolder(task.localPath);
                };
                
                // Validity Check (Asynchronous for Downloads)
                this.ApiService.checkLocalExists(task.localPath).then(exists => {
                    if (exists === false) {
                        row.classList.add('item-invalid');
                        btnReveal.title = "檔案已移除";
                    }
                });
            }
        });
        this._completedListDirty = false;
    },
    
    sortCompleted(key) {
        if (!['time', 'name'].includes(key)) return;
        if (this.completedSort.key === key) this.completedSort.order = (this.completedSort.order === 'asc') ? 'desc' : 'asc';
        else { this.completedSort.key = key; this.completedSort.order = 'desc'; }
        
        this._completedListDirty = true;

        document.querySelectorAll('.sort-item').forEach(el => {
            el.classList.toggle('active', el.dataset.sort === key);
            const icon = el.querySelector('i');
            if (icon) icon.className = el.dataset.sort === key ? (this.completedSort.order === 'asc' ? 'fas fa-sort-up' : 'fas fa-sort-down') : 'fas fa-sort';
        });
        this._renderCompletedList();
    },

    updateMainFileListUI() {
        document.querySelectorAll('.file-item:not(.is-uploading)').forEach(el => {
            const name = el.dataset.name;
            const task = [...this.uploads.values(), ...this.downloads.values()].find(t => t.name === name && t.parentFolderId === this.AppState.currentFolderId);
            el.classList.toggle('in-transfer', !!(task && ['transferring', 'paused', 'queued'].includes(task.status)));
        });
    },
    
    showFileFeedback(name, status) {
        const el = document.querySelector(`.file-item[data-name="${CSS.escape(name)}"]`);
        if (!el) return;
        const flashClass = status === 'completed' ? 'flash-success' : 'flash-fail';
        el.classList.add(flashClass);
        setTimeout(() => el.classList.remove(flashClass), 1000);
    },
    
    pauseTask(id) {
        const result = this.findTask(id);
        if (result && ['transferring', 'queued'].includes(result.task.status)) {
            result.task.status = 'paused';
            this.renderTaskCard(result.task);
            this.ApiService.pauseTransfer(id);
        }
    },

    resumeTask(id) {
        const result = this.findTask(id);
        if (result && ['paused', 'failed'].includes(result.task.status)) {
            result.task.status = 'queued';
            this.renderTaskCard(result.task);
            this.ApiService.resumeTransfer(id);
        }
    },

    cancelItem(id) {
        const result = this.findTask(id);
        if (result) {
            this.ApiService.cancelTransfer(id);
            result.map.delete(id);
            this.tick();
        }
    },

    cancelAll() {
        this.uploads.forEach(t => this.cancelItem(t.id));
        this.downloads.forEach(t => this.cancelItem(t.id));
    },

    resumeAll() {
        [this.uploads, this.downloads].forEach(map => map.forEach(t => { if(['paused', 'failed'].includes(t.status)) this.resumeTask(t.id); }));
    },

    pauseAll() {
        [this.uploads, this.downloads].forEach(map => map.forEach(t => { if(['transferring', 'queued'].includes(t.status)) this.pauseTask(t.id); }));
    },
    
    findTask(id) {
        if (this.uploads.has(id)) return { task: this.uploads.get(id), map: this.uploads };
        if (this.downloads.has(id)) return { task: this.downloads.get(id), map: this.downloads };
        return null;
    },

    clearCompleted() {
        [this.uploads, this.downloads].forEach(map => { for (let [k, t] of map.entries()) if (['completed', 'failed', 'cancelled'].includes(t.status)) map.delete(k); });
        this.tick();
        if (this.currentTab === 'completed') this._renderCompletedList();
    },

    setDownloadDestination(path) { this.currentDownloadDestination = path; },
    
    setupEventListeners() {
        const sidebarStatus = document.getElementById('sidebar-transfer-status');
        if (sidebarStatus) sidebarStatus.addEventListener('click', () => { if(window.switchPage) window.switchPage('transfer'); });
        document.querySelectorAll('.tabs-container .tab-item').forEach(btn => btn.addEventListener('click', () => {
            document.querySelector('.tabs-container .tab-item.active').classList.remove('active');
            btn.classList.add('active');
            this.currentTab = btn.dataset.tab;
            if (this.currentTab === 'completed') this._completedListDirty = true;
            this.renderDashboard();
        }));
        document.querySelectorAll('.filter-segment').forEach(btn => btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-segment').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            this.completedFilter = btn.dataset.filter;
            this._completedListDirty = true;
            this._renderCompletedList();
        }));
        document.querySelectorAll('.sort-item').forEach(btn => btn.addEventListener('click', () => this.sortCompleted(btn.dataset.sort)));
        
        document.getElementById('btn-clear-completed')?.addEventListener('click', () => this.clearCompleted());

        document.getElementById('global-cancel-btn')?.addEventListener('click', () => this.cancelAll());
        document.getElementById('global-pause-btn')?.addEventListener('click', () => this.pauseAll());
        document.getElementById('retry-all-btn')?.addEventListener('click', () => this.resumeAll());
    }
};
