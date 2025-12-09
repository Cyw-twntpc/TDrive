const ApiService = {
    // The 'expose' function is no longer needed as QWebChannel doesn't use it.
    
    // Helper for calls that expect a return value via a callback (wrapped in a Promise)
    _callBridge(functionName, ...args) {
        return new Promise((resolve, reject) => {
            if (window.tdrive_bridge) {
                // The actual function call, with a callback as the last argument
                window.tdrive_bridge[functionName](...args, function(result) {
                    if (result && result.success === false) {
                        console.warn(`Bridge call '${functionName}' returned an error:`, result.message);
                    }
                    resolve(result);
                });
            } else {
                reject(new Error("Bridge is not available."));
            }
        });
    },

    // Helper for fire-and-forget calls that do not have a direct return value
    _fireAndForget(functionName, ...args) {
        if (window.tdrive_bridge && typeof window.tdrive_bridge[functionName] === 'function') {
            window.tdrive_bridge[functionName](...args);
        } else {
            console.error(`Bridge function '${functionName}' is not available for fire-and-forget call.`);
        }
    },

    // --- User and Auth ---
    getUserInfo: () => ApiService._callBridge('get_user_info'),
    getUserAvatar: () => ApiService._callBridge('get_user_avatar'),
    logout: () => ApiService._callBridge('logout'),

    // --- File & Folder Data ---
    getFolderTreeData: () => ApiService._callBridge('get_folder_tree_data'),
    getFolderContents: (folderId, requestId) => ApiService._fireAndForget('get_folder_contents', folderId, requestId),
    searchDbItems: (baseFolderId, term, requestId) => ApiService._fireAndForget('search_db_items', baseFolderId, term, requestId),

    // --- File & Folder Actions ---
    renameItem: (id, newName, type) => ApiService._callBridge('rename_item', id, newName, type),
    deleteItems: (items) => ApiService._callBridge('delete_items', items),
    createFolder: (parentId, folderName) => ApiService._callBridge('create_folder', parentId, folderName),

    // --- Native Dialogs ---
    selectDirectory: (title) => ApiService._callBridge('select_directory', title),
    selectFiles: (allowMultiple, title) => ApiService._callBridge('select_files', allowMultiple, title),

    // --- Transfers ---
    uploadFiles: (parentId, files, concurrency) => ApiService._callBridge('upload_files', parentId, files, concurrency),
    downloadItems: (items, destination, concurrency) => ApiService._callBridge('download_items', items, destination, concurrency),
    cancelTransfer: (taskId) => ApiService._callBridge('cancel_transfer', taskId),
};

