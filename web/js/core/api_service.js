/**
 * @fileoverview API Service module for communicating with the Python backend.
 *
 * This object encapsulates all calls to the `tdrive_bridge` QWebChannel object,
 * providing a clean, Promise-based interface for the rest of the frontend application.
 */
const ApiService = {
    /**
     * A private helper function to wrap QWebChannel's callback-based functions
     * into modern Promises, allowing for async/await syntax.
     * @param {string} functionName - The name of the function to call on the bridge.
     * @param {...any} args - The arguments to pass to the bridge function.
     * @returns {Promise<any>} A promise that resolves with the result from the backend.
     * @private
     */
    _callBridge(functionName, ...args) {
        return new Promise((resolve, reject) => {
            if (window.tdrive_bridge && typeof window.tdrive_bridge[functionName] === 'function') {
                // The Qt bridge function expects a callback as its final argument.
                window.tdrive_bridge[functionName](...args, function(result) {
                    if (result && result.success === false) {
                        // Log a warning for backend-reported failures but still resolve the promise.
                        // The caller is responsible for handling the failure case based on the result object.
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

    /**
     * A private helper for "fire-and-forget" calls that do not require a response.
     * @param {string} functionName - The name of the function to call on the bridge.
     * @param {...any} args - The arguments to pass to the bridge function.
     * @private
     */
    _fireAndForget(functionName, ...args) {
        if (window.tdrive_bridge && typeof window.tdrive_bridge[functionName] === 'function') {
            window.tdrive_bridge[functionName](...args);
        } else {
            console.error(`Bridge function '${functionName}' is not available for a fire-and-forget call.`);
        }
    },

    // --- User and Authentication ---
    getUserInfo: () => ApiService._callBridge('get_user_info'),
    getUserAvatar: () => ApiService._callBridge('get_user_avatar'),
    logout: () => ApiService._callBridge('logout'),

    // --- File & Folder Data Retrieval ---
    getFolderTreeData: () => ApiService._callBridge('get_folder_tree_data'),
    getFolderContents: (folderId, requestId) => ApiService._fireAndForget('get_folder_contents', folderId, requestId),
    searchDbItems: (baseFolderId, term, requestId) => ApiService._fireAndForget('search_db_items', baseFolderId, term, requestId),

    // --- File & Folder Actions ---
    renameItem: (id, newName, type) => ApiService._callBridge('rename_item', id, newName, type),
    /**
     * Deletes a batch of items.
     * @param {Array} items - Array of {id, type} objects.
     */
    deleteItems: (items) => ApiService._callBridge('delete_items', items),

    /**
     * Moves a batch of items to a new folder.
     * @param {Array} items - Array of {id, type} objects.
     * @param {number} targetFolderId - The destination folder ID.
     */
    moveItems: (items, targetFolderId) => ApiService._callBridge('move_items', items, targetFolderId),

    createFolder: (parentId, folderName) => ApiService._callBridge('create_folder', parentId, folderName),

    // --- Native OS Dialogs ---
    selectDirectory: (title) => ApiService._callBridge('select_directory', title),
    selectFiles: (allowMultiple, title) => ApiService._callBridge('select_files', allowMultiple, title),

    // --- File Transfers ---
    uploadFiles: (parentId, files) => ApiService._callBridge('upload_files', parentId, files),
    uploadFolder: (parentId, folderPath, taskId) => ApiService._callBridge('upload_folder', parentId, folderPath, taskId),
    downloadItems: (items, destination) => ApiService._callBridge('download_items', items, destination),
    
    // Control Methods
    cancelTransfer: (taskId) => ApiService._callBridge('cancel_transfer', taskId),
    pauseTransfer: (taskId) => ApiService._callBridge('pause_transfer', taskId),
    resumeTransfer: (taskId) => ApiService._callBridge('resume_transfer', taskId),
    
    // Startup & State Methods
    getIncompleteTransfers: () => ApiService._callBridge('get_incomplete_transfers'),
    getInitialTrafficStats: () => ApiService._callBridge('get_initial_traffic_stats'),
};