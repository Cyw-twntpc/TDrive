const ApiService = {
    _callBridge(functionName, ...args) {
        return new Promise((resolve, reject) => {
            if (window.tdrive_bridge && typeof window.tdrive_bridge[functionName] === 'function') {
                window.tdrive_bridge[functionName](...args, function(result) {
                    if (result && result.success === false) {
                        console.warn(`Bridge call '${functionName}' reported a failure:`, result.message);
                    }
                    resolve(result);
                });
            } else {
                console.error(`Bridge function '${functionName}' is not available.`);
                reject(new Error("Bridge is not available or function does not exist."));
            }
        });
    },

    _fireAndForget(functionName, ...args) {
        if (window.tdrive_bridge && typeof window.tdrive_bridge[functionName] === 'function') {
            window.tdrive_bridge[functionName](...args);
        } else {
            console.error(`Bridge function '${functionName}' is not available for a fire-and-forget call.`);
        }
    },

    getUserInfo: () => ApiService._callBridge('get_user_info'),
    getUserAvatar: () => ApiService._callBridge('get_user_avatar'),
    logout: () => ApiService._callBridge('logout'),

    getFolderTreeData: () => ApiService._callBridge('get_folder_tree_data'),
    getFolderContents: (folderId, requestId) => ApiService._fireAndForget('get_folder_contents', folderId, requestId),
    searchDbItems: (baseFolderId, term, requestId) => ApiService._fireAndForget('search_db_items', baseFolderId, term, requestId),

    renameItem: (id, newName, type) => ApiService._callBridge('rename_item', id, newName, type),
    deleteItems: (items) => ApiService._callBridge('delete_items', items),
    restoreItems: (items) => ApiService._callBridge('restore_items', items),
    deleteItemsPermanently: (items) => ApiService._callBridge('delete_items_permanently', items),
    emptyTrash: () => ApiService._callBridge('empty_trash'),
    getTrashItems: () => ApiService._callBridge('get_trash_items'),
    moveItems: (items, targetFolderId) => ApiService._callBridge('move_items', items, targetFolderId),
    createFolder: (parentId, folderName) => ApiService._callBridge('create_folder', parentId, folderName),

    selectDirectory: (title) => ApiService._callBridge('select_directory', title),
    selectFiles: (allowMultiple, title) => ApiService._callBridge('select_files', allowMultiple, title),
    showItemInFolder: (path) => ApiService._callBridge('show_item_in_folder', path),
    checkLocalExists: (path) => ApiService._callBridge('check_local_exists', path),

    uploadFiles: (parentId, files) => ApiService._callBridge('upload_files', parentId, files),
    uploadFolder: (parentId, folderPath, taskId) => ApiService._callBridge('upload_folder', parentId, folderPath, taskId),
    downloadItems: (items, destination) => ApiService._callBridge('download_items', items, destination),
    
    cancelTransfer: (taskId) => ApiService._callBridge('cancel_transfer', taskId),
    pauseTransfer: (taskId) => ApiService._callBridge('pause_transfer', taskId),
    resumeTransfer: (taskId) => ApiService._callBridge('resume_transfer', taskId),
    removeTransferHistory: (taskId) => ApiService._callBridge('remove_transfer_history', taskId),
    
    getIncompleteTransfers: () => ApiService._callBridge('get_incomplete_transfers'),
    getAllFileStatuses: () => ApiService._callBridge('get_all_file_statuses'),
    getInitialStats: () => ApiService._callBridge('get_initial_stats'),

    // --- Gallery API ---
    getThumbnails: (folderId) => ApiService._callBridge('get_thumbnails', folderId),
    getPreview: (fileId) => ApiService._callBridge('get_preview', fileId),
};