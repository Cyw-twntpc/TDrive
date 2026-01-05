const TransferManager = {
    uploads: new Map(),
    downloads: new Map(),
    uploadHistory: new Map(),
    downloadHistory: new Map(),
    updateInterval: null,
    currentDownloadDestination: '',
    AppState: null,
    ApiService: null,
    UIManager: null,
    refreshCallback: null,
    
    currentTab: 'uploads', 
    completedSort: { key: 'time', order: 'desc' },
    completedFilter: 'all',
    _completedListDirty: true,
    _validityCheckInterval: null,
    _showCompletedState: false,
    chunkSize: 33554432, 
    
    initialize(AppState, ApiService, UIManager, refreshCallback) {
        this.AppState = AppState;
        this.ApiService = ApiService;
        this.UIManager = UIManager;
        this.refreshCallback = refreshCallback;
        this.setupEventListeners();
        
        if (window.tdrive_bridge && window.tdrive_bridge.transfer_progress_updated) {
            window.tdrive_bridge.transfer_progress_updated.connect(this.updateTask.bind(this));
        }
        if (window.tdrive_bridge && window.tdrive_bridge.file_status_changed) {
            window.tdrive_bridge.file_status_changed.connect(this.updateFileExistence.bind(this));
        }

        if (this.ApiService.getInitialStats) {
            this.ApiService.getInitialStats().then(data => {
                if (data) {
                    if (data.todayTraffic !== undefined) {
                        const trafficEl = document.getElementById('hero-daily-traffic');
                        if (trafficEl) trafficEl.textContent = this.UIManager.formatBytes(data.todayTraffic);
                    }
                    if (data.chunkSize) {
                        this.chunkSize = data.chunkSize;
                    }
                }
                
                this.ApiService.getIncompleteTransfers().then(stateData => {
                    if (stateData) {
                        this.restoreTasks(stateData.uploads, 'upload');
                        this.restoreTasks(stateData.downloads, 'download');
                        this.updateAllUI();

                        this.ApiService.getAllFileStatuses().then(statuses => {
                            if (statuses) {
                                const changes = Object.entries(statuses).map(([id, exists]) => ({ id, exists }));
                                this.updateFileExistence(changes);
                            }
                        });
                    }
                });
            });
        } else {
            this.ApiService.getIncompleteTransfers().then(stateData => {
                if (stateData) {
                    this.restoreTasks(stateData.uploads, 'upload');
                    this.restoreTasks(stateData.downloads, 'download');
                    this.updateAllUI();
                }
            });
        }
    },

    restoreTasks(taskMap, type) {
        if (!taskMap) return;

        for (const [taskId, info] of Object.entries(taskMap)) {
            const calculateProgress = (taskInfo) => {
                let p = 0;
                if (taskInfo.transferred_parts && Array.isArray(taskInfo.transferred_parts)) {
                    p = taskInfo.transferred_parts.length * this.chunkSize;
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
                type: type,
                targetExists: true 
            };

            if (isFolder && type === 'download' && !task.name) {
                task.name = info.save_path ? info.save_path.split(/[\\/]/).pop() : 'Unknown Folder';
            }

            if (task.status === 'completed') {
                if (type === 'upload') this.uploadHistory.set(taskId, task);
                else this.downloadHistory.set(taskId, task);
            } else {
                if (type === 'upload') this.uploads.set(taskId, task);
                else this.downloads.set(taskId, task);
            }
        }
    },

    addDownload(item) {
        this._showCompletedState = false;
        if (this.downloads.has(item.task_id)) return;
        const task = { 
            id: item.task_id, db_id: item.db_id, name: item.name, size: item.size || 0, 
            progress: 0, status: 'queued', localPath: item.save_path || this.currentDownloadDestination, 
            parentFolderId: this.AppState.currentFolderId, feedbackShown: false, 
            alertShown: false, startTime: Date.now(), completedAt: null,
            type: 'download',
            targetExists: true
        };
        this.downloads.set(item.task_id, task);
        this.startUpdater();
    },

    addUpload(fileData) {
        this._showCompletedState = false;
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
        if (data.parent_id) return; 

        let task = this.downloads.get(data.id) || this.uploads.get(data.id);

        if (!task) return;

        if (data.status === 'completed' && !task.completedAt) task.completedAt = Date.now();

        if (data.todayTraffic !== undefined) {
            const trafficEl = document.getElementById('hero-daily-traffic');
            if (trafficEl) trafficEl.textContent = this.UIManager.formatBytes(data.todayTraffic);
        }

        // Fix: Prevent progress reset on pause
        if (data.status !== 'paused') {
            if (data.delta !== undefined && data.delta !== null) {
                task.progress += data.delta;
                if (task.size > 0 && task.progress > task.size) task.progress = task.size;
            } else if (data.transferred !== undefined) {
                task.progress = data.transferred;
            }
            if (data.total !== undefined && data.total > 0) task.size = data.total;
        }

        let statusChanged = false;
        if (data.status) {
            const newStatus = data.status;
            
            if (task.status !== newStatus) {
                task.status = newStatus;
                statusChanged = true;
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
        this.checkAndArchive(); 

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

    checkAndArchive() {
        const activeUploads = [...this.uploads.values()].filter(t => ['queued', 'transferring', 'paused'].includes(t.status));
        const activeDownloads = [...this.downloads.values()].filter(t => ['queued', 'transferring', 'paused'].includes(t.status));
        
        if (activeUploads.length === 0 && activeDownloads.length === 0) {
            let moved = false;
            
            for (const [id, task] of this.uploads.entries()) {
                if (task.status === 'completed' && task.feedbackShown) {
                    this.uploadHistory.set(id, task);
                    this.uploads.delete(id);
                    moved = true;
                }
            }
            
            for (const [id, task] of this.downloads.entries()) {
                if (task.status === 'completed' && task.feedbackShown) {
                    this.downloadHistory.set(id, task);
                    this.downloads.delete(id);
                    moved = true;
                }
            }
            
            if (moved) {
                this._showCompletedState = true;
                this._completedListDirty = true;
                if (this.AppState.currentPage === 'transfer' && this.currentTab === 'completed') {
                    this._renderCompletedList();
                }
            }
        }
    },

    updateAllUI() {
        this.updateSummaryPanel();
        if (this.AppState.currentPage === 'transfer') this.renderDashboard();
        this.updateMainFileListUI();
    },

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
            if (task.status === 'failed' || task.status === 'cancelled') {
                if (task.status === 'failed') failedCount++;
                return;
            }

            totalSize += task.size;
            const currentProgress = (task.status === 'completed') ? task.size : task.progress;
            totalProgress += currentProgress;
            
            if (['transferring', 'queued'].includes(task.status)) {
                activeCount++;
                currentSpeed += (task.speed || 0);
                if (this.uploads.has(task.id)) activeUploads++;
                else activeDownloads++;
            }
        });

        panel.classList.remove('status-completed', 'transfer-active', 'upload-active', 'download-active', 'mixed-active');
        
        if (activeCount > 0) {
            panel.classList.add('transfer-active');
            if (activeUploads > 0 && activeDownloads === 0) panel.classList.add('upload-active');
            else if (activeDownloads > 0 && activeUploads === 0) panel.classList.add('download-active');
            else panel.classList.add('mixed-active');

            titleEl.textContent = `正在傳輸 ${activeCount} 個項目`;
            speedEl.textContent = (currentSpeed > 0) ? `${this.UIManager.formatBytes(currentSpeed)}/s` : '-- B/s';
        } else if (allTasks.some(t => t.status === 'paused')) {
            panel.classList.add('transfer-active');
            titleEl.textContent = '傳輸已暫停';
            speedEl.textContent = '-- B/s';
        } else if ((totalSize > 0 && totalProgress >= totalSize) || (activeCount === 0 && this._showCompletedState)) {
            panel.classList.add('status-completed');
            titleEl.textContent = '傳輸完成';
            speedEl.textContent = '-- B/s'; 
        } else {
            this.setPanelToReadyState();
            return;
        }

        let scale = 0;
        if (totalSize > 0) {
            scale = totalProgress / totalSize;
        } else if (activeCount === 0 && this._showCompletedState) {
            scale = 1;
        }
        barEl.style.transform = `scaleX(${scale})`;
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
            allTasks.forEach(t => { 
                if(['transferring', 'queued'].includes(t.status)) {
                    totalRemaining += (t.size - t.progress); 
                }
            });
            if (currentSpeed > 0 && totalRemaining > 0) {
                const seconds = Math.ceil(totalRemaining / currentSpeed);
                etaEl.textContent = seconds > 86400 ? '> 1 天' : new Date(seconds * 1000).toISOString().substr(11, 8);
            } else etaEl.textContent = '--:--:--';
        }
    },

    renderDashboard() {
        const container = document.getElementById('page-transfer');
        if (!container || container.classList.contains('hidden')) {
            this.stopValidityChecker();
            return;
        }

        const liveView = document.getElementById('transfer-live-view');
        const completedView = document.getElementById('transfer-completed-view');
        
        if (this.currentTab === 'completed') {
            liveView.classList.add('hidden');
            completedView.classList.remove('hidden');
            
            if (this._completedListDirty) {
                this._renderCompletedList();
            } else {
                this._refreshHistoryPathLabels();
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
            } else if (['transferring', 'paused'].includes(task.status)) {
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
        const activeCount = [...this.uploads.values(), ...this.downloads.values()].filter(t => !['completed', 'cancelled'].includes(t.status)).length;
        if (activeCount === 0) {
             this.renderDashboard();
        }
    },

    _renderSection(type, tasks, createCardFn) {
        const listEl = document.getElementById(`list-${type}`);
        if (!listEl) return; 

        if (tasks.length > 0) {
            const emptyState = listEl.querySelector('.empty-state');
            if (emptyState) emptyState.remove();
        }

        const existingCards = new Map();
        listEl.querySelectorAll('[data-id]').forEach(el => existingCards.set(el.dataset.id, el));
        const toRemove = new Set(existingCards.keys());

        tasks.forEach(task => {
            if (existingCards.has(task.id)) {
                this.renderTaskCard(task, existingCards.get(task.id));
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

        const btn = el.querySelector('.btn-toggle');
        const speedEl = el.querySelector('.meta-speed');
        
        if (speedEl) {
            if (task.status === 'paused') {
                speedEl.textContent = '已暫停';
                speedEl.style.color = '#f59e0b';
                if (btn && btn.dataset.lastStatus !== 'paused') {
                    btn.innerHTML = '<i class="fas fa-play"></i>';
                    btn.title = '繼續';
                    btn.dataset.lastStatus = 'paused';
                }
            } else if (task.status === 'failed') {
                speedEl.textContent = '傳輸失敗';
                speedEl.style.color = 'var(--danger-color)';
                if (btn && btn.dataset.lastStatus !== 'failed') {
                    btn.innerHTML = '<i class="fas fa-redo"></i>';
                    btn.title = '重試';
                    btn.dataset.lastStatus = 'failed';
                }
            } else if (['queued', 'pending'].includes(task.status)) {
                speedEl.textContent = '等待中...';
                speedEl.style.color = '';
                if (btn && btn.dataset.lastStatus !== 'queued') {
                    btn.innerHTML = '<i class="fas fa-pause"></i>'; 
                    btn.title = '暫停';
                    btn.dataset.lastStatus = 'queued';
                }
            } else {
                speedEl.style.color = '';
                const speed = this.UIManager.formatBytes(task.speed || 0);
                let eta = '';
                if (task.speed > 0 && (task.size - task.progress) > 0) {
                    const sec = Math.ceil((task.size - task.progress) / task.speed);
                    eta = ` • 剩餘 ${sec > 60 ? Math.ceil(sec / 60) + ' 分' : sec + ' 秒'}`;
                }
                speedEl.textContent = `${speed}/s${eta}`;
                
                if (btn && btn.dataset.lastStatus !== 'transferring') {
                    btn.innerHTML = '<i class="fas fa-pause"></i>';
                    btn.title = '暫停';
                    btn.dataset.lastStatus = 'transferring';
                }
            }
        }
        const failedMsg = el.querySelector('.failed-msg');
        if (failedMsg && task.message) failedMsg.textContent = task.message;
    },

    toggleTaskState(id) {
        const result = this.findTask(id);
        if (!result) return;
        
        if (['paused', 'failed'].includes(result.task.status)) {
            this.resumeTask(id);
        } else {
            this.pauseTask(id);
        }
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

        if (!current) return '路徑不存在';

        while (current) {
            path.unshift(current.name);
            
            if (current.parent_id === null) {
                return path.join(' / ');
            }

            const next = this.AppState.folderMap.get(current.parent_id);
            
            if (!next) return '路徑不存在';
            
            current = next;
        }

        return '路徑不存在';
    },

    _renderCompletedList() {
        const listEl = document.getElementById('list-completed');
        if (!listEl) return; 
        listEl.innerHTML = ''; 

        let sourceTasks = [];
        if (this.completedFilter === 'all') {
            sourceTasks = [
                ...this.uploadHistory.values(), ...this.downloadHistory.values(),
                ...[...this.uploads.values()].filter(t => t.status === 'completed'),
                ...[...this.downloads.values()].filter(t => t.status === 'completed')
            ];
        } else if (this.completedFilter === 'upload') {
            sourceTasks = [
                ...this.uploadHistory.values(),
                ...[...this.uploads.values()].filter(t => t.status === 'completed')
            ];
        } else {
            sourceTasks = [
                ...this.downloadHistory.values(),
                ...[...this.downloads.values()].filter(t => t.status === 'completed')
            ];
        }

        const allCompleted = sourceTasks;
        const { key, order } = this.completedSort;
        allCompleted.sort((a, b) => {
            if (key === 'time') {
                const valA = a.completedAt || 0;
                const valB = b.completedAt || 0;
                return order === 'asc' ? valA - valB : valB - valA;
            } else {
                return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }) * (order === 'asc' ? 1 : -1);
            }
        });
        allCompleted.forEach(task => {
            const row = document.createElement('div');
            row.className = 'history-item';
            row.dataset.id = task.id;
            const isUp = task.type === 'upload';
            row.dataset.type = isUp ? 'upload' : 'download';
            
            const cloudPath = isUp ? this._getCloudPath(task.parentFolderId) : '';
            
            const isValid = task.targetExists !== false;
            const itemClass = isValid ? 'history-item' : 'history-item item-invalid';
            
            let btnTitle;
            if (isUp) btnTitle = isValid ? '前往雲端位置' : '資料夾已不存在';
            else btnTitle = isValid ? '在檔案總管中顯示' : '檔案已移除';

            row.className = itemClass;
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
                        ? `<button class="icon-btn btn-go-cloud" title="${btnTitle}"><i class="fas fa-external-link-alt"></i></button>`
                        : `<button class="icon-btn btn-reveal-local" title="${btnTitle}"><i class="fas fa-folder-open"></i></button>`
                    }
                </div>
                <button class="btn-remove-history" title="移除此紀錄"><i class="fas fa-trash-alt"></i></button>`;
            
            listEl.appendChild(row);

            row.querySelector('.btn-remove-history').onclick = (e) => {
                e.stopPropagation();
                this.removeSingleHistoryItem(task.id, task.type);
            };

            if (isUp) {
                row.querySelector('.btn-go-cloud').onclick = () => {
                    if (window.switchPage) window.switchPage('files');
                    if (window.navigateTo) window.navigateTo(task.parentFolderId);
                };
            } else {
                const btnReveal = row.querySelector('.btn-reveal-local');
                btnReveal.onclick = () => {
                    this.ApiService.showItemInFolder(task.localPath);
                };
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
        if (!result) return;
        this.ApiService.pauseTransfer(id);
    },

    resumeTask(id) {
        const result = this.findTask(id);
        if (!result) return;
        this.ApiService.resumeTransfer(id);
    },

    cancelItem(id) {
        const result = this.findTask(id);
        if (result) {
            this.ApiService.cancelTransfer(id);
            result.map.delete(id);
            
            if (this.AppState && this.AppState.currentFolderContents) {
                let removed = false;
                const fileIndex = this.AppState.currentFolderContents.files.findIndex(f => f.id === id);
                if (fileIndex > -1) {
                    this.AppState.currentFolderContents.files.splice(fileIndex, 1);
                    removed = true;
                }
                const folderIndex = this.AppState.currentFolderContents.folders.findIndex(f => f.id === id);
                if (folderIndex > -1) {
                    this.AppState.currentFolderContents.folders.splice(folderIndex, 1);
                    removed = true;
                }
                
                if (removed && typeof FileListHandler !== 'undefined') {
                    FileListHandler.sortAndRender(this.AppState);
                }
            }

            this.tick();
        }
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
        const idsToRemove = [];
        
        const cleanMap = (map) => {
            for (let [k, t] of map.entries()) {
                if (t.status === 'completed') {
                    idsToRemove.push(k);
                    map.delete(k);
                }
            }
        };

        cleanMap(this.uploads);
        cleanMap(this.downloads);
        cleanMap(this.uploadHistory);
        cleanMap(this.downloadHistory);

        idsToRemove.forEach(id => this.ApiService.removeTransferHistory(id));

        this.tick();
        if (this.currentTab === 'completed') this._renderCompletedList();
    },

    removeSingleHistoryItem(taskId, type) {
        if (type === 'upload') {
            this.uploadHistory.delete(taskId);
            this.uploads.delete(taskId); 
        } else {
            this.downloadHistory.delete(taskId);
            this.downloads.delete(taskId);
        }
        
        this.ApiService.removeTransferHistory(taskId);
        
        if (this.currentTab === 'completed') this._renderCompletedList();
        
        this.updateSummaryPanel();
    },

    setDownloadDestination(path) { this.currentDownloadDestination = path; },

    updateFileExistence(changes) {
        console.log("[TransferManager] Received existence changes:", changes);
        if (!Array.isArray(changes)) return;
        
        changes.forEach(change => {
            const result = this.findTask(change.id);
            const task = result ? result.task : (this.uploadHistory.get(change.id) || this.downloadHistory.get(change.id));
            
            if (task) {
                if (task.targetExists !== change.exists) {
                    task.targetExists = change.exists;
                    
                    const el = document.querySelector(`.history-item[data-id="${change.id}"]`);
                    if (el) {
                        const isUp = task.type === 'upload';
                        const isValid = change.exists;
                        
                        if (isValid) {
                            el.classList.remove('item-invalid');
                        } else {
                            el.classList.add('item-invalid');
                        }

                        let btnTitle;
                        if (isUp) btnTitle = isValid ? '前往雲端位置' : '資料夾已不存在';
                        else btnTitle = isValid ? '在檔案總管中顯示' : '檔案已移除';

                        const btn = isUp ? el.querySelector('.btn-go-cloud') : el.querySelector('.btn-reveal-local');
                        if (btn) btn.title = btnTitle;

                        if (isUp) {
                            const pathEl = el.querySelector('.sm-name div');
                            if (pathEl) {
                                pathEl.textContent = `上傳至: ${this._getCloudPath(task.parentFolderId)}`;
                            }
                        }
                    }
                }
            }
        });
    },

    _refreshHistoryPathLabels() {
        const listEl = document.getElementById('list-completed');
        if (!listEl) return;

        const items = listEl.querySelectorAll('.history-item[data-type="upload"]');
        items.forEach(el => {
            const taskId = el.dataset.id;
            const task = this.uploads.get(taskId) || this.uploadHistory.get(taskId);
            if (task && task.parentFolderId) {
                const pathEl = el.querySelector('.sm-name div');
                if (pathEl) {
                    pathEl.textContent = `上傳至: ${this._getCloudPath(task.parentFolderId)}`;
                }
            }
        });
    },
    
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
