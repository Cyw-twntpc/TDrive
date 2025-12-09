const ApiService = {
    // The 'expose' function is no longer needed as QWebChannel doesn't use it.
    
    // Helper function to wrap QWebChannel calls in Promises
    _callBridge(functionName, ...args) {
        return new Promise((resolve, reject) => {
            if (window.tdrive_bridge) {
                // The actual function call, with a callback as the last argument
                window.tdrive_bridge[functionName](...args, function(result) {
                    // QJsonValue from Python is automatically converted to a JS object/value
                    if (result && result.success === false) {
                        // Log the error but resolve with the error object so the UI can handle it
                        console.warn(`Bridge call '${functionName}' returned an error:`, result.message);
                    }
                    resolve(result);
                });
            } else {
                // This can happen if the channel is not ready yet.
                reject(new Error("Bridge is not available."));
            }
        });
    },

    // --- User and Auth ---
    getUserInfo: () => ApiService._callBridge('get_user_info'),
    getUserAvatar: () => ApiService._callBridge('get_user_avatar'),
    logout: () => ApiService._callBridge('logout'),

    // --- File & Folder Data ---
    getFolderTreeData: () => ApiService._callBridge('get_folder_tree_data'),
    getFolderContents: (folderId) => ApiService._callBridge('get_folder_contents', folderId),
    searchDbItems: (baseFolderId, term) => ApiService._callBridge('search_db_items', baseFolderId, term),

    // --- File & Folder Actions ---
    renameItem: (id, newName, type) => ApiService._callBridge('rename_item', id, newName, type),
    deleteItems: (items) => ApiService._callBridge('delete_items', items),
    createFolder: (parentId, folderName) => ApiService._callBridge('create_folder', parentId, folderName),

    // --- Native Dialogs ---
    selectDirectory: (title) => ApiService._callBridge('select_directory', title),
    selectFiles: (allowMultiple, title) => ApiService._callBridge('select_files', allowMultiple, title),

    // --- Transfers ---
    // Note: The progress callback mechanism will need to be redesigned with Qt signals.
    // For now, these functions just start the process.
    uploadFiles: (parentId, files, concurrency) => ApiService._callBridge('upload_files', parentId, files, concurrency),
    downloadItems: (items, destination, concurrency) => ApiService._callBridge('download_items', items, destination, concurrency),
    cancelTransfer: (taskId) => ApiService._callBridge('cancel_transfer', taskId),
};

