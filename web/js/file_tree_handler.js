/**
 * @fileoverview Manages the rendering and state of the collapsible folder tree view.
 */
const FileTreeHandler = {
    fileTreeEl: document.getElementById('file-tree'),

    /**
     * Renders the entire folder tree based on the provided application state.
     * Implements a "smart expansion" feature that automatically expands the tree
     * to reveal the currently active folder.
     * @param {object} AppState - The global application state.
     * @param {Function} navigateTo - Callback function to handle navigation when a folder is clicked.
     */
    render(AppState, navigateTo) {
        this.fileTreeEl.innerHTML = '';
        if (!AppState.folderTreeData || AppState.folderTreeData.length === 0) {
            return;
        }

        // --- Smart Expansion Logic ---
        // 1. Create a set of all ancestors of the current folder for quick lookup.
        const ancestors = new Set();
        let currentId = AppState.currentFolderId;
        while (currentId) {
            ancestors.add(currentId);
            const folder = AppState.folderMap.get(currentId);
            currentId = folder ? folder.parent_id : null;
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
    
        if (rootFolder) {
            // 2. Pass the ancestor set down the recursive build process.
            const rootItem = this._buildTreeItem(rootFolder, AppState, navigateTo, childrenOf, ancestors, true);
            const rootUl = document.createElement('ul');
            rootUl.appendChild(rootItem);
            this.fileTreeEl.appendChild(rootUl);
        } else {
            console.error("Could not find root folder to render file tree.");
        }
    },
    
    /**
     * Recursively builds a single list item (<li>) for a folder in the tree.
     * @param {object} folder - The folder data object.
     * @param {object} AppState - The global application state.
     * @param {Function} navigateTo - The navigation callback.
     * @param {Map} childrenOf - The map of parent-to-children IDs.
     * @param {Set} ancestors - The set of ancestor IDs for smart expansion.
     * @param {boolean} [isRoot=false] - True if this is the root node.
     * @returns {HTMLLIElement} The created list item element.
     * @private
     */
    _buildTreeItem(folder, AppState, navigateTo, childrenOf, ancestors, isRoot = false) {
        const li = document.createElement('li');
        const itemDiv = document.createElement('div');
        itemDiv.className = 'tree-item';
        itemDiv.dataset.id = folder.id;
    
        const hasSubFolders = childrenOf.has(folder.id) && childrenOf.get(folder.id).length > 0;
    
        // Create the expand/collapse toggle arrow.
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
        const icon = isRoot ? 'fa-hdd' : 'fa-folder'; // Root gets a different icon.
        contentDiv.innerHTML = `<i class="fas ${icon} folder-icon"></i><span class="folder-name">${folder.name}</span>`;
        contentDiv.addEventListener('click', () => navigateTo(folder.id));
    
        itemDiv.appendChild(toggle);
        itemDiv.appendChild(contentDiv);
        li.appendChild(itemDiv);
    
        if (hasSubFolders) {
            const ul = document.createElement('ul');
            // 3. If the current folder is an ancestor of the active folder, expand it by default.
            if (ancestors.has(folder.id)) {
                toggle.classList.add('open');
            } else {
                ul.classList.add('collapsed');
            }

            const sortedChildrenIds = childrenOf.get(folder.id).sort((a, b) => {
                const nameA = AppState.folderMap.get(a).name;
                const nameB = AppState.folderMap.get(b).name;
                // Use localeCompare for natural sorting, including pinyin for Chinese.
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
    
    /**
     * Updates the visual selection in the tree to highlight the current folder.
     * @param {object} AppState - The global application state.
     */
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
