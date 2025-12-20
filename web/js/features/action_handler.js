/**
 * @fileoverview Central dispatcher for handling user actions from the UI,
 * such as button clicks for rename, download, delete, etc.
 */

const ActionHandler = {
    // --- Dependencies (injected via init) ---
    _appState: null,
    _apiService: null,
    _uiModals: null,
    _transferManager: null,
    _refreshAllCallback: null,
    _navigateToCallback: null,
    _uiManager: null,

    /**
     * Initializes the handler with all necessary dependencies.
     * @param {object} dependencies - An object containing all required service and state modules.
     */
    init(dependencies) {
        this._appState = dependencies.appState;
        this._apiService = dependencies.apiService;
        this._uiModals = dependencies.uiModals;
        this._transferManager = dependencies.transferManager;
        this._refreshAllCallback = dependencies.refreshAllCallback;
        this._navigateToCallback = dependencies.navigateToCallback;
        this._uiManager = dependencies.uiManager;
    },

    /**
     * Handles the rename action for a single file or folder.
     * @param {object} item - The file or folder item to rename.
     */
    async handleRename(item) {
        const { id, name, type } = item;
        
        await this._uiModals.showPrompt(
            '重新命名',
            `請輸入 "${name}" 的新名稱：`,
            name,
            async (newName) => {
                if (newName === name) return { success: true }; // No change
                
                try {
                    const result = await this._apiService.renameItem(id, newName, type);
                    if (result.success) {
                        await this._refreshAllCallback();
                        return { success: true };
                    } else {
                        // Return error to be displayed inline
                        return { success: false, message: result.message };
                    }
                } catch (error) {
                    console.error("Rename operation failed:", error);
                    return { success: false, message: "與後端通訊時發生錯誤，請重試。" };
                }
            },
            'filename' // Selection strategy: only select the filename part
        );
    },

    /**
     * Handles the download action for all selected items.
     * It either uses the default download path or prompts the user to select one.
     */
    async handleDownload() {
        if (this._appState.selectedItems.length === 0) {
            return await this._uiModals.showAlert("提示", "請先選擇要下載的項目。", 'btn-primary');
        }
        
        let destinationDir = null;
        const useDefault = localStorage.getItem('useDefaultDownloadPath') === 'true';
        
        if (useDefault) {
            destinationDir = localStorage.getItem('defaultDownloadPath');
            if (!destinationDir) {
                await this._uiModals.showAlert("錯誤", "已啟用預設下載路徑但尚未設定。", 'btn-primary');
                return;
            }
        } else {
            UIManager.toggleModal('blocking-overlay', true);
            try {
                destinationDir = await this._apiService.selectDirectory("選擇下載資料夾");
            } finally {
                UIManager.toggleModal('blocking-overlay', false);
            }
            if (!destinationDir) return; // User cancelled the dialog
        }

        this._transferManager.setDownloadDestination(destinationDir);
        
        const itemsToDownload = [];
        let duplicateCount = 0;

        for (const item of this._appState.selectedItems) {
            // Check for duplicate download task (Same DB ID and Same Destination)
            let isDuplicate = false;
            for (const task of this._transferManager.downloads.values()) {
                // Check if task is active (not completed/failed/cancelled) and matches ID AND Destination
                if (['queued', 'transferring', 'paused', 'starting_folder'].includes(task.status) && 
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
            this._uiModals.showAlert("提示", `${duplicateCount} 個項目已在下載佇列中，將被略過。`, 'btn-secondary');
        }

        if (itemsToDownload.length > 0) {
            itemsToDownload.forEach(item => this._transferManager.addDownload(item));
            this._apiService.downloadItems(itemsToDownload, destinationDir);
        }
    },

    /**
     * Handles the delete action for all selected items after user confirmation.
     */
    async handleDelete() {
        if (this._appState.selectedItems.length === 0) {
            return await this._uiModals.showAlert("提示", "請先選擇要刪除的項目。", 'btn-primary');
        }
        const confirmation = await this._uiModals.showConfirm('確認刪除', `您確定要刪除這 ${this._appState.selectedItems.length} 個項目嗎？<br><b>此動作無法復原。</b>`);
        if (!confirmation) return;

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
            this._uiManager.handleBackendError({ message: "與後端通訊時發生錯誤，請重試。" });
        } finally {
            this._uiManager.stopProgress();
            this._uiManager.setInteractionLock(false);
        }
    },

                /**

                 * Checks if a move operation is valid.

                 * @param {Array} items - The items to move.

                 * @param {number} targetFolderId - The destination folder ID.

                 * @returns {boolean} True if the move is valid.

                 */

                isValidMove(items, targetFolderId) {

                    if (!items || items.length === 0) return false;

                    

                    // Ensure targetFolderId is a number

                    const targetId = Number(targetFolderId);

            

                    // Check if any item is being moved into itself

                    // [Fix] Only folders can be target destinations, so only a folder with the same ID is invalid (self-move).

                    // A file with ID 5 CAN be moved into a folder with ID 5.

                    const isSelf = items.some(item => item.type === 'folder' && item.id === targetId);

                    if (isSelf) return false;

            

                    // Check for circular dependency (moving a folder into its own subtree)

                    const isCircular = items.some(item => {

                        if (item.type !== 'folder') return false;

                        let current = targetId;

                        // Prevent infinite loop if tree is malformed

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

        

            /**

             * Executes the move operation via the API and handles the UI updates.

         * This is a shared helper used by both the move modal and drag-and-drop.

         * @param {Array} items - The items to move.

         * @param {number} targetFolderId - The destination folder ID.

         */

        async executeMove(items, targetFolderId) {

            if (!items || items.length === 0) return;

    

            // Prevent moving into self or moving to current folder (no-op)

            if (targetFolderId === this._appState.currentFolderId) return;

            

            // Prevent moving a folder into its own subtree (Basic frontend check, backend also checks)

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

                this._uiModals.showAlert("操作無效", "無法將資料夾移動到其子資料夾中。", 'btn-danger');

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

                this._uiManager.handleBackendError({ message: "與後端通訊時發生錯誤，請重試。" });

            } finally {

                this._uiManager.stopProgress();

                this._uiManager.setInteractionLock(false);

            }

        },

    

        /**

         * Handles the move action for all selected items via the modal dialog.

         */

        async handleMove() {

            if (this._appState.selectedItems.length === 0) {

                return await this._uiModals.showAlert("提示", "請先選擇要移動的項目。", 'btn-primary');

            }

    

            const modalId = 'move-modal';

            const treeContainer = document.getElementById('move-tree-container');

            const confirmBtn = document.getElementById('move-confirm-btn');

            const cancelBtn = document.getElementById('move-cancel-btn');

            const closeBtn = document.getElementById('move-close-btn');

    

            let selectedTargetId = null;

    

            // Helper to get the path of IDs from root to current folder for default expansion

            const getPathToCurrent = () => {

                const path = [];

                let current = this._appState.currentFolderId;

                while(current) {

                    path.unshift(current);

                    const folder = this._appState.folderMap.get(current);

                    current = folder ? folder.parent_id : null;

                }

                return path;

            };

            const expandedIds = new Set(getPathToCurrent());

    

            // Render the folder tree specifically for the move operation

            const renderMoveTree = () => {

                treeContainer.innerHTML = '';

                

                // Get root folders

                const roots = this._appState.folderTreeData.filter(f => f.parent_id === null);

                

                // Helper to check if a folder is one of the items being moved (to prevent moving into self)

                const isBeingMoved = (folderId) => {

                    return this._appState.selectedItems.some(i => i.type === 'folder' && i.id === folderId);

                };

    

                const createNode = (folder, level) => {

                    // If this folder is being moved, don't render it or its children in the destination tree

                    if (isBeingMoved(folder.id)) return null;

    

                    const nodeEl = document.createElement('div');

                    nodeEl.className = 'tree-node';

                    nodeEl.style.paddingLeft = `${level * 20}px`;

                    

                    const contentEl = document.createElement('div');

                    contentEl.className = 'tree-content';

                    contentEl.dataset.id = folder.id;

                    

                    // Add toggle icon if children exist

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

                        contentEl.title = "目前位置";

                    }

    

                    // Event listener for selection

                    contentEl.addEventListener('click', (e) => {

                        // Prevent selection of current folder

                        if (folder.id === this._appState.currentFolderId) return;

    

                        // Handle toggle click separately if clicked on the caret

                        if (e.target.classList.contains('tree-toggle')) {

                            e.stopPropagation();

                            if (expandedIds.has(folder.id)) expandedIds.delete(folder.id);

                            else expandedIds.add(folder.id);

                            renderMoveTree(); // Re-render to show/hide children

                            return;

                        }

    

                        document.querySelectorAll('#move-tree-container .tree-content.selected').forEach(el => el.classList.remove('selected'));

                        contentEl.classList.add('selected');

                        selectedTargetId = folder.id;

                        confirmBtn.disabled = false;

                    });

    

                    nodeEl.appendChild(contentEl);

    

                    // Render children if expanded

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

            confirmBtn.disabled = true; // [已恢復] 初始狀態禁用

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

    /**
     * Handles the creation of a new folder.
     */
    async handleNewFolder() {
        await this._uiModals.showPrompt(
            "新資料夾", 
            "請輸入新資料夾的名稱：", 
            "未命名資料夾",
            async (newFolderName) => {
                try {
                    const result = await this._apiService.createFolder(this._appState.currentFolderId, newFolderName);
                    if (result.success) {
                        await this._refreshAllCallback();
                        return { success: true };
                    } else {
                        // Return error to be displayed inline
                        return { success: false, message: result.message };
                    }
                } catch (error) {
                    console.error("Create folder operation failed:", error);
                    return { success: false, message: "與後端通訊時發生錯誤，請重試。" };
                }
            }
        );
    },

    /**
     * Handles the file upload action. Prompts the user to select files and initiates the upload process.
     */
    async handleFileUpload() {
        UIManager.toggleModal('blocking-overlay', true);
        try {
            const localPaths = await this._apiService.selectFiles(true, "選擇要上傳的檔案");
            if (!localPaths || localPaths.length === 0) return;

            const parentId = this._appState.currentFolderId;
            const filesToUpload = [];
            
            localPaths.forEach(path => {
                const fileName = path.split(/[\\/]/).pop();
                const isDuplicate = this._appState.currentFolderContents.files.some(f => f.name === fileName) || 
                                    this._appState.currentFolderContents.folders.some(f => f.name === fileName);
                
                if (isDuplicate) {
                    this._uiModals.showAlert('上傳失敗', `此資料夾中已存在名為 "${fileName}" 的項目。`);
                    return;
                }

                const fileToUploadData = {
                    localPath: path,
                    name: fileName,
                    task_id: crypto.randomUUID(),
                    parentFolderId: parentId,
                    isFolder: false // Explicitly mark as file
                };
                this._transferManager.addUpload(fileToUploadData);
                filesToUpload.push(fileToUploadData);

                // Add a placeholder to the UI immediately for better responsiveness.
                const placeholderItem = {
                    id: fileToUploadData.task_id, name: fileName,
                    modif_date: new Date().toISOString().slice(0, 10),
                    size: '---', raw_size: 0, isUploading: true, type: 'file'
                };
                this._appState.currentFolderContents.files.push(placeholderItem);
            });

            if (filesToUpload.length > 0) {
                // Re-render the file list with the new placeholder items.
                FileListHandler.sortAndRender(this._appState); 
                // Start the actual upload in the background.
                this._apiService.uploadFiles(parentId, filesToUpload.map(f => ({ local_path: f.localPath, task_id: f.task_id })));
            }
        } finally {
            UIManager.toggleModal('blocking-overlay', false);
        }
    },

    /**
     * Handles the folder upload action.
     */
    async handleFolderUpload() {
        UIManager.toggleModal('blocking-overlay', true);
        try {
            const folderPath = await this._apiService.selectDirectory("選擇要上傳的資料夾");
            if (!folderPath) return;

            const parentId = this._appState.currentFolderId;
            // folderPath is an absolute path string on Windows
            // Extract the last part as the folder name
            const folderName = folderPath.split(/[\\/]/).pop();

            const isDuplicate = this._appState.currentFolderContents.files.some(f => f.name === folderName) || 
                                this._appState.currentFolderContents.folders.some(f => f.name === folderName);
            
            if (isDuplicate) {
                this._uiModals.showAlert('上傳失敗', `此資料夾中已存在名為 "${folderName}" 的項目。`);
                return;
            }

            // We don't have a task_id yet (backend generates it for main folder), 
            // but we need one for the UI placeholder.
            // Backend's 'starting_folder' event will come with the real ID.
            // However, TransferManager logic relies on ID. 
            // Solution: We don't add to TransferManager here manually for the *folder*.
            // We let the backend's 'starting_folder' event trigger the addition of the folder card.
            // But we SHOULD add a placeholder in the file list.
            
            // Wait, TransferManager.addUpload is for files mostly.
            // Let's rely on backend events to populate the Transfer Dashboard.
            // But for the File List (main view), we want immediate feedback.
            
            const tempId = 'temp_folder_' + Date.now();
            const placeholderItem = {
                id: tempId, name: folderName,
                modif_date: new Date().toISOString().slice(0, 10),
                size: '---', raw_size: 0, isUploading: true, type: 'folder'
            };
            this._appState.currentFolderContents.folders.push(placeholderItem);
            FileListHandler.sortAndRender(this._appState); 

            this._apiService.uploadFolder(parentId, folderPath);

        } finally {
            UIManager.toggleModal('blocking-overlay', false);
        }
    },

    /**
     * Initiates a search operation.
     * @param {string} term - The search term.
     */
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

        // Provide immediate visual feedback.
        this._uiManager.startProgress();
        this._appState.currentFolderContents = { folders: [], files: [] }; // Clear previous results
        FileListHandler.sortAndRender(this._appState); // Render the empty state
        FileListHandler.updateBreadcrumb(this._appState, this._navigateToCallback);
        
        // This is a fire-and-forget API call; results will be streamed back.
        const rootFolder = this._appState.folderTreeData.find(f => f.parent_id === null);
        const baseFolderId = (this._appState.searchScope === 'all' && rootFolder) ? rootFolder.id : this._appState.currentFolderId;
        this._apiService.searchDbItems(baseFolderId, this._appState.searchTerm, requestId);
    },

    /**
     * Handles the user logout process after confirmation.
     */
    async handleLogout() {
        const confirmed = await this._uiModals.showConfirm('確認登出', '您確定要登出嗎？此動作將清除所有本機資料和設定。');
        if (confirmed) {
            this._uiManager.startProgress();
            this._uiManager.setInteractionLock(true);
            try {
                await this._apiService.logout();
                localStorage.clear();
                window.location.href = 'login.html';
            } catch (error) {
                console.error("Logout operation failed:", error);
                this._uiManager.handleBackendError({ message: "登出過程中發生錯誤，請重試。" });
                this._uiManager.stopProgress();
                this._uiManager.setInteractionLock(false);
            }
            // On success, we navigate away, so no need to stop progress or unlock UI.
        }
    },

    /**
     * Resets the application's state from search mode back to normal browsing.
     */
    exitSearchMode() {
        this._appState.isSearching = false;
        this._appState.searchTerm = '';
        document.querySelector('.search-bar input').value = '';
    }
};