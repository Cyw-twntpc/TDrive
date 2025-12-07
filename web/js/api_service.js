const ApiService = {
    // --- Initialization ---
    expose(functionName, alias) {
        eel.expose(functionName, alias);
    },

    // --- User and Auth ---
    getUserInfo: () => eel.get_user_info()(),
    getUserAvatar: () => eel.get_user_avatar()(),
    logout: () => eel.logout()(),

    // --- File & Folder Data ---
    getFolderTreeData: () => eel.get_folder_tree_data()(),
    getFolderContents: (folderId) => eel.get_folder_contents(folderId)(),
    searchDbItems: (baseFolderId, term) => eel.search_db_items(baseFolderId, term)(),

    // --- File & Folder Actions ---
    renameItem: (id, newName, type) => eel.rename_item(id, newName, type)(),
    deleteItems: (items) => eel.delete_items(items)(),
    createFolder: (parentId, folderName) => eel.create_folder(parentId, folderName)(),

    // --- Native Dialogs ---
    selectDirectory: (title) => eel.select_directory(title)(),
    selectFiles: (allowMultiple, title) => eel.select_files(allowMultiple, title)(),

    // --- Transfers ---
    uploadFiles: (parentId, files, concurrency) => eel.upload_files(parentId, files, concurrency)(),
    downloadItems: (items, destination, concurrency) => eel.download_items(items, destination, concurrency)(),
    cancelTransfer: (taskId) => eel.cancel_transfer(taskId)(),
};
