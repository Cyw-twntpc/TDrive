const FileTreeHandler = {

    render(AppState, navigateTo) {
        // 1. Capture currently expanded folder IDs to maintain state across renders
        const previouslyExpanded = new Set();
        FileTreeView.fileTreeEl.querySelectorAll('.subtree-wrapper.is-expanded').forEach(el => {
            const li = el.closest('li');
            if (li && li.dataset.id) {
                previouslyExpanded.add(Number(li.dataset.id));
            }
        });

        FileTreeView.fileTreeEl.innerHTML = '';
        if (!AppState.folderTreeData || AppState.folderTreeData.length === 0) {
            return;
        }

        // Pre-process the flat list into a map for efficient child lookup.
        const childrenOf = new Map();
        AppState.folderTreeData.forEach(folder => {
            const parentId = folder.parent_id;
            if (!childrenOf.has(parentId)) {
                childrenOf.set(parentId, []);
            }
            childrenOf.get(parentId).push(folder.id);
        });
    
        const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
        const pendingExpansions = [];
        const pendingCollapses = [];
    
        if (rootFolder) {
            // Ensure root is always expanded in the state
            if (!FileTreeModel.expandedFolders[rootFolder.id]) {
                FileTreeModel.expandedFolders[rootFolder.id] = {};
            }

            // 2. Pass the ancestor set down the recursive build process.
            const rootItem = FileTreeView._buildTreeItem(rootFolder, AppState, navigateTo, childrenOf, previouslyExpanded, pendingExpansions, pendingCollapses, true, 0);
            const rootUl = document.createElement('ul');
            rootUl.appendChild(rootItem);
            FileTreeView.fileTreeEl.appendChild(rootUl);
        } else {
            console.error("Could not find root folder to render file tree.");
        }

        // 3. Trigger animations for newly expanded and collapsed folders
        if (pendingExpansions.length > 0 || pendingCollapses.length > 0) {
            // Force reflow
            void FileTreeView.fileTreeEl.offsetHeight; 
            
            requestAnimationFrame(() => {
                pendingExpansions.forEach(({ wrapper, toggle }) => {
                    wrapper.classList.add('is-expanded');
                    toggle.classList.add('open');
                });
                pendingCollapses.forEach(({ wrapper, toggle }) => {
                    wrapper.classList.remove('is-expanded');
                    toggle.classList.remove('open');
                });
            });
        }
    },

    open(id, parentId) {
        const li = FileTreeView.fileTreeEl.querySelector(`li[data-id="${id}"]`);
        if (li) {
            const subtreeWrapper = li.querySelector('.subtree-wrapper');
            const toggle = li.querySelector('.folder-toggle');
            if (subtreeWrapper) subtreeWrapper.classList.add('is-expanded');
            if (toggle) toggle.classList.add('open');
        }

        if (parentId === null) {
            if (!FileTreeModel.expandedFolders[id]) FileTreeModel.expandedFolders[id] = {};
        } else {
            const parentNode = FileTreeModel._findStateNode(parentId);
            if (parentNode) {
                if (!parentNode[id]) parentNode[id] = {};
            }
        }
    },

    close(id, parentId) {
        const li = FileTreeView.fileTreeEl.querySelector(`li[data-id="${id}"]`);
        if (li) {
            const subtreeWrapper = li.querySelector('.subtree-wrapper');
            const toggle = li.querySelector('.folder-toggle');
            
            // Close current node
            if (subtreeWrapper) subtreeWrapper.classList.remove('is-expanded');
            if (toggle) toggle.classList.remove('open');

            // Recursively close all descendants to sync with state removal
            if (subtreeWrapper) {
                subtreeWrapper.querySelectorAll('.is-expanded').forEach(el => el.classList.remove('is-expanded'));
                subtreeWrapper.querySelectorAll('.open').forEach(el => el.classList.remove('open'));
            }
        }

        if (parentId === null) {
            // Root
        } else {
            const parentNode = FileTreeModel._findStateNode(parentId);
            if (parentNode && parentNode[id]) {
                delete parentNode[id];
            }
        }
    },

    compareAndSwitch(targetPathIds, AppState) {
        const toOpen = [];
        const toClose = [];

        // 1. Recursive check starting from Root to identify folders to close
        const rootId = targetPathIds[0];
        if (!rootId) return;

        const traverseAndCompare = (stateNode, currentPathIndex) => {
            const targetId = (currentPathIndex < targetPathIds.length) ? targetPathIds[currentPathIndex] : null;
            
            Object.keys(stateNode).forEach(key => {
                const folderId = Number(key);
                
                if (folderId === targetId) {
                    // Folder is in the target path, keep open and recurse
                    traverseAndCompare(stateNode[folderId], currentPathIndex + 1);
                } else {
                    // Folder is open but not in target path, close it
                    toClose.push(folderId);
                }
            });
        };

        if (FileTreeModel.expandedFolders[rootId]) {
            traverseAndCompare(FileTreeModel.expandedFolders[rootId], 1); 
        }

        // 2. Identify folders to open based on the target path
        let currentLevel = FileTreeModel.expandedFolders;
        for (let i = 0; i < targetPathIds.length; i++) {
            const id = targetPathIds[i];
            if (currentLevel[id]) {
                currentLevel = currentLevel[id];
            } else {
                // Not found in state, needs opening
                const parentId = (i === 0) ? null : targetPathIds[i-1];
                toOpen.push({ id: id, parentId: parentId });
            }
        }
        
        // 3. Execute Actions
        toClose.forEach(id => {
            const folder = AppState.folderMap.get(id);
            if (folder) FileTreeHandler.close(id, folder.parent_id);
        });

        toOpen.forEach(item => {
            FileTreeHandler.open(item.id, item.parentId);
        });
    },

};
