/**
 * @fileoverview The main entry point for the TDrive application's frontend.
 * This script initializes all UI modules, sets up the QWebChannel bridge,
 * fetches initial data, and wires up all global event listeners.
 */

document.addEventListener('DOMContentLoaded', () => {
    // --- Global Dependencies & State ---
    // Modules like AppState, ApiService, etc., are loaded via <script> tags in index.html,
    // making them available in the global scope.

    // --- DOM Element References ---
    const fileListBodyEl = document.getElementById('file-list-body');
    const searchInput = document.querySelector('.search-bar input');
    const searchScopeToggle = document.getElementById('search-scope-toggle');

    /**
     * A central coordinator for rendering the file list and ensuring the transfer manager's UI is in sync.
     */
    function renderListAndSyncManager() {
        FileListHandler.sortAndRender(AppState);
        TransferManager.updateMainFileListUI();
    }
    
    // --- Data Handling & Navigation ---

    /**
     * Callback for when folder contents are received from the backend.
     * @param {object} response - The response object from the backend signal.
     */
    function onFolderContentsReady(response) {
        const { data, request_id } = response;
        // Ignore responses that don't match the latest request to prevent race conditions.
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

    /**
     * Callback for when search results are streamed from the backend.
     * @param {object} response - The response object from the backend signal.
     */
    function onSearchResultsReady(response) {
        const { request_id, type, data } = response;

        if (request_id !== AppState.currentViewRequestId) {
            return; // Ignore stale search results.
        }

        if (type === 'batch') {
            if (data.folders) AppState.currentFolderContents.folders.push(...data.folders);
            if (data.files) AppState.currentFolderContents.files.push(...data.files);
            renderListAndSyncManager(); // Progressively render new results.
        } else if (type === 'done') {
            UIManager.stopProgress();
            console.log(`Search complete for request_id: ${request_id}`);
        } else if (type === 'error') {
            UIManager.stopProgress();
            UIManager.handleBackendError(data || { message: "搜尋過程中發生未知錯誤。" });
        }
    }

    /**
     * Navigates to a specific folder, updating the application state and fetching new content.
     * @param {number} folderId - The ID of the folder to navigate to.
     */
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

        // Provide immediate visual feedback by clearing the view and showing a progress bar.
        UIManager.startProgress();
        AppState.currentFolderContents = { folders: [], files: [] };
        renderListAndSyncManager(); 
        
        // Update UI components that don't depend on the async call.
        FileTreeHandler.render(AppState, navigateTo);
        FileListHandler.updateBreadcrumb(AppState, navigateTo);
        FileTreeHandler.updateSelection(AppState);

        // Fetch folder contents in a fire-and-forget manner. The view will be updated by the onFolderContentsReady callback.
        ApiService.getFolderContents(folderId, requestId);
    }

    /**
     * Performs a full refresh of the application's data and UI.
     */
    async function refreshAll() {
        if (AppState.isSearching) {
            ActionHandler.handleSearch(AppState.searchTerm);
            return;
        }

        UIManager.startProgress();
        
        const rawFolderTree = await ApiService.getFolderTreeData();
        
        // The main progress bar can stop here; `navigateTo` will manage its own progress indication.
        UIManager.stopProgress(); 

        if (!Array.isArray(rawFolderTree)) {
            console.error("Failed to load folder tree. Backend returned:", rawFolderTree);
            return UIModals.showAlert('嚴重錯誤', '無法載入資料夾結構，請重新整理或重新登入。', 'btn-danger');
        }

        // Rebuild the folder tree data structures.
        AppState.folderTreeData = rawFolderTree;
        AppState.folderMap.clear();
        AppState.folderTreeData.forEach(f => AppState.folderMap.set(f.id, f));

        // If the current folder no longer exists, navigate to the root.
        if (AppState.currentFolderId === null || !AppState.folderMap.has(AppState.currentFolderId)) {
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder) {
                AppState.currentFolderId = rootFolder.id;
            } else {
                return UIModals.showAlert('嚴重錯誤', '找不到根目錄。', 'btn-danger');
            }
        }
        
        // Trigger the navigation flow to refresh the file list view.
        navigateTo(AppState.currentFolderId);
    }
    
    /**
     * Fetches and displays the user's name and avatar.
     */
    async function loadUserDisplayInfo() {
        UIManager.populateUserInfoPopover(AppState);
        const [info, avatar] = await Promise.all([ApiService.getUserInfo(), ApiService.getUserAvatar()]);
        if (info && info.success) AppState.userInfo = info;
        if (avatar && avatar.success) AppState.userAvatar = avatar.avatar_base64;
        UIManager.updateUserAvatar(AppState);
        UIManager.populateUserInfoPopover(AppState);
    }

    /**
     * Sets up global event listeners for UI actions.
     */
    function setupEventListeners() {
        document.getElementById('logout-btn').addEventListener('click', () => ActionHandler.handleLogout());
        document.getElementById('upload-btn').addEventListener('click', () => ActionHandler.handleUpload());
        document.getElementById('download-btn').addEventListener('click', () => ActionHandler.handleDownload());
        document.getElementById('move-btn').addEventListener('click', () => ActionHandler.handleMove());
        document.getElementById('new-folder-btn').addEventListener('click', () => ActionHandler.handleNewFolder());
        document.getElementById('delete-btn').addEventListener('click', () => ActionHandler.handleDelete());
        
        searchScopeToggle.addEventListener('click', () => {
            AppState.searchScope = (AppState.searchScope === 'all') ? 'current' : 'all';
            searchScopeToggle.textContent = (AppState.searchScope === 'all') ? '所有資料夾' : '目前資料夾';
            if (AppState.isSearching) ActionHandler.handleSearch(AppState.searchTerm);
        });

        // Debounce search input to avoid excessive API calls.
        let searchTimeout;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => ActionHandler.handleSearch(e.target.value), 300);
        });    

        // Use event delegation to handle actions on dynamically created file list items.
        fileListBodyEl.addEventListener('item-rename', e => ActionHandler.handleRename(e.detail));
        fileListBodyEl.addEventListener('item-move', e => {
            // Select only the clicked item for a direct move action.
            document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
            e.target.closest('.file-item')?.classList.add('selected');
            AppState.selectedItems = [e.detail];
            ActionHandler.handleMove();
        });
        fileListBodyEl.addEventListener('item-download', e => {
            // Select only the clicked item for a direct download action.
            document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
            e.target.closest('.file-item')?.classList.add('selected');
            AppState.selectedItems = [e.detail];
            ActionHandler.handleDownload();
        });
        fileListBodyEl.addEventListener('item-delete', e => {
            // Select only the clicked item for a direct delete action.
            AppState.selectedItems = [e.detail];
            ActionHandler.handleDelete();
        });
        fileListBodyEl.addEventListener('folder-dblclick', e => navigateTo(e.detail.id));
    }

    /**
     * The main application initialization sequence.
     */
    async function initialize() {
        // Create the global tooltip element for the file tree
        const tooltip = document.createElement('div');
        tooltip.id = 'tree-tooltip';
        tooltip.style.display = 'none';
        document.body.appendChild(tooltip);

        // 1. Wait for the QWebChannel bridge to become available.
        await new Promise(resolve => {
            const interval = setInterval(() => {
                if (window.tdrive_bridge) {
                    clearInterval(interval);
                    resolve();
                }
            }, 50);
        });

        // 2. Connect to signals from the backend.
        if (window.tdrive_bridge.connection_status_changed) {
            window.tdrive_bridge.connection_status_changed.connect(UIManager.handleConnectionStatus);
        }
        if (window.tdrive_bridge.folderContentsReady) {
            window.tdrive_bridge.folderContentsReady.connect(onFolderContentsReady);
        }
        if (window.tdrive_bridge.searchResultsReady) {
            window.tdrive_bridge.searchResultsReady.connect(onSearchResultsReady);
        }
        console.log("Successfully connected to backend signals.");

        // 3. Initialize all frontend modules with their dependencies.
        ActionHandler.init({
            appState: AppState, apiService: ApiService, uiModals: UIModals,
            transferManager: TransferManager, refreshAllCallback: refreshAll,
            navigateToCallback: navigateTo, uiManager: UIManager
        });
        FileListHandler.init(renderListAndSyncManager, () => {});
        UIManager.setupPopovers();
        SettingsHandler.setupEventListeners();
        TransferManager.initialize(AppState, ApiService, UIManager, refreshAll);

        // 4. Fetch initial data and render the UI.
        await refreshAll();
        SettingsHandler.loadAndApply();
        loadUserDisplayInfo();
        
        // 5. Set up final global event listeners.
        setupEventListeners();
    }

    initialize();
});
