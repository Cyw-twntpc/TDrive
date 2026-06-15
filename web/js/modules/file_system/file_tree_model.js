const FileTreeModel = {
    expandedFolders: {},

    _findStateNode(id) {
        if (FileTreeModel.expandedFolders[id]) return FileTreeModel.expandedFolders[id];
        
        const queue = Object.values(FileTreeModel.expandedFolders);
        while (queue.length > 0) {
            const current = queue.shift();
            if (current[id]) return current[id];
            Object.values(current).forEach(child => queue.push(child));
        }
        return null;
    },

    _isFolderExpanded(id) {
        if (FileTreeModel.expandedFolders[id]) return true;
        
        const queue = Object.values(FileTreeModel.expandedFolders);
        while (queue.length > 0) {
            const current = queue.shift();
            if (current[id]) return true;
            Object.values(current).forEach(child => queue.push(child));
        }
        return false;
    },

};
