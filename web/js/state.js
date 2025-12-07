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
