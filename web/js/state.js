// --- Application State ---
const AppState = {
    currentFolderId: null,
    currentViewRequestId: null, // 用於追蹤最新視圖請求的 ID
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
