const ActionHandler = {
    // --- Dependencies ---
    _appState: null,
    _apiService: null,
    _uiModals: null,
    _transferManager: null,
    _refreshAllCallback: null,
    _navigateToCallback: null,
    _uiManager: null, // 新增 UIManager 依賴

    // --- Initialization ---
    init(dependencies) {
        this._appState = dependencies.appState;
        this._apiService = dependencies.apiService;
        this._uiModals = dependencies.uiModals;
        this._transferManager = dependencies.transferManager;
        this._refreshAllCallback = dependencies.refreshAllCallback;
        this._navigateToCallback = dependencies.navigateToCallback;
        this._uiManager = dependencies.uiManager; // 賦值 UIManager
    },

    // --- Action Handlers ---
    async handleRename(item) {
        const { id, name, type } = item;
        const validator = async (newName) => {
            if (newName === name) return { success: true };
            return await this._apiService.renameItem(id, newName, type);
        };
        const newName = await this._uiModals.showPrompt('重新命名', `請為 "${name}" 輸入新的名稱：`, name, validator);
        if (newName !== null) {
            await this._refreshAllCallback();
        }
    },

    async handleDownload() {
        if (this._appState.selectedItems.length === 0) {
            return await this._uiModals.showAlert("提示", "請先選擇要下載的項目。", 'btn-primary');
        }
        
        let destinationDir = null;
        const useDefault = localStorage.getItem('useDefaultDownloadPath') === 'true';
        
        if (useDefault) {
            destinationDir = localStorage.getItem('defaultDownloadPath');
            if (!destinationDir) {
                await this._uiModals.showAlert("錯誤", "您已啟用預設下載路徑，但尚未設定路徑。", 'btn-primary');
                return;
            }
        } else {
            UIManager.toggleModal('blocking-overlay', true);
            try {
                destinationDir = await this._apiService.selectDirectory("選取下載資料夾");
            } finally {
                UIManager.toggleModal('blocking-overlay', false);
            }
            if (!destinationDir) return; // User cancelled dialog
        }

        this._transferManager.setDownloadDestination(destinationDir);
        const itemsToDownload = this._appState.selectedItems.map(item => ({
            db_id: item.id,
            task_id: crypto.randomUUID(),
            type: item.type,
            name: item.name,
            size: item.raw_size
        }));

        if (itemsToDownload.length > 0) {
            itemsToDownload.forEach(item => this._transferManager.addDownload(item));
            this._apiService.downloadItems(itemsToDownload, destinationDir, this._transferManager.getConcurrencyLimit());
        }
    },

    async handleDelete() {
        if (this._appState.selectedItems.length === 0) {
            return await this._uiModals.showAlert("提示", "請先選擇要刪除的項目。", 'btn-primary');
        }
        const confirmation = await this._uiModals.showConfirm('確認刪除', `您確定要刪除這 ${this._appState.selectedItems.length} 個項目嗎？<br><b>此操作無法復原。</b>`);
        if (!confirmation) return;

        const itemsToDelete = this._appState.selectedItems.map(item => ({ id: item.id, type: item.type }));
        document.body.style.cursor = 'wait';
        const result = await this._apiService.deleteItems(itemsToDelete);
        document.body.style.cursor = 'default';
        if (result.success) {
            await this._refreshAllCallback();
        } else {
            await this._uiManager.handleBackendError(result);
        }
    },

    async handleNewFolder() {
        const validator = async (folderName) => {
            return await this._apiService.createFolder(this._appState.currentFolderId, folderName);
        };
        const newFolderName = await this._uiModals.showPrompt("新增資料夾", "請輸入新資料夾的名稱：", "", validator);
        if (newFolderName !== null) {
            await this._refreshAllCallback();
        }
    },

    async handleUpload() {
        UIManager.toggleModal('blocking-overlay', true);
        try {
            const localPaths = await this._apiService.selectFiles(true, "選取要上傳的檔案");
            if (!localPaths || localPaths.length === 0) return;

            const parentId = this._appState.currentFolderId;
            const filesToUpload = [];
            
            localPaths.forEach(path => {
                const fileName = path.split(/[\\/]/).pop();
                const isDuplicate = this._appState.currentFolderContents.files.some(f => f.name === fileName) || 
                                    this._appState.currentFolderContents.folders.some(f => f.name === fileName);
                
                if (isDuplicate) {
                    this._uiModals.showAlert('上傳失敗', `檔案夾中已存在同名項目 "${fileName}"。`);
                    return;
                }

                const fileToUploadData = {
                    localPath: path,
                    name: fileName,
                    task_id: crypto.randomUUID(),
                    parentFolderId: parentId
                };
                this._transferManager.addUpload(fileToUploadData);
                filesToUpload.push(fileToUploadData);

                // Add a placeholder to UI
                const placeholderItem = {
                    id: fileToUploadData.task_id,
                    name: fileName,
                    modif_date: new Date().toISOString().slice(0, 10),
                    size: '---',
                    raw_size: 0,
                    isUploading: true,
                    type: 'file'
                };
                this._appState.currentFolderContents.files.push(placeholderItem);
            });

            if (filesToUpload.length > 0) {
                // Re-render the file list with the placeholder
                FileListHandler.sortAndRender(this._appState); 
                this._apiService.uploadFiles(parentId, filesToUpload.map(f => ({ local_path: f.localPath, task_id: f.task_id })), this._transferManager.getConcurrencyLimit());
            }
        } finally {
            UIManager.toggleModal('blocking-overlay', false);
        }
    },

    async handleSearch(term) {
        if (!term || term.trim() === '') {
            this.exitSearchMode();
            await this._navigateToCallback(this._appState.currentFolderId);
            return;
        }
        this._appState.isSearching = true;
        this._appState.searchTerm = term.trim();
        document.body.style.cursor = 'wait';

        const rootFolder = this._appState.folderTreeData.find(f => f.parent_id === null);
        const baseFolderId = (this._appState.searchScope === 'all' && rootFolder) ? rootFolder.id : this._appState.currentFolderId;
        
        const rawResults = await this._apiService.searchDbItems(baseFolderId, this._appState.searchTerm);
        document.body.style.cursor = 'default';

        if (rawResults && rawResults.success !== false) {
            this._appState.currentFolderContents = rawResults;
            FileListHandler.sortAndRender(this._appState);
            FileListHandler.updateBreadcrumb(this._appState, this._navigateToCallback);
        } else {
            await this._uiManager.handleBackendError(rawResults);
        }
    },

    async handleLogout() {
        const confirmed = await this._uiModals.showConfirm('確認登出', '您確定要登出嗎？這將清除所有本地資料和設定。');
        if (confirmed) {
            await this._apiService.logout();
            localStorage.clear();
            window.location.href = 'login.html';
        }
    },

    exitSearchMode() {
        this._appState.isSearching = false;
        this._appState.searchTerm = '';
        document.querySelector('.search-bar input').value = '';
    }
};
