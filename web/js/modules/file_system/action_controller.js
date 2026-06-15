const ActionController = {
    _appState: null,
    _apiService: null,
    _uiModals: null,
    _refreshAllCallback: null,
    _navigateToCallback: null,
    _uiManager: null,

    init(dependencies) {
        ActionController._appState = dependencies.appState;
        ActionController._apiService = dependencies.apiService;
        ActionController._uiModals = dependencies.uiModals;
        ActionController._refreshAllCallback = dependencies.refreshAllCallback;
        ActionController._navigateToCallback = dependencies.navigateToCallback;
        ActionController._uiManager = dependencies.uiManager;
    },

    async handleMove() {
        if (ActionController._appState.selectedItems.length === 0) {
            return await ActionController._uiModals.showAlert(window.t('dialog.alert_title'), window.t('dialog.select_move_item'), 'btn-primary');
        }

        return ActionView.showMoveModal(
            ActionController._appState,
            ActionController._uiManager,
            ActionController._uiModals,
            async (selectedTargetId) => {
                const itemsToMove = ActionController._appState.selectedItems.map(item => ({ id: item.id, type: item.type }));
                await ActionController.executeMove(itemsToMove, selectedTargetId);
            }
        );
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
                const folder = ActionController._appState.folderMap.get(current);
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
        if (targetFolderId === ActionController._appState.currentFolderId) return;

        const isCircular = items.some(item => {
            if (item.type !== 'folder') return false;
            let current = targetFolderId;
            while (current) {
                if (current === item.id) return true;
                const folder = ActionController._appState.folderMap.get(current);
                current = folder ? folder.parent_id : null;
            }
            return false;
        });

        if (isCircular) {
            ActionController._uiModals.showAlert(window.t('dialog.invalid_move'), window.t('dialog.invalid_move_sub'), 'btn-danger');
            return;
        }
        ActionController._uiManager.startProgress();
        ActionController._uiManager.setInteractionLock(true);

        try {
            const result = await ActionController._apiService.moveItems(items, targetFolderId);
            if (result.success) {
                await ActionController._refreshAllCallback();
            } else {
                ActionController._uiManager.handleBackendError(result);
            }
        } catch (error) {
            console.error("Move operation failed:", error);
            ActionController._uiManager.handleBackendError({ message: window.t('dialog.err_backend') });
        } finally {
            ActionController._uiManager.stopProgress();
            ActionController._uiManager.setInteractionLock(false);
        }
    },

    async handleDetails() {
        ActionView.showDetailsModal(ActionController._appState, ActionController._uiModals);
    },

    async handleRename(item) {
        const { id, name, type } = item;

        await ActionController._uiModals.showPrompt(
            window.t('menu.rename'),
            window.t('dialog.rename_prompt').replace('{name}', name),
            name,
            async (newName) => {
                if (newName === name) return { success: true };

                try {
                    const result = await ActionController._apiService.renameItem(id, newName, type);
                    if (result.success) {
                        await ActionController._refreshAllCallback();
                        return { success: true };
                    } else {
                        return { success: false, error_code: result.error_code };
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
        if (ActionController._appState.selectedItems.length === 0) {
            return await ActionController._uiModals.showAlert(window.t('dialog.alert_title'), window.t('dialog.select_del_item'), 'btn-primary');
        }
        
        ActionController._uiManager.startProgress();
        ActionController._uiManager.setInteractionLock(true);
        try {
            const itemsToDelete = ActionController._appState.selectedItems.map(item => ({ id: item.id, type: item.type }));
            const result = await ActionController._apiService.deleteItems(itemsToDelete);
            if (result.success) {
                await ActionController._refreshAllCallback();
            } else {
                ActionController._uiManager.handleBackendError(result);
            }
        } catch (error) {
            console.error("Delete operation failed:", error);
            ActionController._uiManager.handleBackendError({ message: window.t('dialog.err_backend') });
        } finally {
            ActionController._uiManager.stopProgress();
            ActionController._uiManager.setInteractionLock(false);
        }
    },

    async handleNewFolder() {
        await ActionController._uiModals.showPrompt(
            window.t('dialog.new_folder'),
            window.t('dialog.new_folder_prompt'),
            window.t('dialog.unnamed_folder'),
            async (newFolderName) => {
                try {
                    const result = await ActionController._apiService.createFolder(ActionController._appState.currentFolderId, newFolderName);
                    if (result.success) {
                        await ActionController._refreshAllCallback();
                        return { success: true };
                    } else {
                        return { success: false, error_code: result.error_code };
                    }
                } catch (error) {
                    console.error("Create folder operation failed:", error);
                    return { success: false, message: window.t('dialog.err_backend') };
                }
            }
        );
    },

    async handleDownload() {
        if (ActionController._appState.selectedItems.length === 0) {
            return await ActionController._uiModals.showAlert(window.t('dialog.alert_title'), window.t('dialog.select_dl_item'), 'btn-primary');
        }

        let destinationDir = null;
        const useDefault = localStorage.getItem('useDefaultDownloadPath') === 'true';

        if (useDefault) {
            destinationDir = localStorage.getItem('defaultDownloadPath');
            if (!destinationDir) {
                await ActionController._uiModals.showAlert(window.t('dialog.err_title'), window.t('dialog.dl_path_not_set'), 'btn-primary');
                return;
            }
        } else {
            UIManager.toggleModal('blocking-overlay', true);
            try {
                destinationDir = await ActionController._apiService.selectDirectory(window.t('dialog.select_dl_folder'));
            } finally {
                UIManager.toggleModal('blocking-overlay', false);
            }
            if (!destinationDir) return; 
        }

        TransferModel.currentDownloadDestination = destinationDir;

        const itemsToDownload = [];
        let duplicateCount = 0;

        for (const item of ActionController._appState.selectedItems) {
            let isDuplicate = false;
            for (const task of TransferModel.downloads.values()) {
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
            ActionController._uiModals.showAlert(window.t('dialog.alert_title'), window.t('dialog.dl_duplicate_skip').replace('{count}', duplicateCount), 'btn-secondary');
        }

        if (itemsToDownload.length > 0) {
            itemsToDownload.forEach(item => TransferModel.addDownload(item));
            ActionController._apiService.downloadItems(itemsToDownload, destinationDir);
        }
    },

    async handleFileUpload() {
        UIManager.toggleModal('blocking-overlay', true);
        try {
            const localPaths = await ActionController._apiService.selectFiles(true, window.t('dialog.select_ul_file'));
            if (!localPaths || localPaths.length === 0) return;

            const parentId = ActionController._appState.currentFolderId;
            const filesToUpload = [];

            localPaths.forEach(path => {
                const fileName = path.split(/[\\/]/).pop();
                const isDuplicate = ActionController._appState.currentFolderContents.files.some(f => f.name === fileName) ||
                    ActionController._appState.currentFolderContents.folders.some(f => f.name === fileName);

                if (isDuplicate) {
                    ActionController._uiModals.showAlert(window.t('dialog.ul_failed'), window.t('dialog.file_exists').replace('{name}', fileName));
                    return;
                }

                const fileToUploadData = {
                    localPath: path,
                    name: fileName,
                    task_id: crypto.randomUUID(),
                    parentFolderId: parentId,
                    isFolder: false 
                };
                TransferModel.addUpload(fileToUploadData);
                filesToUpload.push(fileToUploadData);
            });

            if (filesToUpload.length > 0) {
                ActionController._apiService.uploadFiles(parentId, filesToUpload.map(f => ({ local_path: f.localPath, task_id: f.task_id })));
            }
        } finally {
            UIManager.toggleModal('blocking-overlay', false);
        }
    },

    async handleFolderUpload() {
        UIManager.toggleModal('blocking-overlay', true);
        try {
            const folderPath = await ActionController._apiService.selectDirectory(window.t('dialog.select_ul_folder'));
            if (!folderPath) return;

            const parentId = ActionController._appState.currentFolderId;
            const folderName = folderPath.split(/[\\/]/).pop();

            const isDuplicate = ActionController._appState.currentFolderContents.files.some(f => f.name === folderName) ||
                ActionController._appState.currentFolderContents.folders.some(f => f.name === folderName);

            if (isDuplicate) {
                ActionController._uiModals.showAlert(window.t('dialog.ul_failed'), window.t('dialog.file_exists').replace('{name}', folderName));
                return;
            }

            const taskId = crypto.randomUUID();

            TransferModel.addUpload({
                task_id: taskId,
                name: folderName,
                localPath: folderPath,
                parentFolderId: parentId,
                size: 0
            });

            ActionController._apiService.uploadFolder(parentId, folderPath, taskId);

        } finally {
            UIManager.toggleModal('blocking-overlay', false);
        }
    },

    handleSearch(term) {
        if (!term || term.trim() === '') {
            ActionController.exitSearchMode();
            ActionController._navigateToCallback(ActionController._appState.currentFolderId);
            return;
        }

        const requestId = Date.now().toString();
        ActionController._appState.currentViewRequestId = requestId;
        ActionController._appState.isSearching = true;
        ActionController._appState.searchTerm = term.trim();

        ActionController._uiManager.startProgress();
        ActionController._appState.currentFolderContents = { folders: [], files: [] }; 
        FileListHandler.sortAndRender(ActionController._appState); 
        FileListHandler.updateBreadcrumb(ActionController._appState, ActionController._navigateToCallback);

        const rootFolder = ActionController._appState.folderTreeData.find(f => f.parent_id === null);
        const baseFolderId = (ActionController._appState.searchScope === 'all' && rootFolder) ? rootFolder.id : ActionController._appState.currentFolderId;
        
        const onBatch = (data) => {
            if (ActionController._appState.currentViewRequestId !== requestId) return;
            if (data.folders) ActionController._appState.currentFolderContents.folders.push(...data.folders);
            if (data.files) ActionController._appState.currentFolderContents.files.push(...data.files);
            FileListHandler.sortAndRender(ActionController._appState);
        };

        ActionController._apiService.searchDbItems(baseFolderId, ActionController._appState.searchTerm, onBatch).then(response => {
            if (ActionController._appState.currentViewRequestId !== requestId) return;
            ActionController._uiManager.stopProgress();
            if (!response.success) {
                ActionController._uiManager.handleBackendError({ error_code: response.error_code || 'SEARCH_FAILED' });
            } else {
                console.log(`Search complete for request_id: ${requestId}`);
            }
        });
    },

    exitSearchMode() {
        ActionController._appState.isSearching = false;
        ActionController._appState.searchTerm = '';
        document.querySelector('.search-bar input').value = '';
    },

    async handleLogout() {
        const confirmed = await ActionController._uiModals.showConfirm(window.t('dialog.confirm_logout'), window.t('dialog.confirm_logout_msg'));
        if (confirmed) {
            ActionController._uiManager.startProgress();
            ActionController._uiManager.setInteractionLock(true);
            try {
                await ActionController._apiService.logout();
                // Bridge settings are persistent, no need to clear localStorage anymore
                // localStorage.clear();
                window.location.href = 'login.html';
            } catch (error) {
                console.error("Logout operation failed:", error);
                ActionController._uiManager.handleBackendError({ message: window.t('dialog.logout_err') });
                ActionController._uiManager.stopProgress();
                ActionController._uiManager.setInteractionLock(false);
            }
        }
    },

    async handlePlayVideo(fileId) {
        ActionController._uiManager.startProgress();
        try {
            const result = await ActionController._apiService.playVideo(fileId);
            if (!result.success) {
                ActionController._uiManager.handleBackendError(result);
            }
        } catch (error) {
            console.error("Play video failed:", error);
            ActionController._uiManager.handleBackendError({ message: window.t('dialog.play_err') });
        } finally {
            ActionController._uiManager.stopProgress();
        }
    },
};
