document.addEventListener('DOMContentLoaded', () => {
    const fileListBodyEl = document.getElementById('file-list-body');
    const searchInput = document.querySelector('.search-bar input');
    const searchScopeToggle = document.getElementById('search-scope-toggle');
    const navRail = document.getElementById('nav-rail');
    const sidebarNewBtn = document.getElementById('sidebar-new-btn');
    const closeTransferPageBtn = document.getElementById('close-transfer-page-btn');

    function renderListAndSyncManager() {
        FileListHandler.sortAndRender(AppState);
        TransferManager.updateMainFileListUI();
    }
    
    function updateNavState() {
        if (AppState.currentPage !== 'files') {
            navRail.classList.add('expanded');
        } else {
            navRail.classList.remove('expanded');
        }
    }

    function switchPage(pageId) {
        if (AppState.currentPage === pageId) return;
        
        AppState.currentPage = pageId;
        
        document.querySelectorAll('.page-view').forEach(el => el.classList.add('hidden'));
        const targetPage = document.getElementById(`page-${pageId}`);
        if (targetPage) targetPage.classList.remove('hidden');

        document.querySelectorAll('.nav-item').forEach(item => {
            item.classList.toggle('active', item.dataset.page === pageId);
        });

        updateNavState();
        
        if (pageId === 'transfer') {
             TransferManager.updateAllUI();
        } else if (pageId === 'trash') {
             TrashHandler.loadTrashItems();
        }
    }
    window.switchPage = switchPage;
    
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
            return;
        }

        if (type === 'batch') {
            if (data.folders) AppState.currentFolderContents.folders.push(...data.folders);
            if (data.files) AppState.currentFolderContents.files.push(...data.files);
            renderListAndSyncManager();
        } else if (type === 'done') {
            UIManager.stopProgress();
            console.log(`Search complete for request_id: ${request_id}`);
        } else if (type === 'error') {
            UIManager.stopProgress();
            UIManager.handleBackendError(data || { message: "搜尋過程中發生未知錯誤。" });
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

        UIManager.startProgress();
        AppState.currentFolderContents = { folders: [], files: [] };
        renderListAndSyncManager(); 
        
        const targetPathIds = [];
        let tempId = folderId;
        
        const currentFolder = AppState.folderMap.get(tempId);
        if (currentFolder && currentFolder.parent_id !== null) {
            tempId = currentFolder.parent_id;
            while(tempId) {
                targetPathIds.unshift(tempId);
                const f = AppState.folderMap.get(tempId);
                tempId = f ? f.parent_id : null;
            }
        }
        
        const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
        if (rootFolder && targetPathIds.length === 0) {
             if (folderId !== rootFolder.id) targetPathIds.unshift(rootFolder.id);
        } else if (rootFolder && targetPathIds[0] !== rootFolder.id) {
             targetPathIds.unshift(rootFolder.id);
        }

        FileTreeHandler.compareAndSwitch(targetPathIds, AppState);
        FileTreeHandler.updateSelection(AppState);
        FileListHandler.updateBreadcrumb(AppState, navigateTo);

        ApiService.getFolderContents(folderId, requestId);
    }
    window.navigateTo = navigateTo;

    async function refreshAll() {
        if (AppState.isSearching) {
            ActionHandler.handleSearch(AppState.searchTerm);
            return;
        }

        UIManager.startProgress();
        
        const rawFolderTree = await ApiService.getFolderTreeData();
        
        UIManager.stopProgress(); 

        if (!Array.isArray(rawFolderTree)) {
            console.error("Failed to load folder tree. Backend returned:", rawFolderTree);
            return UIModals.showAlert('嚴重錯誤', '無法載入資料夾結構，請重新整理或重新登入。', 'btn-danger');
        }

        AppState.folderTreeData = rawFolderTree;
        AppState.folderMap.clear();
        AppState.folderTreeData.forEach(f => AppState.folderMap.set(f.id, f));

        if (AppState.currentFolderId === null || !AppState.folderMap.has(AppState.currentFolderId)) {
            const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
            if (rootFolder) {
                AppState.currentFolderId = rootFolder.id;
            } else {
                return UIModals.showAlert('嚴重錯誤', '找不到根目錄。', 'btn-danger');
            }
        }
        
        FileTreeHandler.render(AppState, navigateTo);
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

    function setupEventListeners() {
        document.getElementById('logout-btn').addEventListener('click', () => ActionHandler.handleLogout());
        document.getElementById('download-btn').addEventListener('click', () => ActionHandler.handleDownload());
        document.getElementById('move-btn').addEventListener('click', () => ActionHandler.handleMove());
        document.getElementById('delete-btn').addEventListener('click', () => ActionHandler.handleDelete());
        
        document.getElementById('new-upload-file-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelectorAll('.popover').forEach(p => p.classList.add('hidden'));
            ActionHandler.handleFileUpload();
        });
        document.getElementById('new-upload-folder-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelectorAll('.popover').forEach(p => p.classList.add('hidden'));
            ActionHandler.handleFolderUpload();
        });
        document.getElementById('new-create-folder-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelectorAll('.popover').forEach(p => p.classList.add('hidden'));
            ActionHandler.handleNewFolder();
        });

        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', () => {
                if (item.classList.contains('disabled')) return;
                switchPage(item.dataset.page);
            });
        });

        navRail.addEventListener('click', (e) => {
            if (AppState.currentPage !== 'files') return;
            if (e.target.closest('.nav-item')) return;

            navRail.classList.add('temp-disabled');
            navRail.addEventListener('mouseenter', () => {
                navRail.classList.remove('temp-disabled');
            }, { once: true });
        });
        
        if (closeTransferPageBtn) {
            closeTransferPageBtn.addEventListener('click', () => switchPage('files'));
        }

        const sidebarStatus = document.getElementById('sidebar-transfer-status');
        if (sidebarStatus) {
        }

        searchScopeToggle.addEventListener('click', () => {
            AppState.searchScope = (AppState.searchScope === 'all') ? 'current' : 'all';
            searchScopeToggle.textContent = (AppState.searchScope === 'all') ? '所有資料夾' : '目前資料夾';
            if (AppState.isSearching) ActionHandler.handleSearch(AppState.searchTerm);
        });

        let searchTimeout;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => ActionHandler.handleSearch(e.target.value), 300);
        });    

        fileListBodyEl.addEventListener('item-rename', e => ActionHandler.handleRename(e.detail));
        fileListBodyEl.addEventListener('item-move', e => {
            document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
            e.target.closest('.file-item')?.classList.add('selected');
            AppState.selectedItems = [e.detail];
            ActionHandler.handleMove();
        });
        fileListBodyEl.addEventListener('item-download', e => {
            document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
            e.target.closest('.file-item')?.classList.add('selected');
            AppState.selectedItems = [e.detail];
            ActionHandler.handleDownload();
        });
        fileListBodyEl.addEventListener('item-delete', e => {
            AppState.selectedItems = [e.detail];
            ActionHandler.handleDelete();
        });
        fileListBodyEl.addEventListener('folder-dblclick', e => navigateTo(e.detail.id));
    }

    async function initialize() {
        const tooltip = document.createElement('div');
        tooltip.id = 'tree-tooltip';
        tooltip.style.display = 'none';
        document.body.appendChild(tooltip);

        await new Promise(resolve => {
            const interval = setInterval(() => {
                if (window.tdrive_bridge) {
                    clearInterval(interval);
                    resolve();
                }
            }, 50);
        });

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

        ActionHandler.init({
            appState: AppState, apiService: ApiService, uiModals: UIModals,
            transferManager: TransferManager, refreshAllCallback: refreshAll,
            navigateToCallback: navigateTo, uiManager: UIManager
        });
        FileListHandler.init(renderListAndSyncManager, () => {});
        TrashHandler.init();
        UIManager.setupPopovers();
        SettingsHandler.setupEventListeners();
        
        TransferManager.initialize(AppState, ApiService, UIManager, async () => {
             await refreshAll();
        });

        await refreshAll();
        SettingsHandler.loadAndApply();
        loadUserDisplayInfo();
        
        setupEventListeners();
        
        setupGlobalDragAndDrop();
    }

    initialize();
});

function setupGlobalDragAndDrop() {
    const dropZone = document.body;

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.stopPropagation();
    });

    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        e.stopPropagation();
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();

        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            ActionHandler.handleFileUpload(e.dataTransfer.files);
        }
    });
}