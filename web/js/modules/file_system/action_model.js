const ActionModel = {

    isValidMove(items, targetFolderId) {
        if (!items || items.length === 0) return false;

        const targetId = Number(targetFolderId);
        const isSelf = items.some(item => item.type === 'folder' && item.id === targetId);
        if (isSelf) return false;

        const isCircular = items.some(item => {
            if (item.type !== 'folder') return false;
            let current = targetId;

            let depth = 0;
            while (current && depth < 100) {
                if (current === item.id) return true;
                const folder = ActionController._appState.folderMap.get(current);
                current = folder ? folder.parent_id : null;
                depth++;
            }
            return false;
        });
        if (isCircular) return false;
        return true;
    },

};
