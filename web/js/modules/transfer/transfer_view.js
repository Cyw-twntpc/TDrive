const TransferView = {
    currentTab: 'uploads',
    completedSort: { key: 'time', order: 'desc' },
    completedFilter: 'all',
    _completedListDirty: true,
    _showCompletedState: false,

    updateAllUI() {
        TransferView.updateSummaryPanel();
        if (TransferManager.AppState.currentPage === 'transfer') TransferView.renderDashboard();
        TransferView.updateMainFileListUI();
    },

    updateSummaryPanel() {
        const panel = document.getElementById('sidebar-transfer-status');
        const titleEl = document.getElementById('sidebar-transfer-title');
        const speedEl = document.getElementById('sidebar-transfer-speed');
        const barEl = document.getElementById('sidebar-transfer-bar');
        
        let totalSize = 0, totalProgress = 0, currentSpeed = 0, activeCount = 0, failedCount = 0;
        let activeUploads = 0, activeDownloads = 0;
        const allTasks = [...TransferModel.uploads.values(), ...TransferModel.downloads.values()];

        if (allTasks.length === 0) { TransferView.setPanelToReadyState(); return; }

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
                if (TransferModel.uploads.has(task.id)) activeUploads++;
                else activeDownloads++;
            }
        });

        panel.classList.remove('status-completed', 'transfer-active', 'upload-active', 'download-active', 'mixed-active');
        
        if (activeCount > 0) {
            panel.classList.add('transfer-active');
            if (activeUploads > 0 && activeDownloads === 0) panel.classList.add('upload-active');
            else if (activeDownloads > 0 && activeUploads === 0) panel.classList.add('download-active');
            else panel.classList.add('mixed-active');

            titleEl.textContent = window.t('transfer.status_active').replace('{count}', activeCount);
            speedEl.textContent = (currentSpeed > 0) ? `${TransferManager.UIManager.formatBytes(currentSpeed)}/s` : '-- B/s';
        } else if (allTasks.some(t => t.status === 'paused')) {
            panel.classList.add('transfer-active');
            titleEl.textContent = window.t('transfer.status_paused');
            speedEl.textContent = '-- B/s';
        } else if ((totalSize > 0 && totalProgress >= totalSize) || (activeCount === 0 && TransferView._showCompletedState)) {
            panel.classList.add('status-completed');
            titleEl.textContent = window.t('transfer.status_completed');
            speedEl.textContent = '-- B/s'; 
        } else {
            TransferView.setPanelToReadyState();
            return;
        }

        let scale = 0;
        if (totalSize > 0) {
            scale = totalProgress / totalSize;
        } else if (activeCount === 0 && TransferView._showCompletedState) {
            scale = 1;
        }
        barEl.style.transform = `scaleX(${scale})`;
        TransferView.updateDashboardHero(currentSpeed, activeCount, allTasks);
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
        if (speedEl) speedEl.textContent = currentSpeed > 0 ? `${TransferManager.UIManager.formatBytes(currentSpeed)}/s` : '-- B/s';
        
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
                etaEl.textContent = seconds > 86400 ? window.t('transfer.eta_days') : new Date(seconds * 1000).toISOString().substr(11, 8);
            } else etaEl.textContent = '--:--:--';
        }
    },

    renderDashboard() {
        const container = document.getElementById('page-transfer');
        if (!container || container.classList.contains('hidden')) {
            return;
        }

        const liveView = document.getElementById('transfer-live-view');
        const completedView = document.getElementById('transfer-completed-view');
        
        if (TransferView.currentTab === 'completed') {
            liveView.classList.add('hidden');
            completedView.classList.remove('hidden');
            
            if (TransferView._completedListDirty) {
                TransferView._renderCompletedList();
            } else {
                TransferView._refreshHistoryPathLabels();
            }
            return;
        }

        liveView.classList.remove('hidden');
        completedView.classList.add('hidden');

        const targetMap = (TransferView.currentTab === 'uploads') ? TransferModel.uploads : TransferModel.downloads;
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

        TransferView._renderSection('active', activeTasks, TransferView._createActiveCard.bind(TransferView));
        TransferView._renderSection('failed', failedTasks, TransferView._createFailedCard.bind(TransferView));
        TransferView._renderSection('queued', queuedTasks, TransferView._createQueuedCard.bind(TransferView));
        
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
                if (listActive) TransferView.renderEmptyState(listActive);
            }
        }
    },

    renderEmptyState(container) {
        container.innerHTML = `
            <div class="empty-state" style="text-align:center; padding:40px; color:#9ca3af;">
                <i class="fas fa-tasks" style="font-size:48px; margin-bottom:15px; display:block;"></i>
                <p>${window.t('transfer.no_transfers')}</p>
            </div>
        `;
    },

    checkEmptyState() {
        const activeCount = [...TransferModel.uploads.values(), ...TransferModel.downloads.values()].filter(t => !['completed', 'cancelled'].includes(t.status)).length;
        if (activeCount === 0) {
             TransferView.renderDashboard();
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
                TransferView.renderTaskCard(task, existingCards.get(task.id));
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
        if (sizeEl) sizeEl.textContent = `${TransferManager.UIManager.formatBytes(task.progress)} / ${TransferManager.UIManager.formatBytes(task.size)}`;

        const btn = el.querySelector('.btn-toggle');
        const speedEl = el.querySelector('.meta-speed');
        
        if (speedEl) {
            if (task.status === 'paused') {
                speedEl.textContent = window.t('transfer.lbl_paused');
                speedEl.style.color = '#f59e0b';
                if (btn && btn.dataset.lastStatus !== 'paused') {
                    btn.innerHTML = '<i class="fas fa-play"></i>';
                    btn.title = window.t('transfer.lbl_resume');
                    btn.dataset.lastStatus = 'paused';
                }
            } else if (task.status === 'failed') {
                speedEl.textContent = window.t('transfer.lbl_failed');
                speedEl.style.color = 'var(--danger-color)';
                if (btn && btn.dataset.lastStatus !== 'failed') {
                    btn.innerHTML = '<i class="fas fa-redo"></i>';
                    btn.title = window.t('transfer.lbl_retry');
                    btn.dataset.lastStatus = 'failed';
                }
            } else if (['queued', 'pending'].includes(task.status)) {
                speedEl.textContent = window.t('transfer.lbl_queued');
                speedEl.style.color = '';
                if (btn && btn.dataset.lastStatus !== 'queued') {
                    btn.innerHTML = '<i class="fas fa-pause"></i>'; 
                    btn.title = window.t('transfer.lbl_pause');
                    btn.dataset.lastStatus = 'queued';
                }
            } else {
                speedEl.style.color = '';
                const speed = TransferManager.UIManager.formatBytes(task.speed || 0);
                let eta = '';
                if (task.speed > 0 && (task.size - task.progress) > 0) {
                    const sec = Math.ceil((task.size - task.progress) / task.speed);
                    eta = (sec > 60 ? window.t('transfer.eta_format_min').replace('{time}', Math.ceil(sec / 60)) : window.t('transfer.eta_format_sec').replace('{time}', sec));
                }
                speedEl.textContent = `${speed}/s${eta}`;
                
                if (btn && btn.dataset.lastStatus !== 'transferring') {
                    btn.innerHTML = '<i class="fas fa-pause"></i>';
                    btn.title = window.t('transfer.lbl_pause');
                    btn.dataset.lastStatus = 'transferring';
                }
            }
        }
        const failedMsg = el.querySelector('.failed-msg');
        if (failedMsg && task.message) failedMsg.textContent = task.message;
    },

    toggleTaskState(id) {
        const result = TransferModel.findTask(id);
        if (!result) return;
        
        if (['paused', 'failed'].includes(result.task.status)) {
            TransferManager.resumeTask(id);
        } else {
            TransferManager.pauseTask(id);
        }
    },

    _createActiveCard(task) {
        const el = document.createElement('div');
        el.className = 'task-card-lg';
        el.dataset.id = task.id;
        el.dataset.type = task.type;
        const pathInfo = (task.type === 'upload') 
            ? `<i class="fas fa-file-upload"></i> ${TransferManager.UIManager.escapeHtml(task.localPath || window.t('transfer.path_unknown'))}` 
            : `<i class="fas fa-cloud-download-alt"></i> ` + window.t('transfer.download_to').replace('{path}', TransferManager.UIManager.escapeHtml(task.localPath || window.t('transfer.path_default')));
        el.innerHTML = `
            <div class="card-row-main">
                <div class="file-icon-lg"><i class="${TransferManager.UIManager.getFileTypeIcon(task.name)}"></i></div>
                <div class="card-content">
                    <div class="file-title">${TransferManager.UIManager.escapeHtml(task.name)}</div>
                    <div class="file-path">${pathInfo}</div>
                    <div class="progress-track"><div class="progress-fill"></div></div>
                    <div class="meta-row"><span class="meta-size"></span><span class="meta-speed"></span></div>
                </div>
                <div class="card-actions">
                    <button class="icon-btn btn-toggle"><i class="fas fa-pause"></i></button>
                    <button class="icon-btn btn-cancel" title="${window.t('transfer.btn_cancel')}"><i class="fas fa-times"></i></button>
                </div>
            </div>`;
        TransferView._bindCardActions(el, task);
        TransferView.renderTaskCard(task, el);
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
                <div class="failed-info"><span style="font-weight:600; font-size:14px;">${TransferManager.UIManager.escapeHtml(task.name)}</span><span class="failed-msg">${TransferManager.UIManager.escapeHtml(task.message || window.t('transfer.reason_unknown'))}</span></div>
            </div>
            <div class="card-actions">
                <button class="icon-btn btn-retry" style="color:var(--primary-color);" title="${window.t('transfer.btn_retry')}"><i class="fas fa-redo"></i></button>
                <button class="icon-btn btn-cancel" title="${window.t('transfer.btn_cancel')}"><i class="fas fa-times"></i></button>
            </div>`;
        TransferView._bindCardActions(el, task);
        TransferView.renderTaskCard(task, el);
        return el;
    },

    _createQueuedCard(task) {
        const el = document.createElement('div');
        el.className = 'task-card-queued';
        el.dataset.id = task.id;
        el.dataset.type = task.type;
        el.innerHTML = `
            <div class="drag-handle"><i class="fas fa-grip-vertical"></i></div>
            <div class="queued-content"><div class="queued-name">${TransferManager.UIManager.escapeHtml(task.name)}</div><div class="queued-size">${TransferManager.UIManager.formatBytes(task.size)}</div></div>
            <button class="icon-btn btn-cancel" title="${window.t('transfer.btn_cancel')}"><i class="fas fa-times"></i></button>`;
        TransferView._bindCardActions(el, task);
        TransferView.renderTaskCard(task, el);
        return el;
    },

    _bindCardActions(el, task) {
        el.querySelector('.btn-cancel')?.addEventListener('click', () => TransferManager.cancelItem(task.id));
        el.querySelector('.btn-toggle')?.addEventListener('click', () => TransferView.toggleTaskState(task.id));
        el.querySelector('.btn-retry')?.addEventListener('click', () => TransferManager.resumeTask(task.id));
    },

    _getCloudPath(folderId) {
        if (!TransferManager.AppState || !TransferManager.AppState.folderMap) return window.t('transfer.path_not_exists');
        
        const path = [];
        let current = TransferManager.AppState.folderMap.get(folderId);

        if (!current) return window.t('transfer.path_not_exists');

        while (current) {
            path.unshift(current.name);
            
            if (current.parent_id === null) {
                return path.join(' / ');
            }

            const next = TransferManager.AppState.folderMap.get(current.parent_id);
            
            if (!next) return window.t('transfer.path_not_exists');
            
            current = next;
        }

        return window.t('transfer.path_not_exists');
    },

    _renderCompletedList() {
        const listEl = document.getElementById('list-completed');
        if (!listEl) return; 
        listEl.innerHTML = ''; 

        let sourceTasks = [];
        if (TransferView.completedFilter === 'all') {
            sourceTasks = [
                ...TransferModel.uploadHistory.values(), ...TransferModel.downloadHistory.values(),
                ...[...TransferModel.uploads.values()].filter(t => t.status === 'completed'),
                ...[...TransferModel.downloads.values()].filter(t => t.status === 'completed')
            ];
        } else if (TransferView.completedFilter === 'upload') {
            sourceTasks = [
                ...TransferModel.uploadHistory.values(),
                ...[...TransferModel.uploads.values()].filter(t => t.status === 'completed')
            ];
        } else {
            sourceTasks = [
                ...TransferModel.downloadHistory.values(),
                ...[...TransferModel.downloads.values()].filter(t => t.status === 'completed')
            ];
        }

        const allCompleted = sourceTasks;
        const { key, order } = TransferView.completedSort;
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
            
            const cloudPath = isUp ? TransferView._getCloudPath(task.parentFolderId) : '';
            
            const isValid = task.targetExists !== false;
            const itemClass = isValid ? 'history-item' : 'history-item item-invalid';
            
            let btnTitle;
            if (isUp) btnTitle = isValid ? window.t('transfer.go_to_cloud') : window.t('transfer.folder_not_exists');
            else btnTitle = isValid ? window.t('transfer.show_in_explorer') : window.t('transfer.file_removed');

            row.className = itemClass;
            row.innerHTML = `
                <div class="sm-icon"><i class="${TransferManager.UIManager.getFileTypeIcon(task.name)}"></i></div>
                <div class="sm-name">
                    ${TransferManager.UIManager.escapeHtml(task.name)}
                    <div style="font-size:12px; color:#9ca3af; margin-top:2px;">
                        ${isUp ? window.t('transfer.upload_to').replace('{path}', TransferManager.UIManager.escapeHtml(cloudPath)) : window.t('transfer.download_to').replace('{path}', TransferManager.UIManager.escapeHtml(task.localPath || window.t('transfer.path_default')))}
                    </div>
                </div>
                <div class="sm-badge">${isUp ? window.t('transfer.upload_success') : window.t('transfer.download_success')}</div>
                <div class="history-actions">
                    ${isUp 
                        ? `<button class="icon-btn btn-go-cloud" title="${btnTitle}"><i class="fas fa-external-link-alt"></i></button>`
                        : `<button class="icon-btn btn-reveal-local" title="${btnTitle}"><i class="fas fa-folder-open"></i></button>`
                    }
                </div>
                <button class="btn-remove-history" title="${window.t('transfer.remove_record')}"><i class="fas fa-trash-alt"></i></button>`;
            
            listEl.appendChild(row);

            row.querySelector('.btn-remove-history').onclick = (e) => {
                e.stopPropagation();
                TransferModel.removeSingleHistoryItem(task.id, task.type);
            };

            if (isUp) {
                row.querySelector('.btn-go-cloud').onclick = () => {
                    if (window.switchPage) window.switchPage('files');
                    if (window.navigateTo) window.navigateTo(task.parentFolderId);
                };
            } else {
                const btnReveal = row.querySelector('.btn-reveal-local');
                btnReveal.onclick = () => {
                    TransferManager.ApiService.showItemInFolder(task.localPath);
                };
            }
        });
        TransferView._completedListDirty = false;
    },

    sortCompleted(key) {
        if (!['time', 'name'].includes(key)) return;
        if (TransferView.completedSort.key === key) TransferView.completedSort.order = (TransferView.completedSort.order === 'asc') ? 'desc' : 'asc';
        else { TransferView.completedSort.key = key; TransferView.completedSort.order = 'desc'; }
        
        TransferView._completedListDirty = true;

        document.querySelectorAll('.sort-item').forEach(el => {
            el.classList.toggle('active', el.dataset.sort === key);
            const icon = el.querySelector('i');
            if (icon) icon.className = el.dataset.sort === key ? (TransferView.completedSort.order === 'asc' ? 'fas fa-sort-up' : 'fas fa-sort-down') : 'fas fa-sort';
        });
        TransferView._renderCompletedList();
    },

    updateMainFileListUI() {
        document.querySelectorAll('.file-item:not(.is-uploading)').forEach(el => {
            const name = el.dataset.name;
            // Skip folders to avoid locking them (greyed out)
            if (el.dataset.type === 'folder') return;
            
            const task = [...TransferModel.uploads.values(), ...TransferModel.downloads.values()].find(t => t.name === name && t.parentFolderId === TransferManager.AppState.currentFolderId);
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

    _refreshHistoryPathLabels() {
        const listEl = document.getElementById('list-completed');
        if (!listEl) return;

        const items = listEl.querySelectorAll('.history-item[data-type="upload"]');
        items.forEach(el => {
            const taskId = el.dataset.id;
            const task = TransferModel.uploads.get(taskId) || TransferModel.uploadHistory.get(taskId);
            if (task && task.parentFolderId) {
                const pathEl = el.querySelector('.sm-name div');
                if (pathEl) {
                    pathEl.textContent = window.t('transfer.upload_to').replace('{path}', TransferView._getCloudPath(task.parentFolderId));
                }
            }
        });
    },

};
