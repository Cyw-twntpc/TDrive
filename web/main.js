window.onload = function() {
    // --- 應用程式狀態 ---
    const AppState = {
        currentFolderId: 1,
        folderTreeData: [],
        folderMap: new Map(),
        selectedItems: new Set(),
        currentFolderContents: { folders: [], files: [] },
        currentSort: { key: 'name', order: 'asc' },
        searchScope: 'all',
        userInfo: null,
        userAvatar: null,
        isSearching: false,
        searchTerm: '',
        searchResults: []
    };

    // --- DOM 元素 ---
    const fileListBodyEl = document.getElementById('file-list-body');
    const searchScopeToggle = document.getElementById('search-scope-toggle');
    const searchInput = document.querySelector('.search-bar input');
    
    const uploadBtn = document.getElementById('upload-btn');
    const newFolderBtn = document.getElementById('new-folder-btn');
    const deleteBtn = document.getElementById('delete-btn');
    const downloadBtn = document.getElementById('download-btn');
    const saveSettingsBtn = document.getElementById('save-settings-btn');
    const concurrencyLimitSelect = document.getElementById('concurrency-limit');

    // --- 設定管理 ---
    function loadSettings() {
        const savedLimit = localStorage.getItem('concurrencyLimit');
        if (savedLimit) {
            concurrencyLimitSelect.value = savedLimit;
        } else {
            concurrencyLimitSelect.value = '3';
        }
        TransferManager.setConcurrencyLimit(parseInt(concurrencyLimitSelect.value, 10));
    }

    function saveSettings() {
        const limit = concurrencyLimitSelect.value;
        localStorage.setItem('concurrencyLimit', limit);
        TransferManager.setConcurrencyLimit(parseInt(limit, 10));
        
        const originalText = saveSettingsBtn.textContent;
        saveSettingsBtn.textContent = '已儲存！';
        saveSettingsBtn.disabled = true;
        setTimeout(() => {
            saveSettingsBtn.textContent = originalText;
            saveSettingsBtn.disabled = false;
        }, 1500);
    }
    
    async function loadUserDisplayInfo() {
        UIHandler.populateUserInfoPopover(AppState);
        
        const [info, avatar] = await Promise.all([eel.get_user_info()(), eel.get_user_avatar()()]);

        if (info && info.success) AppState.userInfo = info;
        if (avatar && avatar.success) AppState.userAvatar = avatar.avatar_base64;
        
        UIHandler.updateUserAvatar(AppState);
        UIHandler.populateUserInfoPopover(AppState);
    }


    // --- UI 渲染協調 ---
    function renderListAndSyncManager(appState) {
        UIHandler.sortAndRenderList(appState);
        TransferManager.updateMainFileListUI();
    }


    // --- 全局刷新函式 ---
    async function refreshAll() {
        if (AppState.isSearching) {
            await handleSearch(AppState.searchTerm);
            return;
        }
    
        const folderTree = await eel.get_folder_tree_data()();
        AppState.folderTreeData = folderTree;
        AppState.folderMap.clear();
        folderTree.forEach(f => AppState.folderMap.set(f.id, f));

        if (AppState.currentFolderId === 1) { 
            const rootFolder = folderTree.find(f => f.parent_id === null);
            if (rootFolder) {
                AppState.currentFolderId = rootFolder.id;
            } else {
                return UIHandler.showAlert('嚴重錯誤', '找不到根目錄，無法載入檔案。', 'btn-danger');
            }
        }
    
        const contents = await eel.get_folder_contents(AppState.currentFolderId)();
    
        if (contents && contents.success !== false) {
            AppState.currentFolderContents = contents;
        } else {
            console.error(`無法獲取資料夾 ID ${AppState.currentFolderId} 的內容。導覽至根目錄。`);
            await UIHandler.handleBackendError(contents);
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder) await navigateTo(rootFolder.id);
            return;
        }
    
        UIHandler.renderFileTree(AppState, navigateTo);
        renderListAndSyncManager(AppState);
        UIHandler.updateBreadcrumb(AppState, navigateTo);
        UIHandler.updateTreeSelection(AppState);
    }


    // --- 動作處理函式 (已重構 v3) ---
    async function handleRename(item) {
        const { id, name, type } = item;
        
        const validator = async (newName) => {
            if (newName === name) return { success: true }; // 名稱未變更，直接視為成功
            return await eel.rename_item(id, newName, type)();
        };

        const newName = await UIHandler.showPrompt('重新命名', `請為 "${name}" 輸入新的名稱：`, name, validator);
        
        if (newName !== null) { // 當 Promise resolve 的值不是 null 時 (代表成功且非取消)
            await refreshAll();
        }
    }

    async function handleDownload() {
        if (AppState.selectedItems.size === 0) {
            return await UIHandler.showAlert("提示", "請先選擇要下載的項目。", 'btn-primary');
        }

        const destinationDir = await eel.ask_folder("請選擇儲存位置")();
        if (!destinationDir) return;

        TransferManager.setDownloadDestination(destinationDir);

        const itemsToDownload = [];
        const currentItemsMap = new Map();
        AppState.currentFolderContents.folders.forEach(item => currentItemsMap.set(item.id, { ...item, type: 'folder' }));
        AppState.currentFolderContents.files.forEach(item => currentItemsMap.set(item.id, { ...item, type: 'file' }));

        AppState.selectedItems.forEach(id => {
            const item = currentItemsMap.get(id);
            if (item) {
                itemsToDownload.push({ id: item.id, type: item.type, name: item.name, size: item.raw_size });
            }
        });
        
        if (itemsToDownload.length > 0) {
            // 將原始項目列表傳遞給後端
            eel.download_items(itemsToDownload, destinationDir, TransferManager.getConcurrencyLimit())();
        }
    }

    async function handleDelete() {
        if (AppState.selectedItems.size === 0) {
            return await UIHandler.showAlert("提示", "請先選擇要刪除的項目。", 'btn-primary');
        }
        
        const confirmation = await UIHandler.showConfirm('確認刪除', `您確定要刪除這 ${AppState.selectedItems.size} 個項目嗎？<br><b>此操作無法復原。</b>`);
        if (!confirmation) return;

        const itemsToDelete = [];
        AppState.selectedItems.forEach(id => {
            let itemType = AppState.currentFolderContents.folders.find(i => i.id === id) ? 'folder' : 'file';
            itemsToDelete.push({ id: id, type: itemType });
        });
        
        document.body.style.cursor = 'wait';
        const result = await eel.delete_items(itemsToDelete)();
        document.body.style.cursor = 'default';

        if (result.success) {
            await refreshAll();
        } else {
            await UIHandler.handleBackendError(result);
        }
    }

    async function handleNewFolder() {
        const validator = async (folderName) => {
            return await eel.create_folder(AppState.currentFolderId, folderName)();
        };

        const newFolderName = await UIHandler.showPrompt("新增資料夾", "請輸入新資料夾的名稱：", "", validator);

        if (newFolderName !== null) { // 當 Promise resolve 的值不是 null 時 (代表成功且非取消)
            await refreshAll();
        }
    }

    async function handleUpload() {
        const localPaths = await eel.ask_files("請選擇要上傳的檔案", {multiple: true})();
        if (localPaths && localPaths.length > 0) {
            localPaths.forEach(path => {
                const fileName = path.split(/[\\/]/).pop();
                TransferManager.addUpload({ name: fileName, size: 0, localPath: path, parentFolderId: AppState.currentFolderId });
            });
            renderListAndSyncManager(AppState); // Refresh to show placeholders if any
            eel.upload_files(AppState.currentFolderId, localPaths, TransferManager.getConcurrencyLimit())();
        }
    }

    async function handleSearch(term) {
        if (!term || term.trim() === '') {
            exitSearchMode();
            await navigateTo(AppState.currentFolderId);
            return;
        }

        AppState.isSearching = true;
        AppState.searchTerm = term.trim();
        document.body.style.cursor = 'wait';

        const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
        const baseFolderId = (AppState.searchScope === 'all' && rootFolder) ? rootFolder.id : AppState.currentFolderId;

        const results = await eel.search_db_items(baseFolderId, AppState.searchTerm)();
        document.body.style.cursor = 'default';
        
        if (results && results.success !== false) {
            AppState.currentFolderContents = results;
            renderListAndSyncManager(AppState);
            UIHandler.updateBreadcrumb(AppState, navigateTo);
        } else {
            await UIHandler.handleBackendError(results);
        }
    }
    
    async function navigateTo(folderId) {
        if (AppState.isSearching) exitSearchMode();

        if (!AppState.folderMap.has(folderId)) {
            await UIHandler.showAlert("錯誤", "目標資料夾不存在，可能已被移動或刪除。", 'btn-primary');
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if(rootFolder) await navigateTo(rootFolder.id);
            return;
        }
    
        AppState.currentFolderId = folderId;
        const contents = await eel.get_folder_contents(folderId)();
        
        if (contents && contents.success !== false) {
            AppState.currentFolderContents = contents;
        } else {
            await UIHandler.handleBackendError(contents);
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if(rootFolder) await navigateTo(rootFolder.id);
            return;
        }
        
        renderListAndSyncManager(AppState);
        UIHandler.renderFileTree(AppState, navigateTo);
        UIHandler.updateBreadcrumb(AppState, navigateTo);
        UIHandler.updateTreeSelection(AppState);
    }
    
    async function handleLogout() {
        const confirmed = await UIHandler.showConfirm('確認登出', '您確定要登出嗎？這將清除所有本地資料和設定。');
        if (confirmed) {
            await eel.logout()();
            localStorage.clear();
            window.location.href = 'login.html';
        }
    }

    function exitSearchMode() {
        AppState.isSearching = false;
        AppState.searchTerm = '';
        searchInput.value = '';
        AppState.searchResults = [];
    }

    function setupEventListeners() {
        document.getElementById('logout-btn').addEventListener('click', handleLogout);
        uploadBtn.addEventListener('click', handleUpload);
        downloadBtn.addEventListener('click', handleDownload);
        newFolderBtn.addEventListener('click', handleNewFolder);
        deleteBtn.addEventListener('click', handleDelete);
        saveSettingsBtn.addEventListener('click', saveSettings);
        
        searchScopeToggle.addEventListener('click', () => {
            AppState.searchScope = (AppState.searchScope === 'all') ? 'current' : 'all';
            searchScopeToggle.textContent = (AppState.searchScope === 'all') ? '全部資料夾' : '目前資料夾';
        });

        let searchTimeout;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => handleSearch(e.target.value), 300);
        });    
        
        UIHandler.setupSortableHeaders(AppState, () => renderListAndSyncManager(AppState));
        UIHandler.setupSelection(document.getElementById('file-list-container'), AppState, () => {});
        UIHandler.setupPopovers(); // 恢復此呼叫
        
        fileListBodyEl.addEventListener('item-rename', e => handleRename(e.detail));
        fileListBodyEl.addEventListener('item-download', e => {
            UIHandler.selectSingleItem(e.detail.id, AppState);
            handleDownload();
        });
        fileListBodyEl.addEventListener('item-delete', e => {
            AppState.selectedItems.clear();
            AppState.selectedItems.add(e.detail.id);
            handleDelete();
        });
        fileListBodyEl.addEventListener('folder-dblclick', e => navigateTo(e.detail.id));
    }
    
    async function initialize() {
        await refreshAll();
        setupEventListeners();
        TransferManager.initialize(AppState, eel, UIHandler, refreshAll);
        loadSettings();
        loadUserDisplayInfo();
    }
    
    initialize();
};