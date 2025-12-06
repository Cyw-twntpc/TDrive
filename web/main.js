document.addEventListener('DOMContentLoaded', () => {
    // --- DOM Elements ---
    const fileListBodyEl = document.getElementById('file-list-body');
    const uploadBtn = document.getElementById('upload-btn');
    const downloadBtn = document.getElementById('download-btn');
    const newFolderBtn = document.getElementById('new-folder-btn');
    const deleteBtn = document.getElementById('delete-btn');
    const searchInput = document.querySelector('.search-bar input');
    const searchScopeToggle = document.getElementById('search-scope-toggle');
    const saveSettingsBtn = document.getElementById('save-settings-btn');
    const concurrencyLimitSelect = document.getElementById('concurrency-limit');

    // --- Application State ---
    const AppState = {
        currentFolderId: null,
        folderTreeData: [],
        folderMap: new Map(),
        currentFolderContents: { folders: [], files: [] },
        selectedItems: [],
        currentSort: { key: 'name', order: 'asc' },
        isSearching: false,
        searchTerm: '',
        searchScope: 'all', // 'all' or 'current'
        userInfo: null,
        userAvatar: null,
    };

    // --- UI Rendering Coordinator ---
    function renderListAndSyncManager(appState) {
        UIHandler.sortAndRenderList(appState);
        TransferManager.updateMainFileListUI();
    }

    // --- Native Dialogs ---
    function loadAndApplySettings() {
        const pathDisplay = document.getElementById('default-download-path-display');
        const setPathBtn = document.getElementById('set-default-download-path-btn');
        const useDefaultToggle = document.getElementById('use-default-download-path-toggle');

        // Concurrency Limit
        const savedLimit = localStorage.getItem('concurrencyLimit');
        concurrencyLimitSelect.value = savedLimit || '3';
        TransferManager.setConcurrencyLimit(parseInt(concurrencyLimitSelect.value, 10));

        // Default Download Path Toggle State
        const useDefault = localStorage.getItem('useDefaultDownloadPath') === 'true';
        useDefaultToggle.checked = useDefault;
        
        // Path display and button state
        const savedPath = localStorage.getItem('defaultDownloadPath');
        if (savedPath) {
            pathDisplay.textContent = savedPath;
            pathDisplay.title = savedPath;
        } else {
            pathDisplay.textContent = '尚未設定';
            pathDisplay.title = '';
        }
        
        setPathBtn.disabled = !useDefault;
        pathDisplay.style.opacity = useDefault ? '1' : '0.5';
    }

    function saveSettings() {
        // Concurrency Limit
        const limit = concurrencyLimitSelect.value;
        localStorage.setItem('concurrencyLimit', limit);
        TransferManager.setConcurrencyLimit(parseInt(limit, 10));

        // Default Download Path Toggle State
        const useDefaultToggle = document.getElementById('use-default-download-path-toggle');
        localStorage.setItem('useDefaultDownloadPath', useDefaultToggle.checked);
        
        UIHandler.showAlert('設定已儲存', '您的設定已更新。', 'btn-primary');
        document.getElementById('settings-popover').classList.add('hidden');
    }

    async function loadUserDisplayInfo() {
        UIHandler.populateUserInfoPopover(AppState);
        const [info, avatar] = await Promise.all([eel.get_user_info()(), eel.get_user_avatar()()]);
        if (info && info.success) AppState.userInfo = info;
        if (avatar && avatar.success) AppState.userAvatar = avatar.avatar_base64;
        UIHandler.updateUserAvatar(AppState);
        UIHandler.populateUserInfoPopover(AppState);
    }

    async function _fetchAndRenderContents(folderId) {
        const rawContents = await eel.get_folder_contents(folderId)();
        if (rawContents && rawContents.success !== false) {
            AppState.currentFolderContents = rawContents;
        } else {
            await UIHandler.handleBackendError(rawContents);
            // On error, attempt to recover by navigating to the root folder
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder && rootFolder.id !== folderId) { // Avoid infinite loop if root is the problem
                await navigateTo(rootFolder.id);
            }
            return false; // Indicate failure
        }

        renderListAndSyncManager(AppState);
        UIHandler.renderFileTree(AppState, navigateTo); // Re-render the tree on every navigation
        UIHandler.updateBreadcrumb(AppState, navigateTo);
        UIHandler.updateTreeSelection(AppState);
        return true; // Indicate success
    }

    // --- Main Data & Navigation Logic ---
    async function refreshAll() {
        if (AppState.isSearching) {
            await handleSearch(AppState.searchTerm);
            return;
        }

        const rawFolderTree = await eel.get_folder_tree_data()();
        if (!Array.isArray(rawFolderTree)) {
            console.error("Failed to load folder tree. Backend returned:", rawFolderTree);
            return UIHandler.showAlert('嚴重錯誤', '無法載入資料夾結構，請重新整理或重新登入。');
        }

        AppState.folderTreeData = rawFolderTree;
        AppState.folderMap.clear();
        AppState.folderTreeData.forEach(f => AppState.folderMap.set(f.id, f));

        // This part needs to re-render the tree in case folders were added/deleted
        UIHandler.renderFileTree(AppState, navigateTo);

        if (AppState.currentFolderId === null) {
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder) {
                AppState.currentFolderId = rootFolder.id;
            } else {
                return UIHandler.showAlert('嚴重錯誤', '找不到根目錄，無法載入檔案。', 'btn-danger');
            }
        }
        
        await _fetchAndRenderContents(AppState.currentFolderId);
    }

    async function navigateTo(folderId) {
        if (AppState.isSearching) exitSearchMode();

        if (!AppState.folderMap.has(folderId)) {
            await UIHandler.showAlert("錯誤", "目標資料夾不存在，可能已被移動或刪除。", 'btn-primary');
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder) await navigateTo(rootFolder.id);
            return;
        }

        AppState.currentFolderId = folderId;
        await _fetchAndRenderContents(folderId);
    }

    // --- Action Handlers ---
    async function handleRename(item) {
        const { id, name, type } = item;
        const validator = async (newName) => {
            if (newName === name) return { success: true };
            return await eel.rename_item(id, newName, type)();
        };
        const newName = await UIHandler.showPrompt('重新命名', `請為 "${name}" 輸入新的名稱：`, name, validator);
        if (newName !== null) {
            await refreshAll();
        }
    }

    async function handleDownload() {
        if (AppState.selectedItems.length === 0) {
            return await UIHandler.showAlert("提示", "請先選擇要下載的項目。", 'btn-primary');
        }
        
        let destinationDir = null;
        const useDefault = localStorage.getItem('useDefaultDownloadPath') === 'true';
        
        if (useDefault) {
            destinationDir = localStorage.getItem('defaultDownloadPath');
            if (!destinationDir) {
                await UIHandler.showAlert("錯誤", "您已啟用預設下載路徑，但尚未設定路徑。", 'btn-primary');
                return;
            }
        } else {
            destinationDir = await eel.select_directory("選取下載資料夾")();
            if (!destinationDir) return; // User cancelled dialog
        }

        TransferManager.setDownloadDestination(destinationDir);
        const itemsToDownload = AppState.selectedItems.map(item => ({
            db_id: item.id,
            task_id: crypto.randomUUID(),
            type: item.type,
            name: item.name,
            size: item.raw_size
        }));
        if (itemsToDownload.length > 0) {
            itemsToDownload.forEach(item => TransferManager.addDownload(item));
            eel.download_items(itemsToDownload, destinationDir, TransferManager.getConcurrencyLimit())();
        }
    }

    async function handleDelete() {
        if (AppState.selectedItems.length === 0) {
            return await UIHandler.showAlert("提示", "請先選擇要刪除的項目。", 'btn-primary');
        }
        const confirmation = await UIHandler.showConfirm('確認刪除', `您確定要刪除這 ${AppState.selectedItems.length} 個項目嗎？<br><b>此操作無法復原。</b>`);
        if (!confirmation) return;
        const itemsToDelete = AppState.selectedItems.map(item => ({ id: item.id, type: item.type }));
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
        if (newFolderName !== null) {
            await refreshAll();
        }
    }

    async function handleUpload() {
        const localPaths = await eel.select_files(true, "選取要上傳的檔案")();
        if (!localPaths || localPaths.length === 0) return;
        const parentId = AppState.currentFolderId;
        const filesToUpload = [];
        localPaths.forEach(path => {
            const fileName = path.split(/[\\/]/).pop();
            const isDuplicate = AppState.currentFolderContents.files.some(f => f.name === fileName) || AppState.currentFolderContents.folders.some(f => f.name === fileName);
            if (isDuplicate) {
                UIHandler.showAlert('上傳失敗', `檔案夾中已存在同名項目 "${fileName}"。`);
                return;
            }
            const fileToUploadData = {
                localPath: path,
                name: fileName,
                task_id: crypto.randomUUID(), // 生成唯一的 task_id
                parentFolderId: parentId
            };
            TransferManager.addUpload(fileToUploadData);
            filesToUpload.push(fileToUploadData);

            const placeholderItem = {
                id: fileToUploadData.task_id, // 使用 task_id 作為佔位符的 ID
                name: fileName,
                modif_date: new Date().toISOString().slice(0, 10),
                size: '---',
                raw_size: 0,
                isUploading: true,
                type: 'file'
            };
            AppState.currentFolderContents.files.push(placeholderItem);
        });
        if (filesToUpload.length > 0) {
            renderListAndSyncManager(AppState);
            eel.upload_files(parentId, filesToUpload.map(f => ({ local_path: f.localPath, task_id: f.task_id })), TransferManager.getConcurrencyLimit())();
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
        const rawResults = await eel.search_db_items(baseFolderId, AppState.searchTerm)();
        document.body.style.cursor = 'default';
        if (rawResults && rawResults.success !== false) {
            AppState.currentFolderContents = rawResults;
            renderListAndSyncManager(AppState);
            UIHandler.updateBreadcrumb(AppState, navigateTo);
        } else {
            await UIHandler.handleBackendError(rawResults);
        }
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
    }

    function setupEventListeners() {
        document.getElementById('logout-btn').addEventListener('click', handleLogout);
        uploadBtn.addEventListener('click', handleUpload);
        downloadBtn.addEventListener('click', handleDownload);
        newFolderBtn.addEventListener('click', handleNewFolder);
        deleteBtn.addEventListener('click', handleDelete);
        saveSettingsBtn.addEventListener('click', saveSettings);

        document.getElementById('set-default-download-path-btn').addEventListener('click', async () => {
            const path = await eel.select_directory("選取預設下載資料夾")();
            if (path) {
                localStorage.setItem('defaultDownloadPath', path);
                loadAndApplySettings(); // Reload and display the new path
            }
        });
        
        document.getElementById('use-default-download-path-toggle').addEventListener('change', (e) => {
            const isEnabled = e.target.checked;
            document.getElementById('set-default-download-path-btn').disabled = !isEnabled;
            document.getElementById('default-download-path-display').style.opacity = isEnabled ? '1' : '0.5';
        });

        searchScopeToggle.addEventListener('click', () => {
            AppState.searchScope = (AppState.searchScope === 'all') ? 'current' : 'all';
            searchScopeToggle.textContent = (AppState.searchScope === 'all') ? '全部資料夾' : '目前資料夾';
            if (AppState.isSearching) handleSearch(AppState.searchTerm);
        });
        let searchTimeout;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => handleSearch(e.target.value), 300);
        });    
        UIHandler.setupSortableHeaders(AppState, () => renderListAndSyncManager(AppState));
        UIHandler.setupSelection(document.getElementById('file-list-container'), AppState, () => {});
        UIHandler.setupPopovers();
        fileListBodyEl.addEventListener('item-rename', e => handleRename(e.detail));
        fileListBodyEl.addEventListener('item-download', e => {
            UIHandler.selectSingleItem(e.detail.id, e.detail.type, AppState);
            handleDownload();
        });
        fileListBodyEl.addEventListener('item-delete', e => {
            AppState.selectedItems.length = 0;
            AppState.selectedItems.push(e.detail);
            handleDelete();
        });
        fileListBodyEl.addEventListener('folder-dblclick', e => navigateTo(e.detail.id));
    }

    // --- Initialization ---
    async function initialize() {
        await refreshAll();
        setupEventListeners();
        TransferManager.initialize(AppState, eel, UIHandler, refreshAll);
        loadAndApplySettings();
        loadUserDisplayInfo();
    }

    initialize();
});