const TransferModel = {
    uploads: new Map(),
    downloads: new Map(),
    uploadHistory: new Map(),
    downloadHistory: new Map(),
    currentDownloadDestination: '',
    chunkSize: 33554432,

    restoreTasks(taskMap, type) {
        if (!taskMap) return;

        for (const [taskId, info] of Object.entries(taskMap)) {
            const calculateProgress = (taskInfo) => {
                let p = 0;
                if (taskInfo.transferred_parts && Array.isArray(taskInfo.transferred_parts)) {
                    p = taskInfo.transferred_parts.length * TransferModel.chunkSize;
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
                name: (type === 'upload') ? info.file_path.split(/[\\/]/).pop() : (info.file_details?.name || window.t('transfer.unknown_name')),
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
                if (type === 'upload') TransferModel.uploadHistory.set(taskId, task);
                else TransferModel.downloadHistory.set(taskId, task);
            } else {
                if (type === 'upload') TransferModel.uploads.set(taskId, task);
                else TransferModel.downloads.set(taskId, task);
            }
        }
    },

    addDownload(item) {
        TransferView._showCompletedState = false;
        if (TransferModel.downloads.has(item.task_id)) return;
        const task = { 
            id: item.task_id, db_id: item.db_id, name: item.name, size: item.size || 0, 
            progress: 0, status: 'queued', localPath: item.save_path || TransferModel.currentDownloadDestination, 
            parentFolderId: TransferManager.AppState.currentFolderId, feedbackShown: false, 
            alertShown: false, startTime: Date.now(), completedAt: null,
            type: 'download',
            targetExists: true
        };
        TransferModel.downloads.set(item.task_id, task);
        TransferManager.startUpdater();
    },

    addUpload(fileData) {
        TransferView._showCompletedState = false;
        if (TransferModel.uploads.has(fileData.task_id)) return;
        const task = { 
            id: fileData.task_id, name: fileData.name, size: fileData.size || 0, 
            progress: 0, status: 'queued', localPath: fileData.localPath, 
            parentFolderId: fileData.parentFolderId, feedbackShown: false, 
            alertShown: false, startTime: Date.now(), completedAt: null,
            type: 'upload'
        };
        TransferModel.uploads.set(fileData.task_id, task);
        TransferManager.startUpdater();
    },

    updateTask(data) {
        if (data.parent_id) return; 

        let task = TransferModel.downloads.get(data.id) || TransferModel.uploads.get(data.id);

        if (!task) return;

        if (data.status === 'completed' && !task.completedAt) task.completedAt = Date.now();

        if (data.todayTraffic !== undefined) {
            const trafficEl = document.getElementById('hero-daily-traffic');
            if (trafficEl) trafficEl.textContent = TransferManager.UIManager.formatBytes(data.todayTraffic);
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
                    TransferView._completedListDirty = true;
                }
            }
        }
        
        if (data.speed !== undefined) task.speed = data.speed;
        if (data.total !== undefined && data.total > 0) task.size = data.total;
        if (data.error_code) task.message = window.t('errors.' + data.error_code);

        if (task.status === 'failed' && !task.alertShown) {
            TransferManager.UIManager.handleBackendError({
                error_code: 'TRANSFER_FAILED',
                message: window.t('transfer.msg_failed').replace('{name}', task.name).replace('{reason}', task.message || window.t('transfer.reason_unknown'))
            });
            task.alertShown = true;
        }

        if (statusChanged) {
            if (TransferManager.AppState.currentPage === 'transfer') TransferView.renderDashboard();
        } else {
            TransferView.renderTaskCard(task);
        }
        TransferView.updateSummaryPanel();
        TransferManager.startUpdater();
    },

    checkAndArchive() {
        const activeUploads = [...TransferModel.uploads.values()].filter(t => ['queued', 'transferring', 'paused'].includes(t.status));
        const activeDownloads = [...TransferModel.downloads.values()].filter(t => ['queued', 'transferring', 'paused'].includes(t.status));
        
        if (activeUploads.length === 0 && activeDownloads.length === 0) {
            let moved = false;
            
            for (const [id, task] of TransferModel.uploads.entries()) {
                if (task.status === 'completed' && task.feedbackShown) {
                    TransferModel.uploadHistory.set(id, task);
                    TransferModel.uploads.delete(id);
                    moved = true;
                }
            }
            
            for (const [id, task] of TransferModel.downloads.entries()) {
                if (task.status === 'completed' && task.feedbackShown) {
                    TransferModel.downloadHistory.set(id, task);
                    TransferModel.downloads.delete(id);
                    moved = true;
                }
            }
            
            if (moved) {
                TransferView._showCompletedState = true;
                TransferView._completedListDirty = true;
                if (TransferManager.AppState.currentPage === 'transfer' && TransferView.currentTab === 'completed') {
                    TransferView._renderCompletedList();
                }
            }
        }
    },

    findTask(id) {
        if (TransferModel.uploads.has(id)) return { task: TransferModel.uploads.get(id), map: TransferModel.uploads };
        if (TransferModel.downloads.has(id)) return { task: TransferModel.downloads.get(id), map: TransferModel.downloads };
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

        cleanMap(TransferModel.uploads);
        cleanMap(TransferModel.downloads);
        cleanMap(TransferModel.uploadHistory);
        cleanMap(TransferModel.downloadHistory);

        idsToRemove.forEach(id => TransferManager.ApiService.removeTransferHistory(id));

        TransferManager.tick();
        if (TransferView.currentTab === 'completed') TransferView._renderCompletedList();
    },

    removeSingleHistoryItem(taskId, type) {
        if (type === 'upload') {
            TransferModel.uploadHistory.delete(taskId);
            TransferModel.uploads.delete(taskId); 
        } else {
            TransferModel.downloadHistory.delete(taskId);
            TransferModel.downloads.delete(taskId);
        }
        
        TransferManager.ApiService.removeTransferHistory(taskId);
        
        if (TransferView.currentTab === 'completed') TransferView._renderCompletedList();
        
        TransferView.updateSummaryPanel();
    },

    updateFileExistence(changes) {
        if (!Array.isArray(changes)) return;
        
        changes.forEach(change => {
            const result = TransferModel.findTask(change.id);
            const task = result ? result.task : (TransferModel.uploadHistory.get(change.id) || TransferModel.downloadHistory.get(change.id));
            
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
                        if (isUp) btnTitle = isValid ? window.t('transfer.go_to_cloud') : window.t('transfer.folder_not_exists');
                        else btnTitle = isValid ? window.t('transfer.show_in_explorer') : window.t('transfer.file_removed');

                        const btn = isUp ? el.querySelector('.btn-go-cloud') : el.querySelector('.btn-reveal-local');
                        if (btn) btn.title = btnTitle;

                        if (isUp) {
                            const pathEl = el.querySelector('.sm-name div');
                            if (pathEl) {
                                pathEl.textContent = window.t('transfer.upload_to').replace('{path}', TransferView._getCloudPath(task.parentFolderId));
                            }
                        }
                    }
                }
            }
        });
    },

};
