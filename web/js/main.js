document.addEventListener('DOMContentLoaded', () => {
    // --- Global Dependencies & State ---
    // The various handler objects (AppState, ApiService, etc.) are expected to be available globally
    // as they are loaded via <script> tags in index.html before this script.

    // --- DOM Elements ---
    const fileListBodyEl = document.getElementById('file-list-body');
    const searchInput = document.querySelector('.search-bar input');
    const searchScopeToggle = document.getElementById('search-scope-toggle');

    // --- UI Rendering Coordinator ---
    function renderListAndSyncManager() {
        FileListHandler.sortAndRender(AppState);
        TransferManager.updateMainFileListUI();
    }
    
    // --- Main Data & Navigation Logic ---
    function onFolderContentsReady(response) {
        const { data, request_id } = response;
        if (request_id !== AppState.currentViewRequestId) {
            console.log(`Ignoring stale folder content for request_id: ${request_id}`);
            return;
        }

        UIManager.stopProgress();

        if (data && data.success !== false) {
            AppState.currentFolderContents = data;
            renderListAndSyncManager();
        } else {
            UIManager.handleBackendError(data || { message: "無法載入資料夾內容。" });
        }
    }

    function onSearchResultsReady(response) {
        const { request_id, type, data } = response;

        if (request_id !== AppState.currentViewRequestId) {
            return; // Stale search results, ignore.
        }

        if (type === 'batch') {
            // Append new results
            if (data.folders) AppState.currentFolderContents.folders.push(...data.folders);
            if (data.files) AppState.currentFolderContents.files.push(...data.files);
            // Re-sort and re-render the list progressively
            renderListAndSyncManager();
        } else if (type === 'done') {
            UIManager.stopProgress();
            UIManager.toggleSearchSpinner(false);
            console.log(`Search complete for request_id: ${request_id}`);
        } else if (type === 'error') {
            UIManager.stopProgress();
            UIManager.toggleSearchSpinner(false);
            UIManager.handleBackendError(data || { message: "搜尋時發生未知錯誤。" });
        }
    }

    async function navigateTo(folderId) {
        if (AppState.isSearching) ActionHandler.exitSearchMode();

        if (!AppState.folderMap.has(folderId)) {
            await UIModals.showAlert("錯誤", "目標資料夾不存在，可能已被移動或刪除。", 'btn-primary');
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder) await navigateTo(rootFolder.id);
            return;
        }

        const requestId = Date.now().toString();
        AppState.currentViewRequestId = requestId;
        AppState.currentFolderId = folderId;

        // --- Visual Feedback ---
        UIManager.startProgress();
        // Clear old content immediately and show loading
        AppState.currentFolderContents = { folders: [], files: [] };
        renderListAndSyncManager(); 
        // TODO: Add a proper loading state to the file list view itself

        // --- Update UI that doesn't depend on the async call ---
        FileTreeHandler.render(AppState, navigateTo);
        FileListHandler.updateBreadcrumb(AppState, navigateTo);
        FileTreeHandler.updateSelection(AppState);

        // --- Fire-and-forget API call ---
        ApiService.getFolderContents(folderId, requestId);
    }

    async function refreshAll() {
        if (AppState.isSearching) {
            // TODO: Refactor search handling in Phase 4
            ActionHandler.handleSearch(AppState.searchTerm); // Assume search handler will be updated
            return;
        }

        UIManager.startProgress();
        // The folder tree is critical and usually fast, so we can await it.
        const rawFolderTree = await ApiService.getFolderTreeData();
        
        // The main progress bar can stop after the tree is loaded,
        // as navigateTo will manage the progress bar for its specific operation.
        UIManager.stopProgress(); 

        if (!Array.isArray(rawFolderTree)) {
            console.error("Failed to load folder tree. Backend returned:", rawFolderTree);
            return UIModals.showAlert('嚴重錯誤', '無法載入資料夾結構，請重新整理或重新登入。');
        }

        AppState.folderTreeData = rawFolderTree;
        AppState.folderMap.clear();
        AppState.folderTreeData.forEach(f => AppState.folderMap.set(f.id, f));

        // Determine the root folder if no folder is selected or if the current folder was deleted
        if (AppState.currentFolderId === null || !AppState.folderMap.has(AppState.currentFolderId)) {
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder) {
                AppState.currentFolderId = rootFolder.id;
            } else {
                return UIModals.showAlert('嚴重錯誤', '找不到根目錄，無法載入檔案。', 'btn-danger');
            }
        }
        
        // Trigger the non-blocking navigation flow to refresh the contents
        navigateTo(AppState.currentFolderId);
    }
    
    async function loadUserDisplayInfo() {
        UIManager.populateUserInfoPopover(AppState);
        const [info, avatar] = await Promise.all([ApiService.getUserInfo(), ApiService.getUserAvatar()]);
        if (info && info.success) AppState.userInfo = info;
        if (avatar && avatar.success) AppState.userAvatar = avatar.avatar_base64;
        UIManager.updateUserAvatar(AppState);
        UIManager.populateUserInfoPopover(AppState);
    }

    // --- Event Listener Setup ---
    function setupEventListeners() {
        document.getElementById('logout-btn').addEventListener('click', () => ActionHandler.handleLogout());
        document.getElementById('upload-btn').addEventListener('click', () => ActionHandler.handleUpload());
        document.getElementById('download-btn').addEventListener('click', () => ActionHandler.handleDownload());
        document.getElementById('new-folder-btn').addEventListener('click', () => ActionHandler.handleNewFolder());
        document.getElementById('delete-btn').addEventListener('click', () => ActionHandler.handleDelete());
        
        searchScopeToggle.addEventListener('click', () => {
            AppState.searchScope = (AppState.searchScope === 'all') ? 'current' : 'all';
            searchScopeToggle.textContent = (AppState.searchScope === 'all') ? '全部資料夾' : '目前資料夾';
            if (AppState.isSearching) ActionHandler.handleSearch(AppState.searchTerm);
        });

        let searchTimeout;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => ActionHandler.handleSearch(e.target.value), 300);
        });    

        fileListBodyEl.addEventListener('item-rename', e => ActionHandler.handleRename(e.detail));
        fileListBodyEl.addEventListener('item-download', e => {
            const clickedItemEl = e.target.closest('.file-item');

            document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
            AppState.selectedItems.length = 0;
            AppState.selectedItems.push(e.detail);
            
            if (clickedItemEl) {
                clickedItemEl.classList.add('selected');
            }
            ActionHandler.handleDownload();
        });
        fileListBodyEl.addEventListener('item-delete', e => {
            AppState.selectedItems.length = 0;
            AppState.selectedItems.push(e.detail);
            ActionHandler.handleDelete();
        });
        fileListBodyEl.addEventListener('folder-dblclick', e => navigateTo(e.detail.id));
    }

    // --- Application Initialization ---
    async function initialize() {
        // Wait for the tdrive_bridge to be initialized by the QWebChannel
        await new Promise(resolve => {
            const interval = setInterval(() => {
                if (window.tdrive_bridge) {
                    clearInterval(interval);
                    resolve();
                }
            }, 100);
        });

        // Connect to backend signals
        if (window.tdrive_bridge.connection_status_changed) {
            window.tdrive_bridge.connection_status_changed.connect(UIManager.handleConnectionStatus);
            console.log("Connected to backend connection_status_changed signal.");
        }
        if (window.tdrive_bridge.folderContentsReady) {
            window.tdrive_bridge.folderContentsReady.connect(onFolderContentsReady);
            console.log("Connected to backend folderContentsReady signal.");
        }
        if (window.tdrive_bridge.searchResultsReady) {
            window.tdrive_bridge.searchResultsReady.connect(onSearchResultsReady);
            console.log("Connected to backend searchResultsReady signal.");
        }

        // 1. Initialize all handlers and managers
        ActionHandler.init({
            appState: AppState,
            apiService: ApiService,
            uiModals: UIModals,
            transferManager: TransferManager,
            refreshAllCallback: refreshAll,
            navigateToCallback: navigateTo,
            uiManager: UIManager, // 傳遞 UIManager
        });
        FileListHandler.init(
            () => renderListAndSyncManager(),
            () => {} // onUpdateSelection callback, can be wired up if needed
        );
        UIManager.setupPopovers();
        SettingsHandler.setupEventListeners();
        TransferManager.initialize(AppState, ApiService, UIManager, refreshAll);

        // 2. Load initial data and settings
        await refreshAll();
        SettingsHandler.loadAndApply();
        loadUserDisplayInfo();
        
        // 3. Setup main event listeners
        setupEventListeners();
    }

    // Run the app
    initialize();
});
