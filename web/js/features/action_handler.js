const ActionHandler = {
    _appState: null,
    _apiService: null,
    _uiModals: null,
    _transferManager: null,
    _refreshAllCallback: null,
    _navigateToCallback: null,
    _uiManager: null,

    init(dependencies) {
        this._appState = dependencies.appState;
        this._apiService = dependencies.apiService;
        this._uiModals = dependencies.uiModals;
        this._transferManager = dependencies.transferManager;
        this._refreshAllCallback = dependencies.refreshAllCallback;
        this._navigateToCallback = dependencies.navigateToCallback;
        this._uiManager = dependencies.uiManager;
    },

    async handleMove() {
        if (this._appState.selectedItems.length === 0) {
            return await this._uiModals.showAlert(window.t('dialog.alert_title'), window.t('dialog.select_move_item'), 'btn-primary');
        }

        const modalId = 'move-modal';
        const treeContainer = document.getElementById('move-tree-container');
        const confirmBtn = document.getElementById('move-confirm-btn');
        const cancelBtn = document.getElementById('move-cancel-btn');
        const closeBtn = document.getElementById('move-close-btn');
        let selectedTargetId = null;

        const getPathToCurrent = () => {
            const path = [];
            let current = this._appState.currentFolderId;
            while (current) {
                path.unshift(current);
                const folder = this._appState.folderMap.get(current);
                current = folder ? folder.parent_id : null;
            }
            return path;
        };
        const expandedIds = new Set(getPathToCurrent());

        const renderMoveTree = () => {
            treeContainer.innerHTML = '';
            const roots = this._appState.folderTreeData.filter(f => f.parent_id === null);

            const isBeingMoved = (folderId) => {
                return this._appState.selectedItems.some(i => i.type === 'folder' && i.id === folderId);
            };

            const createNode = (folder, level) => {
                if (isBeingMoved(folder.id)) return null;
                const nodeEl = document.createElement('div');
                nodeEl.className = 'tree-node';
                nodeEl.style.paddingLeft = `${level * 20}px`;
                const contentEl = document.createElement('div');
                contentEl.className = 'tree-content';
                contentEl.dataset.id = folder.id;

                const children = this._appState.folderTreeData.filter(f => f.parent_id === folder.id);
                const hasChildren = children.length > 0;

                let toggleIcon = '';
                if (hasChildren) {
                    const isExpanded = expandedIds.has(folder.id);
                    toggleIcon = `<i class="fas ${isExpanded ? 'fa-caret-down' : 'fa-caret-right'} tree-toggle"></i>`;
                } else {
                    toggleIcon = `<span class="tree-toggle-placeholder"></span>`;
                }

                contentEl.innerHTML = `${toggleIcon} <i class="fas fa-folder"></i> <span class="folder-name">${folder.name}</span>`;

                if (folder.id === this._appState.currentFolderId) {
                    contentEl.classList.add('current-location');
                    contentEl.title = window.t('dialog.current_location');
                }

                contentEl.addEventListener('click', (e) => {
                    if (folder.id === this._appState.currentFolderId) return;

                    if (e.target.classList.contains('tree-toggle')) {
                        e.stopPropagation();
                        if (expandedIds.has(folder.id)) expandedIds.delete(folder.id);
                        else expandedIds.add(folder.id);
                        renderMoveTree(); 
                        return;
                    }
                    document.querySelectorAll('#move-tree-container .tree-content.selected').forEach(el => el.classList.remove('selected'));
                    contentEl.classList.add('selected');
                    selectedTargetId = folder.id;
                    confirmBtn.disabled = false;
                });
                nodeEl.appendChild(contentEl);

                if (hasChildren && expandedIds.has(folder.id)) {
                    const childrenContainer = document.createElement('div');
                    children.forEach(child => {
                        const childNode = createNode(child, level + 1);
                        if (childNode) childrenContainer.appendChild(childNode);
                    });
                    nodeEl.appendChild(childrenContainer);
                }

                return nodeEl;
            };

            roots.forEach(root => {
                const rootNode = createNode(root, 0);
                if (rootNode) treeContainer.appendChild(rootNode);
            });
        };

        renderMoveTree();
        confirmBtn.disabled = true; 
        this._uiManager.toggleModal(modalId, true);
        return new Promise(resolve => {
            const cleanup = () => {
                this._uiManager.toggleModal(modalId, false);
                confirmBtn.removeEventListener('click', onConfirm);
                cancelBtn.removeEventListener('click', onCancel);
                closeBtn.removeEventListener('click', onCancel);
            };

            const onConfirm = async () => {
                if (selectedTargetId === null) return;
                cleanup();
                const itemsToMove = this._appState.selectedItems.map(item => ({ id: item.id, type: item.type }));
                await this.executeMove(itemsToMove, selectedTargetId);
                resolve();
            };
            const onCancel = () => {
                cleanup();
                resolve();
            };
            confirmBtn.addEventListener('click', onConfirm);
            cancelBtn.addEventListener('click', onCancel);
            closeBtn.addEventListener('click', onCancel);
        });
    },

    isValidMove(items, targetFolderId) {
        if (!items || items.length === 0) return false;

        const targetId = Number(targetFolderId);
        const isSelf = items.some(item => item.type === 'folder' && item.id === targetId);
        if (isSelf) return false;

        const isCircular = items.some(item => {
            if (item.type !== 'folder') return false;
            let current = targetId;

            let depth = 0;
            while (current && depth < 100) {
                if (current === item.id) return true;
                const folder = this._appState.folderMap.get(current);
                current = folder ? folder.parent_id : null;
                depth++;
            }
            return false;
        });
        if (isCircular) return false;
        return true;
    },

    async executeMove(items, targetFolderId) {
        if (!items || items.length === 0) return;
        if (targetFolderId === this._appState.currentFolderId) return;

        const isCircular = items.some(item => {
            if (item.type !== 'folder') return false;
            let current = targetFolderId;
            while (current) {
                if (current === item.id) return true;
                const folder = this._appState.folderMap.get(current);
                current = folder ? folder.parent_id : null;
            }
            return false;
        });

        if (isCircular) {
            this._uiModals.showAlert(window.t('dialog.invalid_move'), window.t('dialog.invalid_move_sub'), 'btn-danger');
            return;
        }
        this._uiManager.startProgress();
        this._uiManager.setInteractionLock(true);

        try {
            const result = await this._apiService.moveItems(items, targetFolderId);
            if (result.success) {
                await this._refreshAllCallback();
            } else {
                this._uiManager.handleBackendError(result);
            }
        } catch (error) {
            console.error("Move operation failed:", error);
            this._uiManager.handleBackendError({ message: window.t('dialog.err_backend') });
        } finally {
            this._uiManager.stopProgress();
            this._uiManager.setInteractionLock(false);
        }
    },

    async handleDetails() {
        const items = this._appState.selectedItems;
        if (items.length === 0) return;

        let contentHTML = '';
        if (items.length === 1) {
            const item = items[0];
            const sizeStr = item.type === 'file' ? (item.size || '--') : '--';
            const dateStr = item.modif_date || item.created_at || item.uploaded_at || '--';
            const iconClass = item.type === 'folder' ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
            
            // Build full path
            let pathStr = '';
            let currentId = item.parent_id || this._appState.currentFolderId;
            const pathArr = [];
            while (currentId) {
                const folder = this._appState.folderMap.get(currentId);
                if (folder) {
                    pathArr.unshift(folder.name);
                    currentId = folder.parent_id;
                } else break;
            }
            if (pathArr.length > 0) pathStr = pathArr.join('/') + '/';
            else pathStr = '/';

            let typeStr = window.t('file_list.type_file');
            if (item.type === 'folder') {
                typeStr = window.t('file_list.type_folder');
            } else {
                const parts = item.name.split('.');
                if (parts.length > 1) {
                    const ext = parts.pop().toLowerCase();
                    const extKey = `file_list.type_${ext}`;
                    const translatedExt = window.t(extKey);
                    
                    if (translatedExt !== extKey) {
                        typeStr = translatedExt;
                    } else {
                        typeStr = ext.toUpperCase() + ' ' + window.t('file_list.type_file');
                    }
                }
            }

            let visualHtml = `<i class="${iconClass}" style="font-size: 56px; margin-bottom: 12px; color: var(--primary-color);"></i>`;
            if (item.type === 'file' && this._appState.currentThumbnails && this._appState.currentThumbnails[item.id]) {
                visualHtml = `<img src="data:image/jpeg;base64,${this._appState.currentThumbnails[item.id]}" style="max-width: 120px; max-height: 120px; border-radius: 8px; margin-bottom: 12px; object-fit: cover; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">`;
            }

            contentHTML = `
                <div style="text-align: center; margin-bottom: 20px;">
                    ${visualHtml}
                </div>
                <table style="width: 100%; text-align: left; border-spacing: 0 10px; font-size: 0.95rem;">
                    <tr><td style="color: #666; width: 90px;" data-i18n="dialog.detail_name"></td><td style="word-break: break-all;">${item.name}</td></tr>
                    <tr><td style="color: #666; width: 90px;" data-i18n="dialog.detail_type"></td><td>${typeStr}</td></tr>
                    <tr><td style="color: #666;" data-i18n="dialog.detail_size"></td><td>${sizeStr}</td></tr>
                    <tr><td style="color: #666;" data-i18n="dialog.detail_date"></td><td>${dateStr}</td></tr>
                    <tr><td style="color: #666;" data-i18n="dialog.detail_path"></td><td style="word-break: break-all;">${pathStr}</td></tr>
                </table>
                <div id="adv-details-container" style="margin-top: 15px; border-top: 1px solid var(--border-color); padding-top: 15px; min-height: 40px;">
                    <div id="adv-details-spinner" style="text-align: center; color: #999;">
                        <i class="fas fa-spinner fa-spin"></i> ${window.t('main.loading') || 'Loading...'}
                    </div>
                    <table id="adv-details-table" style="width: 100%; text-align: left; border-spacing: 0 10px; font-size: 0.95rem; display: none;">
                        <!-- Advanced metadata rows will be injected here -->
                    </table>
                </div>
            `;

            // Display basic info immediately
            this._uiModals.showAlert(window.t('dialog.details_title'), contentHTML, 'btn-primary');
            
            // Force update title and remove data-i18n to prevent overwriting
            const titleEl = document.getElementById('alert-title');
            if (titleEl) {
                titleEl.removeAttribute('data-i18n');
                titleEl.textContent = window.t('dialog.details_title');
            }

            // Translate dynamically injected elements
            const alertMessageEl = document.getElementById('alert-message');
            if (window.i18n) window.i18n.translateDOM(alertMessageEl);

            // Hide spinner if it's a folder, else fetch advanced details
            const spinner = document.getElementById('adv-details-spinner');
            if (item.type === 'folder') {
                spinner.style.display = 'none';
            } else {
                // Fetch advanced details
                if (window.tdrive_bridge && window.tdrive_bridge.get_file_extended_details) {
                    window.tdrive_bridge.get_file_extended_details(item.id, (responseStr) => {
                        spinner.style.display = 'none';
                        try {
                            const response = JSON.parse(responseStr);
                            if (response && response.success && response.metadata && Object.keys(response.metadata).length > 0) {
                                const table = document.getElementById('adv-details-table');
                                let rows = '';
                                for (const [key, value] of Object.entries(response.metadata)) {
                                    const translatedKey = window.t(`meta.${key}`) || key;
                                    rows += `<tr><td style="color: #666; width: 90px;">${translatedKey}:</td><td style="word-break: break-all;">${value}</td></tr>`;
                                }
                                table.innerHTML = rows;
                                table.style.display = 'table';
                            }
                        } catch (e) {
                            console.error('Failed to parse extended details', e);
                        }
                    });
                } else {
                    spinner.style.display = 'none'; // Backend not implemented yet
                }
            }
        } else {
            let totalSize = 0;
            let folderCount = 0;
            let fileCount = 0;

            items.forEach(item => {
                if (item.type === 'folder') {
                    folderCount++;
                } else {
                    fileCount++;
                    totalSize += (item.raw_size || 0);
                }
            });

            contentHTML = `
                <div style="text-align: center; margin-bottom: 20px;">
                    <i class="fas fa-layer-group" style="font-size: 56px; margin-bottom: 12px; color: var(--primary-color);"></i>
                    <h3 style="margin: 0;">${items.length} ${window.t('dialog.items_selected')}</h3>
                </div>
                <table style="width: 100%; text-align: left; border-spacing: 0 10px; font-size: 0.95rem;">
                    <tr><td style="color: #666; width: 90px;" data-i18n="dialog.detail_folders"></td><td>${folderCount}</td></tr>
                    <tr><td style="color: #666;" data-i18n="dialog.detail_files"></td><td>${fileCount}</td></tr>
                    <tr><td style="color: #666;" data-i18n="dialog.detail_total_size"></td><td>${UIManager.formatBytes(totalSize)}</td></tr>
                </table>
            `;
            
            this._uiModals.showAlert(window.t('dialog.details_title'), contentHTML, 'btn-primary');
            const alertMessageEl = document.getElementById('alert-message');
            if (window.i18n) window.i18n.translateDOM(alertMessageEl);
        }
    },

    async handleRename(item) {
        const { id, name, type } = item;

        await this._uiModals.showPrompt(
            window.t('menu.rename'),
            window.t('dialog.rename_prompt').replace('{name}', name),
            name,
            async (newName) => {
                if (newName === name) return { success: true };

                try {
                    const result = await this._apiService.renameItem(id, newName, type);
                    if (result.success) {
                        await this._refreshAllCallback();
                        return { success: true };
                    } else {
                        return { success: false, message: result.message };
                    }
                } catch (error) {
                    console.error("Rename operation failed:", error);
                    return { success: false, message: window.t('dialog.err_backend') };
                }
            },
            'filename'
        );
    },

    async handleDelete() {
        if (this._appState.selectedItems.length === 0) {
            return await this._uiModals.showAlert(window.t('dialog.alert_title'), window.t('dialog.select_del_item'), 'btn-primary');
        }
        
        this._uiManager.startProgress();
        this._uiManager.setInteractionLock(true);
        try {
            const itemsToDelete = this._appState.selectedItems.map(item => ({ id: item.id, type: item.type }));
            const result = await this._apiService.deleteItems(itemsToDelete);
            if (result.success) {
                await this._refreshAllCallback();
            } else {
                this._uiManager.handleBackendError(result);
            }
        } catch (error) {
            console.error("Delete operation failed:", error);
            this._uiManager.handleBackendError({ message: window.t('dialog.err_backend') });
        } finally {
            this._uiManager.stopProgress();
            this._uiManager.setInteractionLock(false);
        }
    },

    async handleNewFolder() {
        await this._uiModals.showPrompt(
            window.t('dialog.new_folder'),
            window.t('dialog.new_folder_prompt'),
            window.t('dialog.unnamed_folder'),
            async (newFolderName) => {
                try {
                    const result = await this._apiService.createFolder(this._appState.currentFolderId, newFolderName);
                    if (result.success) {
                        await this._refreshAllCallback();
                        return { success: true };
                    } else {
                        return { success: false, message: result.message };
                    }
                } catch (error) {
                    console.error("Create folder operation failed:", error);
                    return { success: false, message: window.t('dialog.err_backend') };
                }
            }
        );
    },

    async handleDownload() {
        if (this._appState.selectedItems.length === 0) {
            return await this._uiModals.showAlert(window.t('dialog.alert_title'), window.t('dialog.select_dl_item'), 'btn-primary');
        }

        let destinationDir = null;
        const useDefault = localStorage.getItem('useDefaultDownloadPath') === 'true';

        if (useDefault) {
            destinationDir = localStorage.getItem('defaultDownloadPath');
            if (!destinationDir) {
                await this._uiModals.showAlert(window.t('dialog.err_title'), window.t('dialog.dl_path_not_set'), 'btn-primary');
                return;
            }
        } else {
            UIManager.toggleModal('blocking-overlay', true);
            try {
                destinationDir = await this._apiService.selectDirectory(window.t('dialog.select_dl_folder'));
            } finally {
                UIManager.toggleModal('blocking-overlay', false);
            }
            if (!destinationDir) return; 
        }

        this._transferManager.setDownloadDestination(destinationDir);

        const itemsToDownload = [];
        let duplicateCount = 0;

        for (const item of this._appState.selectedItems) {
            let isDuplicate = false;
            for (const task of this._transferManager.downloads.values()) {
                if (['queued', 'transferring', 'paused'].includes(task.status) &&
                    task.db_id === item.id &&
                    task.destinationDir === destinationDir) {

                    isDuplicate = true;
                    break;
                }
            }

            if (isDuplicate) {
                duplicateCount++;
                continue;
            }

            itemsToDownload.push({
                db_id: item.id,
                task_id: crypto.randomUUID(),
                type: item.type,
                name: item.name,
                size: item.raw_size
            });
        }

        if (duplicateCount > 0) {
            this._uiModals.showAlert(window.t('dialog.alert_title'), window.t('dialog.dl_duplicate_skip').replace('{count}', duplicateCount), 'btn-secondary');
        }

        if (itemsToDownload.length > 0) {
            itemsToDownload.forEach(item => this._transferManager.addDownload(item));
            this._apiService.downloadItems(itemsToDownload, destinationDir);
        }
    },

    async handleFileUpload() {
        UIManager.toggleModal('blocking-overlay', true);
        try {
            const localPaths = await this._apiService.selectFiles(true, window.t('dialog.select_ul_file'));
            if (!localPaths || localPaths.length === 0) return;

            const parentId = this._appState.currentFolderId;
            const filesToUpload = [];

            localPaths.forEach(path => {
                const fileName = path.split(/[\\/]/).pop();
                const isDuplicate = this._appState.currentFolderContents.files.some(f => f.name === fileName) ||
                    this._appState.currentFolderContents.folders.some(f => f.name === fileName);

                if (isDuplicate) {
                    this._uiModals.showAlert(window.t('dialog.ul_failed'), window.t('dialog.file_exists').replace('{name}', fileName));
                    return;
                }

                const fileToUploadData = {
                    localPath: path,
                    name: fileName,
                    task_id: crypto.randomUUID(),
                    parentFolderId: parentId,
                    isFolder: false 
                };
                this._transferManager.addUpload(fileToUploadData);
                filesToUpload.push(fileToUploadData);
            });

            if (filesToUpload.length > 0) {
                this._apiService.uploadFiles(parentId, filesToUpload.map(f => ({ local_path: f.localPath, task_id: f.task_id })));
            }
        } finally {
            UIManager.toggleModal('blocking-overlay', false);
        }
    },

    async handleFolderUpload() {
        UIManager.toggleModal('blocking-overlay', true);
        try {
            const folderPath = await this._apiService.selectDirectory(window.t('dialog.select_ul_folder'));
            if (!folderPath) return;

            const parentId = this._appState.currentFolderId;
            const folderName = folderPath.split(/[\\/]/).pop();

            const isDuplicate = this._appState.currentFolderContents.files.some(f => f.name === folderName) ||
                this._appState.currentFolderContents.folders.some(f => f.name === folderName);

            if (isDuplicate) {
                this._uiModals.showAlert(window.t('dialog.ul_failed'), window.t('dialog.file_exists').replace('{name}', folderName));
                return;
            }

            const taskId = crypto.randomUUID();

            this._transferManager.addUpload({
                task_id: taskId,
                name: folderName,
                localPath: folderPath,
                parentFolderId: parentId,
                size: 0
            });

            this._apiService.uploadFolder(parentId, folderPath, taskId);

        } finally {
            UIManager.toggleModal('blocking-overlay', false);
        }
    },

    handleSearch(term) {
        if (!term || term.trim() === '') {
            this.exitSearchMode();
            this._navigateToCallback(this._appState.currentFolderId);
            return;
        }

        const requestId = Date.now().toString();
        this._appState.currentViewRequestId = requestId;
        this._appState.isSearching = true;
        this._appState.searchTerm = term.trim();

        this._uiManager.startProgress();
        this._appState.currentFolderContents = { folders: [], files: [] }; 
        FileListHandler.sortAndRender(this._appState); 
        FileListHandler.updateBreadcrumb(this._appState, this._navigateToCallback);

        const rootFolder = this._appState.folderTreeData.find(f => f.parent_id === null);
        const baseFolderId = (this._appState.searchScope === 'all' && rootFolder) ? rootFolder.id : this._appState.currentFolderId;
        
        const onBatch = (data) => {
            if (this._appState.currentViewRequestId !== requestId) return;
            if (data.folders) this._appState.currentFolderContents.folders.push(...data.folders);
            if (data.files) this._appState.currentFolderContents.files.push(...data.files);
            FileListHandler.sortAndRender(this._appState);
        };

        this._apiService.searchDbItems(baseFolderId, this._appState.searchTerm, onBatch).then(response => {
            if (this._appState.currentViewRequestId !== requestId) return;
            this._uiManager.stopProgress();
            if (!response.success) {
                this._uiManager.handleBackendError({ message: response.message || window.t('dialog.search_err') });
            } else {
                console.log(`Search complete for request_id: ${requestId}`);
            }
        });
    },

    exitSearchMode() {
        this._appState.isSearching = false;
        this._appState.searchTerm = '';
        document.querySelector('.search-bar input').value = '';
    },

    async handleLogout() {
        const confirmed = await this._uiModals.showConfirm(window.t('dialog.confirm_logout'), window.t('dialog.confirm_logout_msg'));
        if (confirmed) {
            this._uiManager.startProgress();
            this._uiManager.setInteractionLock(true);
            try {
                await this._apiService.logout();
                // Bridge settings are persistent, no need to clear localStorage anymore
                // localStorage.clear();
                window.location.href = 'login.html';
            } catch (error) {
                console.error("Logout operation failed:", error);
                this._uiManager.handleBackendError({ message: window.t('dialog.logout_err') });
                this._uiManager.stopProgress();
                this._uiManager.setInteractionLock(false);
            }
        }
    },

    async handlePlayVideo(fileId) {
        this._uiManager.startProgress();
        try {
            const result = await this._apiService.playVideo(fileId);
            if (!result.success) {
                this._uiManager.handleBackendError(result);
            }
        } catch (error) {
            console.error("Play video failed:", error);
            this._uiManager.handleBackendError({ message: window.t('dialog.play_err') });
        } finally {
            this._uiManager.stopProgress();
        }
    },
};