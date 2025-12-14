/**
 * @fileoverview Manages the entire UI and state for file transfers (uploads and downloads).
 *
 * This object tracks all active, queued, and completed transfers, renders the
 * sidebar status indicator and the detailed transfer modal, and handles user interactions
 * like cancelling tasks. It listens to progress updates from the backend via a
 * QWebChannel signal.
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

    // --- Initialization ---
    initialize(AppState, ApiService, UIManager, refreshCallback) {
        this.AppState = AppState;
        this.ApiService = ApiService;
        this.UIManager = UIManager;
        this.refreshCallback = refreshCallback;
        this.setupEventListeners();
        
        // Connect to the backend signal for progress updates
        if (window.tdrive_bridge && window.tdrive_bridge.transfer_progress_updated) {
            window.tdrive_bridge.transfer_progress_updated.connect(this.updateTask.bind(this));
            console.log("TransferManager connected to 'transfer_progress_updated' signal.");
        }
    },

    // --- Task Management (Tree Structure Support) ---
    addDownload(item) {
        console.log('[DEBUG] TransferManager.addDownload called with:', JSON.parse(JSON.stringify(item)));
        if (this.downloads.has(item.task_id)) {
            console.warn(`[DEBUG] Download task with id ${item.task_id} already exists.`);
            return;
        }
        const task = { 
            id: item.task_id, 
            db_id: item.db_id,
            name: item.name, 
            size: item.size || 0, 
            progress: 0, 
            speed: 0, 
            status: 'queued',
            isFolder: item.type === 'folder',
            parentFolderId: this.AppState.currentFolderId,
            children: item.type === 'folder' ? new Map() : null,
            total_files: item.type === 'folder' ? 0 : 1,
            completed_files: 0,
            expanded: true, 
            feedbackShown: false, 
            alertShown: false, 
            itemData: item
        };
        this.downloads.set(item.task_id, task);
        console.log(`[DEBUG] Added new download task. Current downloads map size: ${this.downloads.size}`);
        this.startUpdater();
    },

    addUpload(fileData) {
        const task_id = fileData.task_id;
        if (this.uploads.has(task_id)) return;

        const task = { 
            id: task_id, 
            name: fileData.name, 
            size: fileData.size || 0, 
            progress: 0, 
            speed: 0, 
            status: 'queued', 
            isFolder: false,
            localPath: fileData.localPath, 
            parentFolderId: fileData.parentFolderId,
            feedbackShown: false, 
            alertShown: false 
        };
        this.uploads.set(task_id, task);
        this.startUpdater();
    },
    
    updateTask(data) {
        // console.log('[DEBUG] updateTask received data from backend:', JSON.parse(JSON.stringify(data)));
        let task;
        let parentTask = null;

        if (data.parent_id) {
            parentTask = this.downloads.get(data.parent_id);
            if (parentTask && parentTask.children) {
                task = parentTask.children.get(data.id);
                if (!task) return;
            } else {
                return;
            }
        } else {
            task = this.downloads.get(data.id) || this.uploads.get(data.id);
        }

        if (!task) {
            // console.error(`[DEBUG] CRITICAL: updateTask could not find a matching task for id: ${data.id}`);
            return;
        }

        if (data.status === 'starting_folder' && task.isFolder) {
            Object.assign(task, data);
            task.children = new Map();

            const folderNodes = new Map();
            folderNodes.set('', task);

            const sortedChildren = (data.children || []).sort((a, b) => a.relative_path.length - b.relative_path.length);

            sortedChildren.forEach(childInfo => {
                const pathParts = childInfo.relative_path.replace(/\\/g, '/').split('/');
                const parentPath = pathParts.join('/');

                const parentNode = folderNodes.get(parentPath);
                if (!parentNode) {
                    console.error(`Could not find parent node for path: ${parentPath}`);
                    return;
                }
                
                const isFolder = childInfo.type === 'folder';
                const newNode = {
                    ...childInfo,
                    isFolder,
                    status: isFolder ? 'pending' : 'queued',
                    progress: 0,
                    size: childInfo.size || 0,
                    children: isFolder ? new Map() : null,
                    expanded: true,
                    alertShown: false
                };
                
                parentNode.children.set(childInfo.id, newNode);

                if (isFolder) {
                    folderNodes.set(childInfo.relative_path, newNode);
                }
            });
        } else {
            Object.assign(task, data);
        }

        if (parentTask) {
            this.updateParentProgress(parentTask);
        }

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
        if (!parentTask || !parentTask.isFolder) return;

        const _calculateFolderStats = (folderNode) => {
            let totalProgress = 0;
            let completedFiles = 0;
            let isTransferring = false;
            let hasFailures = false;
            let totalFiles = 0;

            for (const child of folderNode.children.values()) {
                if (child.isFolder) {
                    const stats = _calculateFolderStats(child);
                    totalProgress += stats.totalProgress;
                    completedFiles += stats.completedFiles;
                    if (stats.isTransferring) isTransferring = true;
                    if (stats.hasFailures) hasFailures = true;
                    totalFiles += stats.totalFiles;
                } else {
                    totalFiles++;
                    totalProgress += child.progress || 0;
                    if (child.status === 'completed') {
                        completedFiles++;
                    } else if (child.status === 'failed' || child.status === 'cancelled') {
                        hasFailures = true;
                    } else if (child.status === 'transferring' || child.status === 'queued') {
                        isTransferring = true;
                    }
                }
            }
            return { totalProgress, completedFiles, isTransferring, hasFailures, totalFiles };
        };

        const stats = _calculateFolderStats(parentTask);
        
        parentTask.progress = stats.totalProgress;
        parentTask.completed_files = stats.completedFiles;

        if (stats.hasFailures) {
            parentTask.status = 'failed';
        } else if (stats.completedFiles === parentTask.total_files && parentTask.total_files > 0) {
            parentTask.status = 'completed';
        } else if (stats.isTransferring) {
            parentTask.status = 'transferring';
        }
    },
    
    startUpdater() {
        if (!this.updateInterval) {
            this.updateInterval = setInterval(() => this.tick(), 25);
        }
        // Decoupled: UI updates are now only triggered by the interval (tick), not by every data signal.
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
                    if (!task.isFolder) {
                         this.showFileFeedback(task.name, task.status);
                    }
                    task.feedbackShown = true;
                });
            });
        }
    },

    updateAllUI() {
        this.updateSummaryPanel();
        this.updateDetailsModal();
        this.updateMainFileListUI();
    },

    setPanelToReadyState() {
        const panel = document.getElementById('sidebar-transfer-status');
        const titleEl = document.getElementById('sidebar-transfer-title');
        const speedEl = document.getElementById('sidebar-transfer-speed');
        const barEl = document.getElementById('sidebar-transfer-bar');
        
        panel.classList.remove('status-completed', 'status-failed', 'transfer-active', 'upload-active', 'download-active', 'mixed-active');
        
        titleEl.innerHTML = '&nbsp;'; // Use space to maintain height
        speedEl.textContent = '-- B/s';
        barEl.style.transform = 'scaleX(0)';
    },
    
    updateSummaryPanel() {
        const panel = document.getElementById('sidebar-transfer-status');
        const titleEl = document.getElementById('sidebar-transfer-title');
        const speedEl = document.getElementById('sidebar-transfer-speed');
        const barEl = document.getElementById('sidebar-transfer-bar');
        
        let totalSize = 0;
        let totalProgress = 0;
        let currentSpeed = 0;
        let activeCount = 0;
        let failedCount = 0;
        
        let activeUploads = 0;
        let activeDownloads = 0;

        const allTasks = [...this.uploads.values(), ...this.downloads.values()];

        if (allTasks.length === 0) {
            this.setPanelToReadyState();
            return;
        }

        allTasks.forEach(task => {
            if (task.status !== 'cancelled') {
                totalSize += task.size;
                totalProgress += (task.status === 'completed') ? task.size : task.progress;
                
                // Check if task is active (queued, transferring, starting_folder)
                if (['transferring', 'starting_folder', 'queued'].includes(task.status)) {
                    activeCount++;
                    currentSpeed += task.speed || 0;
                    
                    // Identify task type
                    if (this.uploads.has(task.id)) {
                        activeUploads++;
                    } else if (this.downloads.has(task.id)) {
                        activeDownloads++;
                    }
                } else if (task.status === 'failed') {
                    failedCount++;
                }
            }
        });

        // Determine State & Styling
        panel.classList.remove('status-completed', 'status-failed', 'transfer-active', 'upload-active', 'download-active', 'mixed-active');
        
        if (activeCount > 0) {
            panel.classList.add('transfer-active');
            
            // Determine specific activity type
            if (activeUploads > 0 && activeDownloads === 0) {
                panel.classList.add('upload-active');
            } else if (activeDownloads > 0 && activeUploads === 0) {
                panel.classList.add('download-active');
            } else {
                panel.classList.add('mixed-active');
            }

            titleEl.textContent = `正在傳輸 ${activeCount} 個項目`;
            
            // [MODIFIED] Use '-- B/s' for zero or undefined global speed
            const formattedSpeed = this.UIManager.formatBytes(currentSpeed);
            if (currentSpeed > 0 && formattedSpeed !== '0 B') {
                speedEl.textContent = `${formattedSpeed}/s`;
            } else {
                speedEl.textContent = '-- B/s';
            }
        } else if (failedCount > 0) {
            panel.classList.add('status-failed');
            titleEl.textContent = `${failedCount} 個項目失敗`;
            speedEl.textContent = '';
        } else {
            panel.classList.add('status-completed');
            titleEl.textContent = '傳輸完成';
            speedEl.textContent = '-- B/s'; 
        }

        // Update Progress Bar
        const percent = totalSize > 0 ? (totalProgress / totalSize) : 0;
        barEl.style.transform = `scaleX(${percent})`;
    },
    
    showDetailsModal() {
        UIManager.toggleModal('transfer-details-modal', true);
        this.updateDetailsModal();
    },

    updateDetailsModal() {
        const modal = document.getElementById('transfer-details-modal');
        if (modal.classList.contains('hidden')) return;

        const renderListRecursive = (map, element, indentLevel = 0, parentId = null) => {
            const sortedItems = [...map.values()].sort((a, b) => {
                if (a.isFolder && !b.isFolder) return -1;
                if (!a.isFolder && b.isFolder) return 1;
                return a.name.localeCompare(b.name, 'zh-Hans-CN-u-co-pinyin');
            });

            for (const task of sortedItems) {
                element.appendChild(this._createTaskElement(task, indentLevel, parentId));
                if (task.isFolder && task.expanded && task.children) {
                    renderListRecursive(task.children, element, indentLevel + 1, task.id);
                }
            }
        };

        const uploadListEl = document.getElementById('upload-list');
        uploadListEl.innerHTML = '';
        if (this.uploads.size === 0) {
            uploadListEl.innerHTML = '<p class="empty-list-msg">無上傳任務</p>';
        } else {
            renderListRecursive(this.uploads, uploadListEl);
        }

        const downloadListEl = document.getElementById('download-list');
        downloadListEl.innerHTML = '';
        if (this.downloads.size === 0) {
            downloadListEl.innerHTML = '<p class="empty-list-msg">無下載任務</p>';
        } else {
            renderListRecursive(this.downloads, downloadListEl);
        }

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

        row.querySelector('.file-icon').className = `file-icon ${item.isFolder ? 'fas fa-folder' : this.UIManager.getFileTypeIcon(item.name)}`;
        row.querySelector('.item-name').textContent = item.name;
        
        const progressFill = row.querySelector('.progress-fill');
        const progressText = row.querySelector('.item-progress-text');
        const itemStatus = row.querySelector('.item-status');
        const itemSpeed = row.querySelector('.item-speed');

        const percent = item.size > 0 ? (item.progress / item.size * 100) : 0;
        progressFill.style.width = `${percent}%`;
        
        // [MODIFIED] Use '-- B/s' for zero or undefined speed
        const formattedItemSpeed = this.UIManager.formatBytes(item.speed);
        if (item.speed && item.speed > 0 && formattedItemSpeed !== '0 B') {
            itemSpeed.textContent = `${formattedItemSpeed}/s`;
        } else {
            itemSpeed.textContent = '-- B/s';
        }

        switch (item.status) {
            case 'completed':
                progressFill.style.backgroundColor = 'var(--success-color)';
                progressText.textContent = this.UIManager.formatBytes(item.size);
                itemStatus.innerHTML = '<i class="fas fa-check-circle"></i>';
                break;
            case 'failed':
                progressFill.style.backgroundColor = 'var(--danger-color)';
                progressText.textContent = item.message || '失敗';
                itemStatus.innerHTML = '<i class="fas fa-exclamation-circle"></i>';
                break;
            case 'transferring':
                progressText.textContent = `${this.UIManager.formatBytes(item.progress)} / ${this.UIManager.formatBytes(item.size)} (${percent.toFixed(1)}%)`;
                itemStatus.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                break;
            case 'starting_folder':
                progressText.textContent = `${item.completed_files} / ${item.total_files} 個檔案 (${this.UIManager.formatBytes(item.size)})`;
                itemStatus.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                break;
            case 'queued':
                progressText.textContent = '等待中...';
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
        const result = this.findTask(taskId);
        if (result && result.task && result.task.isFolder) {
            result.task.expanded = !result.task.expanded;
            this.updateDetailsModal();
        }
    },
    
    updateMainFileListUI() {
        document.querySelectorAll('.file-item:not(.is-uploading)').forEach(el => {
            const name = el.dataset.name;
            let task = null;

            const findTaskByNameAndParent = (taskMap) => {
                for (const t of taskMap.values()) {
                    if (t.name === name && t.parentFolderId === this.AppState.currentFolderId) {
                        return t;
                    }
                }
                return null;
            };

            task = findTaskByNameAndParent(this.uploads) || findTaskByNameAndParent(this.downloads);

            el.classList.remove('in-transfer');
            if (el.querySelector('.transfer-overlay-icon')) el.querySelector('.transfer-overlay-icon').remove();
            
            if(task && ['transferring', 'paused', 'queued', 'starting_folder'].includes(task.status)) {
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
        const _findRecursive = (searchId, map, parent = null) => {
            for (const task of map.values()) {
                if (task.id === searchId) {
                    return { task, map, parent };
                }
                if (task.isFolder && task.children) {
                    const result = _findRecursive(searchId, task.children, task);
                    if (result) {
                        return result;
                    }
                }
            }
            return null;
        };

        return _findRecursive(id, this.uploads) || _findRecursive(id, this.downloads);
    },
    
    cancelItem(id) {
        const result = this.findTask(id);
        if (result) {
            if (['transferring', 'queued'].includes(result.task.status)) {
                this.ApiService.cancelTransfer(id).then(res => {
                    if (!res.success) {
                        console.warn(`Failed to send cancel request for task ${id}:`, res.message);
                    }
                });
            }
            const map = result.parent ? result.parent.children : result.map;
            const key = result.task.id;
            if (map.has(key)) {
                map.delete(key);
            }
            this.tick();
        }
    },

    cancelAll() {
        const _cancelRecursively = (map) => {
            map.forEach(task => {
                if (task.isFolder && task.children) {
                    _cancelRecursively(task.children);
                }
                this.cancelItem(task.id);
            });
        };
        _cancelRecursively(this.uploads);
        _cancelRecursively(this.downloads);
    },

    clearCompleted() {
        const _filterRecursively = (map) => {
            for (let [key, task] of map.entries()) {
                if (task.isFolder && task.children) {
                    _filterRecursively(task.children);
                }
                if (['completed', 'failed', 'cancelled'].includes(task.status)) {
                    map.delete(key);
                }
            }
        };
        _filterRecursively(this.uploads);
        _filterRecursively(this.downloads);
        this.tick();
    },

    setConcurrencyLimit(limit) { this.concurrencyLimit = limit; },
    getConcurrencyLimit() { return this.concurrencyLimit; },
    setDownloadDestination(path) { this.currentDownloadDestination = path; },
    
    setupEventListeners() {
        // [MODIFIED] New Event Listener for Sidebar Transfer Status
        const sidebarStatus = document.getElementById('sidebar-transfer-status');
        if (sidebarStatus) {
            sidebarStatus.addEventListener('click', () => this.showDetailsModal());
        }

        document.getElementById('close-modal-btn').addEventListener('click', () => UIManager.toggleModal('transfer-details-modal', false));
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
