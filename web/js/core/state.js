const AppState = {
    currentFolderId: null,
    currentViewRequestId: null, 
    folderTreeData: [],
    folderMap: new Map(),
    currentFolderContents: { folders: [], files: [] },
    selectedItems: [],
    currentSort: { key: 'name', order: 'asc' },
    isSearching: false,
    searchTerm: '',
    searchScope: 'all',
    userInfo: null,
    userAvatar: null,
    currentPage: 'files',
    
    trashItems: [],
    trashSort: { key: 'trashed_date', order: 'desc' },

    isDragging: false,
    draggedItems: [],
    dragHoverTimer: null,
};
