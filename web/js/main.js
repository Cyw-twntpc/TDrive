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
    async function _fetchAndRenderContents(folderId) {
        const rawContents = await ApiService.getFolderContents(folderId);
        if (rawContents && rawContents.success !== false) {
            AppState.currentFolderContents = rawContents;
        } else {
            await UIManager.handleBackendError(rawContents);
            // On error, attempt to recover by navigating to the root folder
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder && rootFolder.id !== folderId) { // Avoid infinite loop if root is the problem
                await navigateTo(rootFolder.id);
            }
            return false; // Indicate failure
        }

        renderListAndSyncManager();
        FileTreeHandler.render(AppState, navigateTo);
        FileListHandler.updateBreadcrumb(AppState, navigateTo);
        FileTreeHandler.updateSelection(AppState);
        return true; // Indicate success
    }
    
    async function refreshAll() {
        if (AppState.isSearching) {
            await ActionHandler.handleSearch(AppState.searchTerm);
            return;
        }

        const rawFolderTree = await ApiService.getFolderTreeData();
        if (!Array.isArray(rawFolderTree)) {
            console.error("Failed to load folder tree. Backend returned:", rawFolderTree);
            return UIModals.showAlert('嚴重錯誤', '無法載入資料夾結構，請重新整理或重新登入。');
        }

        AppState.folderTreeData = rawFolderTree;
        AppState.folderMap.clear();
        AppState.folderTreeData.forEach(f => AppState.folderMap.set(f.id, f));

        FileTreeHandler.render(AppState, navigateTo);

        if (AppState.currentFolderId === null) {
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder) {
                AppState.currentFolderId = rootFolder.id;
            } else {
                return UIModals.showAlert('嚴重錯誤', '找不到根目錄，無法載入檔案。', 'btn-danger');
            }
        }
        
        await _fetchAndRenderContents(AppState.currentFolderId);
    }
    
    async function navigateTo(folderId) {
        if (AppState.isSearching) ActionHandler.exitSearchMode();

        if (!AppState.folderMap.has(folderId)) {
            await UIModals.showAlert("錯誤", "目標資料夾不存在，可能已被移動或刪除。", 'btn-primary');
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder) await navigateTo(rootFolder.id);
            return;
        }

        AppState.currentFolderId = folderId;
        await _fetchAndRenderContents(folderId);
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
        TransferManager.initialize(AppState, eel, UIManager, refreshAll);

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
