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
    
    // [新增] Nav Rail DOM Elements
    const navRail = document.getElementById('nav-rail');
    // const menuToggleBtn = document.getElementById('menu-toggle-btn'); // Removed
    const sidebarNewBtn = document.getElementById('sidebar-new-btn');
    const closeTransferPageBtn = document.getElementById('close-transfer-page-btn');

    /**
     * A central coordinator for rendering the file list and ensuring the transfer manager's UI is in sync.
     */
    function renderListAndSyncManager() {
        FileListHandler.sortAndRender(AppState);
        TransferManager.updateMainFileListUI();
    }
    
    // --- [新增] Navigation & Routing Logic ---

    /**
     * Updates the Nav Rail's expanded/collapsed state based on the current page and user interaction.
     */
    function updateNavState() {
        // If not in 'files' page (e.g. transfer page), pin the rail open.
        // Otherwise, let CSS :hover handle it (remove pinned class).
        if (AppState.currentPage !== 'files') {
            navRail.classList.add('expanded');
        } else {
            navRail.classList.remove('expanded');
        }
    }

    /**
     * Switches the main view to the specified page.
     * @param {string} pageId - The ID of the page to show (e.g., 'files', 'transfer').
     */
    function switchPage(pageId) {
        if (AppState.currentPage === pageId) return;
        
        // 1. Update State
        AppState.currentPage = pageId;
        
        // 2. Toggle Page Views
        document.querySelectorAll('.page-view').forEach(el => el.classList.add('hidden'));
        const targetPage = document.getElementById(`page-${pageId}`);
        if (targetPage) targetPage.classList.remove('hidden');

        // 3. Update Nav Rail Active Item
        document.querySelectorAll('.nav-item').forEach(item => {
            item.classList.toggle('active', item.dataset.page === pageId);
        });

        // 4. Update Nav Rail Expansion Logic
        updateNavState();
        
        // 5. Specific Page Logic
        if (pageId === 'transfer') {
             // Ensure Transfer UI is updated when entering the page
             TransferManager.updateAllUI();
        }
    }
    window.switchPage = switchPage;
    
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
        
        // --- Tree View Updates ---
        
        // 1. Calculate the path of ancestors (from Root down to Parent)
        // We want to expand everything leading UP TO the current folder.
        const targetPathIds = [];
        let tempId = folderId;
        
        // Start from parent, because we don't expand the current folder itself
        const currentFolder = AppState.folderMap.get(tempId);
        if (currentFolder && currentFolder.parent_id !== null) {
            tempId = currentFolder.parent_id;
            while(tempId) {
                targetPathIds.unshift(tempId); // Add to beginning
                const f = AppState.folderMap.get(tempId);
                tempId = f ? f.parent_id : null;
            }
        }
        
        // Ensure Root is in the path (it should be always open)
        const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
        if (rootFolder && targetPathIds.length === 0) {
             // If we are at Root, path is empty. If we are below Root, Root is in path.
             // Just ensuring consistent array structure.
             if (folderId !== rootFolder.id) targetPathIds.unshift(rootFolder.id);
             // If folderId IS root, targetPathIds remains empty (no parents to expand), which is correct.
             // Wait, if folderId is Root, we don't need to expand anything except Root itself?
             // Root is special, it's always visible.
             // But our compareAndSwitch starts from Root. 
             // If targetPathIds is empty, compareAndSwitch might close Root's children? Yes.
             // That's correct behavior: "Close everything else".
             
             // Actually, if we are AT Root, we want Root expanded? 
             // "只展開父資料夾" -> Root has no parent. So nothing to expand.
             // BUT Root's children should be visible? Yes, that means Root is expanded.
             // Root is always expanded in `render`. `close` logic shouldn't close Root unless we want to.
             // Our `compareAndSwitch` starts comparing from Root's children.
             // If targetPathIds is empty, it means we don't require any child of Root to be open.
             // So Root stays open (it's the container), but its sub-folders (L1) close. Correct.
        } else if (rootFolder && targetPathIds[0] !== rootFolder.id) {
             // Should not happen if tree is valid, but safety check.
             targetPathIds.unshift(rootFolder.id);
        }

        // 2. Sync Expansion State with Animation
        FileTreeHandler.compareAndSwitch(targetPathIds, AppState);
        
        // 3. Update Visual Selection
        FileTreeHandler.updateSelection(AppState);
        
        FileListHandler.updateBreadcrumb(AppState, navigateTo);

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
        
        // Render the tree structure first (static init)
        FileTreeHandler.render(AppState, navigateTo);

        // Trigger the navigation flow to refresh the file list view and sync tree state.
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
        // [MODIFIED] Removed listeners for upload-btn and new-folder-btn as they are removed from HTML.
        // Their functionality is now handled by sidebarNewBtn.
        document.getElementById('download-btn').addEventListener('click', () => ActionHandler.handleDownload());
        document.getElementById('move-btn').addEventListener('click', () => ActionHandler.handleMove());
        document.getElementById('delete-btn').addEventListener('click', () => ActionHandler.handleDelete());
        
        // [新增] Sidebar 'New' Button Logic
        // For now, let's map it to Upload as primary, or show a context menu. 
        // Based on user request "upload/new folder", we can trigger the upload dialog or a choice.
        // Let's default to Upload for now, or maybe a simple choice menu if UIModals supported it.
        // Given existing handlers, we'll trigger Upload. Ideally, this should open a small menu.
        sidebarNewBtn.addEventListener('click', (e) => {
             // Simple fallback: Left click = Upload, Right click (context) = New Folder?
             // Or just trigger Upload for now as it's the most common action.
             ActionHandler.handleUpload();
        });
        // Allow right click on New button to create folder?
        sidebarNewBtn.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            ActionHandler.handleNewFolder();
        });

        // [新增] Nav Rail Event Listeners
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', () => {
                if (item.classList.contains('disabled')) return;
                switchPage(item.dataset.page);
            });
        });

        // [新增] Click to Dismiss Nav Rail (in files view)
        navRail.addEventListener('click', (e) => {
            // Only applicable in 'files' view (where it auto-collapses)
            if (AppState.currentPage !== 'files') return;

            // If clicked on a nav-item, do nothing (let item click handler work)
            if (e.target.closest('.nav-item')) return;

            // Otherwise (clicked empty space), force collapse
            navRail.classList.add('temp-disabled');

            // Restore functionality only when the user intentionally enters the rail area again.
            // Using 'once: true' ensures this listener is self-cleaning.
            navRail.addEventListener('mouseenter', () => {
                navRail.classList.remove('temp-disabled');
            }, { once: true });
        });
        
        if (closeTransferPageBtn) {
            closeTransferPageBtn.addEventListener('click', () => switchPage('files'));
        }

        // Sidebar Transfer Status Click -> Switch to Transfer Page
        const sidebarStatus = document.getElementById('sidebar-transfer-status');
        if (sidebarStatus) {
            // Remove old listener if any (managed in TransferManager, but we override behavior here or there)
            // TransferManager adds a listener to show modal. We need to intercept or change TransferManager.
            // Since TransferManager is initialized LATER, its listener will be added later.
            // We should modify TransferManager to call switchPage instead of showModal.
        }

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
