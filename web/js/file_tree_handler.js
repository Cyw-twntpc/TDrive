const FileTreeHandler = {
    fileTreeEl: document.getElementById('file-tree'),

    render(AppState, navigateTo) {
        this.fileTreeEl.innerHTML = '';
        if (!AppState.folderTreeData || AppState.folderTreeData.length === 0) {
            return;
        }

        // --- Smart Expansion Logic ---
        // 1. Create a set of all ancestors of the current folder.
        const ancestors = new Set();
        let currentId = AppState.currentFolderId;
        while (currentId) {
            ancestors.add(currentId);
            const folder = AppState.folderMap.get(currentId);
            currentId = folder ? folder.parent_id : null;
        }
    
        const childrenOf = new Map();
        AppState.folderTreeData.forEach(folder => {
            const parentId = folder.parent_id;
            if (!childrenOf.has(parentId)) {
                childrenOf.set(parentId, []);
            }
            childrenOf.get(parentId).push(folder.id);
        });
    
        const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
    
        if (rootFolder) {
            // 2. Pass the ancestor set down the build process.
            const rootItem = this._buildTreeItem(rootFolder, AppState, navigateTo, childrenOf, ancestors, true);
            const rootUl = document.createElement('ul');
            rootUl.appendChild(rootItem);
            this.fileTreeEl.appendChild(rootUl);
        } else {
            console.error("Could not find root folder to render file tree.");
        }
    },
    
    _buildTreeItem(folder, AppState, navigateTo, childrenOf, ancestors, isRoot = false) {
        const li = document.createElement('li');
        const itemDiv = document.createElement('div');
        itemDiv.className = 'tree-item';
        itemDiv.dataset.id = folder.id;
    
        const hasSubFolders = childrenOf.has(folder.id) && childrenOf.get(folder.id).length > 0;
    
        const toggle = document.createElement('span');
        toggle.className = 'folder-toggle';
        if (hasSubFolders) {
            toggle.innerHTML = '<i class="fas fa-caret-right"></i>';
            toggle.addEventListener('click', (e) => {
                e.stopPropagation();
                const subTree = li.querySelector('ul');
                if (subTree) {
                    subTree.classList.toggle('collapsed');
                    toggle.classList.toggle('open');
                }
            });
        }
    
        const contentDiv = document.createElement('div');
        contentDiv.className = 'folder-content';
        const icon = isRoot ? 'fa-hdd' : 'fa-folder';
        contentDiv.innerHTML = `<i class="fas ${icon} folder-icon"></i><span class="folder-name">${folder.name}</span>`;
        contentDiv.addEventListener('click', () => navigateTo(folder.id));
    
        itemDiv.appendChild(toggle);
        itemDiv.appendChild(contentDiv);
        li.appendChild(itemDiv);
    
        if (hasSubFolders) {
            const ul = document.createElement('ul');
            // 3. If the current folder is NOT an ancestor, it should be collapsed by default.
            if (!ancestors.has(folder.id)) {
                ul.classList.add('collapsed');
            } else {
                toggle.classList.add('open');
            }

            const sortedChildrenIds = childrenOf.get(folder.id).sort((a, b) => {
                const nameA = AppState.folderMap.get(a).name;
                const nameB = AppState.folderMap.get(b).name;
                return nameA.localeCompare(nameB, 'zh-Hans-CN-u-co-pinyin');
            });
    
            sortedChildrenIds.forEach(childId => {
                const childFolder = AppState.folderMap.get(childId);
                const childLi = this._buildTreeItem(childFolder, AppState, navigateTo, childrenOf, ancestors);
                ul.appendChild(childLi);
            });
            li.appendChild(ul);
        }
    
        return li;
    },
    
    updateSelection(AppState) {
        const currentActive = this.fileTreeEl.querySelector('.tree-item.active');
        if (currentActive) {
            currentActive.classList.remove('active');
        }
        
        const newActive = this.fileTreeEl.querySelector(`.tree-item[data-id="${AppState.currentFolderId}"]`);
        if (newActive) {
            newActive.classList.add('active');
        }
    }
};
