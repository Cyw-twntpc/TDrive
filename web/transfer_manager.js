const TransferManager = {
    uploads: new Map(),
    downloads: new Map(), // Top-level tasks (files or folders)
    updateInterval: null,
    minimizeTimeout: null,
    isPanelMinimized: false,
    hadActiveTransfers: false,
    concurrencyLimit: 3,
    currentDownloadDestination: '',
    AppState: null,
    eel: null,
    UIHandler: null,
    refreshCallback: null,

    // --- 初始化 ---
    initialize(AppState, eel, UIHandler, refreshCallback) {
        this.AppState = AppState;
        this.eel = eel;
        this.UIHandler = UIHandler;
        this.refreshCallback = refreshCallback;
        this.setupEventListeners();
        this.eel.expose(this.updateTask.bind(this), 'update_transfer_progress');
    },

    // --- 任務管理 (支援樹狀結構) ---
    addDownload(item) {
        if (this.downloads.has(item.id)) return;
        const task = { 
            id: item.id, name: item.name, size: item.size || 0, progress: 0, speed: 0, status: 'queued',
            isFolder: item.type === 'folder',
            children: item.type === 'folder' ? new Map() : null,
            total_files: item.type === 'folder' ? 0 : 1,
            completed_files: 0,
            expanded: true, feedbackShown: false, alertShown: false, itemData: item
        };
        this.downloads.set(item.id, task);
        this.startUpdater();
    },

    addUpload(fileData) {
        if (this.uploads.has(fileData.name)) return;
        const id = `ul_${Date.now()}_${Math.random()}`;
        const task = { 
            id, name: fileData.name, size: fileData.size || 0, progress: 0, speed: 0, status: 'queued', isFolder: false,
            localPath: fileData.localPath, parentFolderId: fileData.parentFolderId,
            feedbackShown: false, alertShown: false 
        };
        this.uploads.set(fileData.name, task);
        this.startUpdater();
    },
    
    updateTask(data) {
        console.log("Received data in updateTask:", data); // 除錯日誌
        let task;
        let parentTask = null;

        if (data.parent_id) {
            parentTask = this.downloads.get(data.parent_id);
            if (parentTask && parentTask.children) {
                task = parentTask.children.get(data.id);
            }
        } else {
            task = this.downloads.get(data.id) || Array.from(this.uploads.values()).find(t => t.id === data.id || (t.name === data.name && t.status === 'queued'));
        }

        if (!task && parentTask) {
             task = { id: data.id, name: data.name, size: data.size, progress: 0, status: 'queued' };
             parentTask.children.set(data.id, task);
        } else if (!task) {
            console.warn(`updateTask: 找不到對應的任務，資料:`, data);
            return;
        }
        
        if (task.id !== data.id && !data.parent_id && data.id.startsWith('ul_')) {
             const oldId = task.id;
             this.uploads.delete(task.name);
             task.id = data.id;
             this.uploads.set(task.name, task);
        }


        if (data.status === 'starting_folder' && task.isFolder) {
            Object.assign(task, data);
            task.children = new Map();
            (data.children || []).forEach(childInfo => {
                task.children.set(childInfo.id, { ...childInfo, status: 'queued', progress: 0, alertShown: false });
            });
        } else {
            Object.assign(task, data);
        }

        if (parentTask) {
            this.updateParentProgress(parentTask);
        }

        if (task.status === 'failed' && !task.alertShown) {
            const typeText = task.isFolder ? '資料夾' : '檔案';
            this.UIHandler.showAlert(`${typeText}傳輸失敗`, `項目 "${task.name}" 傳輸失敗。<br><b>原因:</b> ${task.message || '未知錯誤'}`);
            task.alertShown = true;
        }
        this.startUpdater();
    },

    updateParentProgress(parentTask) {
        if (!parentTask || !parentTask.isFolder) return;
        let aggregateProgress = 0, completedFiles = 0, isTransferring = false;
        for (const child of parentTask.children.values()) {
            aggregateProgress += child.progress || 0;
            if (child.status === 'completed') completedFiles++;
            if (child.status === 'transferring') isTransferring = true;
        }
        parentTask.progress = aggregateProgress;
        parentTask.completed_files = completedFiles;

        if (completedFiles === parentTask.total_files && parentTask.total_files > 0) {
            parentTask.status = 'completed';
        } else if (isTransferring) {
            parentTask.status = 'transferring';
        }
    },
    
    startUpdater() {
        if (!this.updateInterval) {
            this.updateInterval = setInterval(() => this.tick(), 1000);
        }
        this.showPanel();
    },
    
    tick() {
        this.updateAllUI();
        const allTransfers = [...this.uploads.values(), ...this.downloads.values()];
        if (allTransfers.length === 0) {
            clearInterval(this.updateInterval);
            this.updateInterval = null;
            this.hidePanel();
        }

        const completedOrFailedTasks = allTransfers.filter(t => (t.status === 'completed' || t.status === 'failed') && !t.feedbackShown);
        if(completedOrFailedTasks.length > 0) {
            this.refreshCallback().then(() => {
                completedOrFailedTasks.forEach(task => {
                    if (!task.isFolder) {
                         this.showFileFeedback(task.name, task.status);
                    }
                    task.feedbackShown = true;
                });
            });
        }
    },

    scheduleMinimize() {
        if (this.minimizeTimeout) clearTimeout(this.minimizeTimeout);
        this.minimizeTimeout = setTimeout(() => {
            const panel = document.getElementById('transfer-summary-panel');
            if (panel && !panel.matches(':hover') && !document.getElementById('transfer-details-modal').matches(':hover')) {
                 panel.classList.add('minimized');
                 this.isPanelMinimized = true;
            }
        }, 3000);
    },

    updateAllUI() {
        this.updateSummaryPanel();
        this.updateDetailsModal();
        this.updateMainFileListUI();
    },

    showPanel() {
        document.getElementById('transfer-summary-panel').classList.remove('hidden');
    },
    
    hidePanel() {
        document.getElementById('transfer-summary-panel').classList.add('hidden');
    },

    updateSummaryPanel() {
        // Implementation for summary panel
    },
    
    showDetailsModal() {
        document.getElementById('transfer-details-modal').classList.remove('hidden');
        this.updateDetailsModal();
    },

    updateDetailsModal() {
        const modal = document.getElementById('transfer-details-modal');
        if (modal.classList.contains('hidden')) return;

        const renderList = (map, element) => {
            element.innerHTML = '';
            if (map.size === 0) {
                element.innerHTML = '<p class="empty-list-msg">沒有任務</p>';
                return;
            }
            map.forEach(task => {
                element.appendChild(this._createTaskElement(task, 0));
                if (task.isFolder && task.expanded && task.children) {
                    task.children.forEach(child => {
                        element.appendChild(this._createTaskElement(child, 1, task.id));
                    });
                }
            });
        };
        
        renderList(this.uploads, document.getElementById('upload-list'));
        renderList(this.downloads, document.getElementById('download-list'));
        document.getElementById('upload-count').textContent = `(${this.uploads.size})`;
        document.getElementById('download-count').textContent = `(${this.downloads.size})`;
    },

    _createTaskElement(item, indentLevel, parentId = null) {
        const row = document.getElementById('transfer-item-template').content.cloneNode(true).firstElementChild;
        row.dataset.id = item.id;
        if (parentId) row.dataset.parentId = parentId;

        row.querySelector('.item-indent').style.width = `${indentLevel * 25}px`;
        const toggle = row.querySelector('.item-toggle');
        if (item.isFolder) {
            toggle.innerHTML = `<i class="fas ${item.expanded ? 'fa-caret-down' : 'fa-caret-right'}"></i>`;
            toggle.style.cursor = 'pointer';
            toggle.addEventListener('click', () => this.toggleFolder(item.id));
        }

        row.querySelector('.file-icon').className = `file-icon ${item.isFolder ? 'fas fa-folder' : this.UIHandler.getFileTypeIcon(item.name)}`;
        row.querySelector('.item-name').textContent = item.name;
        
        const progressFill = row.querySelector('.progress-fill');
        const progressText = row.querySelector('.item-progress-text');
        const itemStatus = row.querySelector('.item-status');
        const itemSpeed = row.querySelector('.item-speed');

        const percent = item.size > 0 ? (item.progress / item.size * 100) : 0;
        progressFill.style.width = `${percent}%`;
        itemSpeed.textContent = item.speed > 0 ? `${this.UIHandler.formatBytes(item.speed)}/s` : '';

        switch (item.status) {
            case 'completed':
                progressFill.style.backgroundColor = 'var(--success-color)';
                progressText.textContent = this.UIHandler.formatBytes(item.size);
                itemStatus.innerHTML = '<i class="fas fa-check-circle"></i>';
                break;
            case 'failed':
                progressFill.style.backgroundColor = 'var(--danger-color)';
                progressText.textContent = item.message || '失敗';
                itemStatus.innerHTML = '<i class="fas fa-exclamation-circle"></i>';
                break;
            case 'transferring':
                progressText.textContent = `${this.UIHandler.formatBytes(item.progress)} / ${this.UIHandler.formatBytes(item.size)} (${percent.toFixed(1)}%)`;
                itemStatus.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                break;
            case 'starting_folder':
                progressText.textContent = `${item.completed_files} / ${item.total_files} 個檔案 (${this.UIHandler.formatBytes(item.size)})`;
                itemStatus.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                break;
            case 'queued':
                progressText.textContent = '佇列中...';
                itemStatus.innerHTML = '<i class="fas fa-clock"></i>';
                break;
            case 'cancelled':
                progressText.textContent = '已取消';
                itemStatus.innerHTML = '<i class="fas fa-times-circle" style="color: var(--danger-color);"></i>';
                break;
            default:
                 progressText.textContent = '...';
                 itemStatus.innerHTML = '<i class="fas fa-question-circle"></i>';
        }
        return row;
    },

    toggleFolder(taskId) {
        const task = this.downloads.get(taskId);
        if (task && task.isFolder) {
            task.expanded = !task.expanded;
            this.updateDetailsModal();
        }
    },
    
    updateMainFileListUI() {
        document.querySelectorAll('.file-item:not(.is-uploading)').forEach(el => {
            const name = el.dataset.name;
            const task = this.uploads.get(name) || this.downloads.get(name);
            el.classList.remove('in-transfer');
            if (el.querySelector('.transfer-overlay-icon')) el.querySelector('.transfer-overlay-icon').remove();
            if(task && ['transferring', 'paused', 'queued'].includes(task.status)) {
                el.classList.add('in-transfer');
            }
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

    findTask(id) {
        for (const map of [this.uploads, this.downloads]) {
            for (const task of map.values()) {
                if (task.id === id) return {task, map};
                if (task.isFolder && task.children) {
                    const childTask = task.children.get(id);
                    if (childTask) return {task: childTask, map: task.children, parent: task};
                }
            }
        }
        return null;
    },
    
    cancelItem(id) {
        const result = this.findTask(id);
        if (result) {
            if (['transferring', 'queued'].includes(result.task.status)) {
                this.eel.cancel_transfer(id)();
            }
            const map = result.parent ? result.parent.children : result.map;
            
            let key;
            if (map === this.uploads) {
                key = result.task.name;
            } else { // Downloads map and its children are keyed by ID
                key = result.task.id;
            }

            if (map.has(key)) {
                map.delete(key);
            } else {
                console.warn(`Cancel failed: key "${key}" not found in map.`);
            }
            this.tick();
        }
    },

    cancelAll() {
        [...this.uploads.values(), ...this.downloads.values()].forEach(task => {
            this.cancelItem(task.id);
            if(task.isFolder && task.children){
                task.children.forEach(child => this.cancelItem(child.id));
            }
        });
    },

    clearCompleted() {
        const filterAndClear = (map) => {
            for (let [key, task] of map.entries()) {
                if (['completed', 'failed', 'cancelled'].includes(task.status)) {
                    map.delete(key);
                } else if (task.isFolder && task.children) {
                    filterAndClear(task.children);
                }
            }
        };
        filterAndClear(this.uploads);
        filterAndClear(this.downloads);
        this.tick();
    },

    setConcurrencyLimit(limit) { this.concurrencyLimit = limit; },
    getConcurrencyLimit() { return this.concurrencyLimit; },
    setDownloadDestination(path) { this.currentDownloadDestination = path; },
    
    setupEventListeners() {
        const panel = document.getElementById('transfer-summary-panel');
        const minimizedIcon = document.getElementById('minimized-transfer-icon');
        minimizedIcon.addEventListener('click', (e) => {
            e.stopPropagation();
            panel.classList.remove('minimized');
            this.isPanelMinimized = false;
        });
        panel.addEventListener('mouseenter', () => clearTimeout(this.minimizeTimeout));
        panel.addEventListener('mouseleave', () => this.scheduleMinimize());
        document.getElementById('show-details-btn').addEventListener('click', () => this.showDetailsModal());
        document.getElementById('close-modal-btn').addEventListener('click', () => document.getElementById('transfer-details-modal').classList.add('hidden'));
        document.getElementById('cancel-all-btn').addEventListener('click', () => this.cancelAll());
        document.querySelectorAll('.modal-tabs .tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelector('.modal-tabs .tab-btn.active').classList.remove('active');
                btn.classList.add('active');
                document.querySelector('.tab-content.active').classList.remove('active');
                document.getElementById(`${btn.dataset.tab}-tab`).classList.add('active');
            });
        });
        document.getElementById('clear-completed-btn').addEventListener('click', () => this.clearCompleted());
    }
};