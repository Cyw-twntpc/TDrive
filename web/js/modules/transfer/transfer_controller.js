const TransferManager = {
    updateInterval: null,
    AppState: null,
    ApiService: null,
    UIManager: null,
    refreshCallback: null,
    _validityCheckInterval: null,

    initialize(AppState, ApiService, UIManager, refreshCallback) {
        TransferManager.AppState = AppState;
        TransferManager.ApiService = ApiService;
        TransferManager.UIManager = UIManager;
        TransferManager.refreshCallback = refreshCallback;
        TransferManager.setupEventListeners();
        
        if (window.tdrive_bridge && window.tdrive_bridge.transfer_progress_updated) {
            window.tdrive_bridge.transfer_progress_updated.connect(TransferModel.updateTask.bind(TransferView));
        }
        if (window.tdrive_bridge && window.tdrive_bridge.file_status_changed) {
            window.tdrive_bridge.file_status_changed.connect(TransferModel.updateFileExistence.bind(TransferView));
        }

        if (TransferManager.ApiService.getInitialStats) {
            TransferManager.ApiService.getInitialStats().then(data => {
                if (data) {
                    if (data.todayTraffic !== undefined) {
                        const trafficEl = document.getElementById('hero-daily-traffic');
                        if (trafficEl) trafficEl.textContent = TransferManager.UIManager.formatBytes(data.todayTraffic);
                    }
                    if (data.chunkSize) {
                        TransferModel.chunkSize = data.chunkSize;
                    }
                }
                
                TransferManager.ApiService.getIncompleteTransfers().then(stateData => {
                    if (stateData) {
                        TransferModel.restoreTasks(stateData.uploads, 'upload');
                        TransferModel.restoreTasks(stateData.downloads, 'download');
                        TransferView.updateAllUI();

                        TransferManager.ApiService.getAllFileStatuses().then(statuses => {
                            if (statuses) {
                                const changes = Object.entries(statuses).map(([id, exists]) => ({ id, exists }));
                                TransferModel.updateFileExistence(changes);
                            }
                        });
                    }
                });
            });
        } else {
            TransferManager.ApiService.getIncompleteTransfers().then(stateData => {
                if (stateData) {
                    TransferModel.restoreTasks(stateData.uploads, 'upload');
                    TransferModel.restoreTasks(stateData.downloads, 'download');
                    TransferView.updateAllUI();
                }
            });
        }
    },

    startUpdater() {
        if (!TransferManager.updateInterval) TransferManager.updateInterval = setInterval(() => TransferManager.tick(), 50);
    },

    tick() {
        TransferView.updateAllUI();
        TransferModel.checkAndArchive(); 

        const allTransfers = [...TransferModel.uploads.values(), ...TransferModel.downloads.values()];
        if (allTransfers.length === 0) {
            clearInterval(TransferManager.updateInterval);
            TransferManager.updateInterval = null;
            TransferView.setPanelToReadyState();
        }
        
        const completedOrFailedTasks = allTransfers.filter(t => (t.status === 'completed' || t.status === 'failed') && !t.feedbackShown);
        if(completedOrFailedTasks.length > 0) {
            TransferManager.refreshCallback().then(() => {
                completedOrFailedTasks.forEach(task => {
                    TransferView.showFileFeedback(task.name, task.status);
                    task.feedbackShown = true;
                });
            });
        }
    },

    pauseTask(id) {
        const result = TransferModel.findTask(id);
        if (!result) return;
        TransferManager.ApiService.pauseTransfer(id);
    },

    resumeTask(id) {
        const result = TransferModel.findTask(id);
        if (!result) return;
        TransferManager.ApiService.resumeTransfer(id);
    },

    cancelItem(id) {
        const result = TransferModel.findTask(id);
        if (result) {
            TransferManager.ApiService.cancelTransfer(id);
            result.map.delete(id);
            
            if (TransferManager.AppState && TransferManager.AppState.currentFolderContents) {
                let removed = false;
                const fileIndex = TransferManager.AppState.currentFolderContents.files.findIndex(f => f.id === id);
                if (fileIndex > -1) {
                    TransferManager.AppState.currentFolderContents.files.splice(fileIndex, 1);
                    removed = true;
                }
                const folderIndex = TransferManager.AppState.currentFolderContents.folders.findIndex(f => f.id === id);
                if (folderIndex > -1) {
                    TransferManager.AppState.currentFolderContents.folders.splice(folderIndex, 1);
                    removed = true;
                }
                
                if (removed && typeof FileListHandler !== 'undefined') {
                    FileListHandler.sortAndRender(TransferManager.AppState);
                }
            }

            TransferManager.tick();
        }
    },

    resumeAll() {
        [TransferModel.uploads, TransferModel.downloads].forEach(map => map.forEach(t => { if(['paused', 'failed'].includes(t.status)) TransferManager.resumeTask(t.id); }));
    },

    pauseAll() {
        [TransferModel.uploads, TransferModel.downloads].forEach(map => map.forEach(t => { if(['transferring', 'queued'].includes(t.status)) TransferManager.pauseTask(t.id); }));
    },

    cancelAll() {
        [TransferModel.uploads, TransferModel.downloads].forEach(map => map.forEach(t => { if(['transferring', 'queued', 'paused', 'failed'].includes(t.status)) TransferManager.cancelItem(t.id); }));
    },

    setupEventListeners() {
        const sidebarStatus = document.getElementById('sidebar-transfer-status');
        if (sidebarStatus) sidebarStatus.addEventListener('click', () => { if(window.switchPage) window.switchPage('transfer'); });
        document.querySelectorAll('.tabs-container .tab-item').forEach(btn => btn.addEventListener('click', () => {
            document.querySelector('.tabs-container .tab-item.active').classList.remove('active');
            btn.classList.add('active');
            TransferView.currentTab = btn.dataset.tab;
            if (TransferView.currentTab === 'completed') TransferView._completedListDirty = true;
            TransferView.renderDashboard();
        }));
        document.querySelectorAll('.filter-segment').forEach(btn => btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-segment').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            TransferView.completedFilter = btn.dataset.filter;
            TransferView._completedListDirty = true;
            TransferView._renderCompletedList();
        }));
        document.querySelectorAll('.sort-item').forEach(btn => btn.addEventListener('click', () => TransferView.sortCompleted(btn.dataset.sort)));
        
        document.getElementById('btn-clear-completed')?.addEventListener('click', () => TransferModel.clearCompleted());

        document.getElementById('global-cancel-btn')?.addEventListener('click', () => TransferManager.cancelAll());
        document.getElementById('global-pause-btn')?.addEventListener('click', () => TransferManager.pauseAll());
        document.getElementById('retry-all-btn')?.addEventListener('click', () => TransferManager.resumeAll());
    },

};
