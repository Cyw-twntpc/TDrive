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

    _callBridgeSignal(functionName, signalName, ...args) {
        return new Promise((resolve, reject) => {
            if (window.tdrive_bridge && window.tdrive_bridge[functionName] && window.tdrive_bridge[signalName]) {
                const requestId = Date.now().toString(36) + Math.random().toString(36).substr(2);
                const handler = (result) => {
                    if (result && result.request_id === requestId) {
                        window.tdrive_bridge[signalName].disconnect(handler);
                        if (result.data && result.data.success === false) {
                            console.warn(`Bridge call '${functionName}' reported a failure:`, result.data.message);
                        }
                        resolve(result.data);
                    }
                };
                window.tdrive_bridge[signalName].connect(handler);
                window.tdrive_bridge[functionName](...args, requestId);
            } else {
                console.error(`Bridge function '${functionName}' or signal '${signalName}' is not available.`);
                reject(new Error("Bridge or Signal is not available."));
            }
        });
    },

    getUserInfo: () => ApiService._callBridge('get_user_info'),
    getUserAvatar: () => ApiService._callBridge('get_user_avatar'),
    logout: () => ApiService._callBridge('logout'),

    getFolderTreeData: () => ApiService._callBridge('get_folder_tree_data'),
    getFolderContents: (folderId) => ApiService._callBridgeSignal('get_folder_contents', 'queryResultReady', folderId),
    searchDbItems: (baseFolderId, term, onBatch) => {
        return new Promise((resolve, reject) => {
            if (window.tdrive_bridge && window.tdrive_bridge.search_db_items && window.tdrive_bridge.queryResultReady) {
                const requestId = Date.now().toString(36) + Math.random().toString(36).substr(2);
                const handler = (result) => {
                    if (result && result.request_id === requestId) {
                        if (result.type === 'batch') {
                            if (onBatch) onBatch(result.data);
                        } else if (result.type === 'done') {
                            window.tdrive_bridge.queryResultReady.disconnect(handler);
                            resolve({ success: true, request_id: requestId });
                        } else if (result.type === 'error') {
                            window.tdrive_bridge.queryResultReady.disconnect(handler);
                            resolve({ success: false, message: result.data?.message || window.t('dialog.search_err') });
                        }
                    }
                };
                window.tdrive_bridge.queryResultReady.connect(handler);
                window.tdrive_bridge.search_db_items(baseFolderId, term, requestId);
            } else {
                reject(new Error("Bridge or Signal is not available."));
            }
        });
    },

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
    getThumbnails: (folderId) => ApiService._callBridgeSignal('get_thumbnails', 'queryResultReady', folderId),
    getPreview: (fileId) => ApiService._callBridgeSignal('get_preview', 'queryResultReady', fileId),
    playVideo: (fileId) => ApiService._callBridgeSignal('play_video', 'queryResultReady', fileId),
};