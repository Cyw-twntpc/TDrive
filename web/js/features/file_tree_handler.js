/**
 * @fileoverview Manages the rendering and state of the collapsible folder tree view.
 */
const FileTreeHandler = {
    fileTreeEl: document.getElementById('file-tree'),
    
    /**
     * Stores the state of expanded folders using a nested object structure.
     * Format: { "RootID": { "L1_ID": { ... } } }
     */
    expandedFolders: {},

    /**
     * Renders the entire folder tree based on the provided application state.
     * This should only be called on initial load or when the folder structure changes (e.g., add/delete folder).
     * Navigation updates should use compareAndSwitch() instead.
     * 
     * @param {object} AppState - The global application state.
     * @param {Function} navigateTo - Callback function to handle navigation when a folder is clicked.
     */
    render(AppState, navigateTo) {
        // 1. Capture currently expanded folder IDs to maintain state across renders
        const previouslyExpanded = new Set();
        this.fileTreeEl.querySelectorAll('.subtree-wrapper.is-expanded').forEach(el => {
            const li = el.closest('li');
            if (li && li.dataset.id) {
                previouslyExpanded.add(Number(li.dataset.id));
            }
        });

        this.fileTreeEl.innerHTML = '';
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
            if (!this.expandedFolders[rootFolder.id]) {
                this.expandedFolders[rootFolder.id] = {};
            }

            // 2. Pass the ancestor set down the recursive build process.
            const rootItem = this._buildTreeItem(rootFolder, AppState, navigateTo, childrenOf, previouslyExpanded, pendingExpansions, pendingCollapses, true, 0);
            const rootUl = document.createElement('ul');
            rootUl.appendChild(rootItem);
            this.fileTreeEl.appendChild(rootUl);
        } else {
            console.error("Could not find root folder to render file tree.");
        }

        // 3. Trigger animations for newly expanded and collapsed folders
        if (pendingExpansions.length > 0 || pendingCollapses.length > 0) {
            // Force reflow
            void this.fileTreeEl.offsetHeight; 
            
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
    
    /**
     * Recursively builds a single list item (<li>) for a folder in the tree.
     * @param {object} folder - The folder data object.
     * @param {object} AppState - The global application state.
     * @param {Function} navigateTo - The navigation callback.
     * @param {Map} childrenOf - The map of parent-to-children IDs.
     * @param {Set} previouslyExpanded - IDs of folders that were expanded before re-render.
     * @param {Array} pendingExpansions - Array to collect elements needing expansion animation.
     * @param {Array} pendingCollapses - Array to collect elements needing collapse animation.
     * @param {boolean} [isRoot=false] - True if this is the root node.
     * @param {number} [level=0] - The nesting level for dynamic indentation.
     * @returns {HTMLLIElement} The created list item element.
     * @private
     */
    _buildTreeItem(folder, AppState, navigateTo, childrenOf, previouslyExpanded, pendingExpansions, pendingCollapses, isRoot = false, level = 0) {
        level = Number(level) || 0;
        const li = document.createElement('li');
        li.dataset.id = folder.id;
        
        const itemDiv = document.createElement('div');
        itemDiv.className = 'tree-item';
        itemDiv.dataset.id = folder.id;
        
        // Define hasSubFolders early for use in event listeners
        const hasSubFolders = childrenOf.has(folder.id) && childrenOf.get(folder.id).length > 0;

        // 1. Indentation Spacer
        const indentSpacer = document.createElement('div');
        indentSpacer.className = 'tree-indent-spacer';
        const indentWidth = 8 + (level * 18);
        indentSpacer.style.width = `${indentWidth}px`;
        indentSpacer.style.minWidth = `${indentWidth}px`;
        
        // 2. Wrapper for Content (Interactive Area: Icon + Name + Right Space)
        const wrapperDiv = document.createElement('div');
        wrapperDiv.className = 'tree-item-wrapper';

        // --- Navigation Click (Wrapper only) ---
        wrapperDiv.addEventListener('click', (e) => {
            navigateTo(folder.id);
        });

        // Toggle Logic
        let toggle;
        if (hasSubFolders) {
            toggle = document.createElement('span');
            toggle.className = 'folder-toggle';
            toggle.innerHTML = '<i class="fas fa-caret-right"></i>';
            
            toggle.addEventListener('click', (e) => {
                e.stopPropagation(); 
                // Toggle logic now uses the centralized open/close functions
                const parentId = folder.parent_id;
                // Check if currently open (based on DOM state)
                const subTreeWrapper = li.querySelector('.subtree-wrapper');
                if (subTreeWrapper && subTreeWrapper.classList.contains('is-expanded')) {
                    this.close(folder.id, parentId);
                } else {
                    this.open(folder.id, parentId);
                }
            });
        } else {
            toggle = document.createElement('span');
            toggle.className = 'folder-toggle-placeholder';
        }

        if (hasSubFolders) {
            wrapperDiv.addEventListener('dblclick', (e) => {
                e.preventDefault(); 
                const parentId = folder.parent_id;
                const subTreeWrapper = li.querySelector('.subtree-wrapper');
                if (subTreeWrapper && subTreeWrapper.classList.contains('is-expanded')) {
                    this.close(folder.id, parentId);
                } else {
                    this.open(folder.id, parentId);
                }
            });
        }

        // Drag & Drop Logic
        wrapperDiv.draggable = true;
        wrapperDiv.addEventListener('dragstart', (e) => {
            AppState.isDragging = true;
            AppState.draggedItems = [{
                id: folder.id,
                type: 'folder',
                name: folder.name
            }];
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', JSON.stringify(AppState.draggedItems));
            
            const ghost = document.createElement('div');
            ghost.id = 'drag-ghost';
            ghost.innerHTML = `<i class="fas fa-folder"></i> <span>${folder.name}</span>`;
            document.body.appendChild(ghost);
            e.dataTransfer.setDragImage(ghost, 0, 0);
            setTimeout(() => { document.body.removeChild(ghost); }, 0);
            itemDiv.classList.add('dragging');
        });

        wrapperDiv.addEventListener('dragend', () => {
            AppState.isDragging = false;
            AppState.draggedItems = [];
            itemDiv.classList.remove('dragging');
        });

        // Drop Target Logic
        itemDiv.addEventListener('dragover', (e) => {
            if (!AppState.isDragging) return;
            const isValid = ActionHandler.isValidMove(AppState.draggedItems, folder.id);
            if (!isValid) {
                e.dataTransfer.dropEffect = 'none';
                return; 
            }
            e.preventDefault(); e.stopPropagation(); 
            e.dataTransfer.dropEffect = 'move';
            itemDiv.classList.add('drop-target'); 

            // Auto-Expand
            if (hasSubFolders && toggle && toggle.classList.contains('folder-toggle') && !toggle.classList.contains('open')) {
                if (!AppState.dragHoverTimer) {
                    AppState.dragHoverTimer = setTimeout(() => {
                        this.open(folder.id, folder.parent_id);
                        AppState.dragHoverTimer = null;
                    }, 800); 
                }
            }
        });

        itemDiv.addEventListener('dragleave', () => {
            itemDiv.classList.remove('drop-target');
            if (AppState.dragHoverTimer) {
                clearTimeout(AppState.dragHoverTimer);
                AppState.dragHoverTimer = null;
            }
        });

        itemDiv.addEventListener('drop', (e) => {
            e.preventDefault();
            itemDiv.classList.remove('drop-target');
            if (AppState.dragHoverTimer) {
                clearTimeout(AppState.dragHoverTimer);
                AppState.dragHoverTimer = null;
            }
            if (AppState.isDragging) {
                ActionHandler.executeMove(AppState.draggedItems, folder.id);
            }
        });
    
        // Content
        const contentDiv = document.createElement('div');
        contentDiv.className = 'folder-content';
        const icon = isRoot ? 'fa-hdd' : 'fa-folder'; 
        contentDiv.innerHTML = `<i class="fas ${icon} folder-icon"></i><span class="folder-name">${folder.name}</span>`;
        
        contentDiv.addEventListener('mouseenter', (e) => {
            const nameSpan = contentDiv.querySelector('.folder-name');
            if (nameSpan.scrollWidth > nameSpan.clientWidth) {
                this._showTooltip(contentDiv, folder.name, isRoot, folder.id === AppState.currentFolderId);
            }
        });
        
        contentDiv.addEventListener('mouseleave', () => {
            this._hideTooltip();
        });
    
        wrapperDiv.appendChild(contentDiv);
        itemDiv.appendChild(indentSpacer);
        itemDiv.appendChild(toggle);
        itemDiv.appendChild(wrapperDiv);
        li.appendChild(itemDiv);
    
        // Recursively build children
        if (hasSubFolders) {
            const subtreeWrapper = document.createElement('div');
            subtreeWrapper.className = 'subtree-wrapper';
            const ul = document.createElement('ul');
            
            // Initial Expansion Check: Verify if this folder is in the expandedFolders state
            const isExpanded = this._isFolderExpanded(folder.id);
            const wasExpanded = previouslyExpanded.has(folder.id);

            // Determine if we should show it expanded
            // For render, we respect the state (isExpanded)
            // But if we want animation, we use pending lists.
            // Since this is 'render' (re-draw), we only animate if it's a NEW expansion/collapse that happened between renders?
            // Actually, render is called on refreshAll.
            // compareAndSwitch handles dynamic updates.
            // So here we just set the static state.
            
            if (isExpanded || isRoot) {
                subtreeWrapper.classList.add('is-expanded');
                toggle.classList.add('open');
            }

            const sortedChildrenIds = childrenOf.get(folder.id).sort((a, b) => {
                const nameA = AppState.folderMap.get(a).name;
                const nameB = AppState.folderMap.get(b).name;
                return nameA.localeCompare(nameB, 'zh-Hans-CN-u-co-pinyin');
            });
    
            sortedChildrenIds.forEach(childId => {
                const childFolder = AppState.folderMap.get(childId);
                const childLi = this._buildTreeItem(childFolder, AppState, navigateTo, childrenOf, previouslyExpanded, pendingExpansions, pendingCollapses, false, level + 1);
                ul.appendChild(childLi);
            });
            
            subtreeWrapper.appendChild(ul);
            li.appendChild(subtreeWrapper);
        }
    
        return li;
    },

    /**
     * Opens a folder: Updates DOM and State.
     * @param {number} id - The folder ID to open.
     * @param {number|null} parentId - The parent folder ID.
     */
    open(id, parentId) {
        const li = this.fileTreeEl.querySelector(`li[data-id="${id}"]`);
        if (li) {
            const subtreeWrapper = li.querySelector('.subtree-wrapper');
            const toggle = li.querySelector('.folder-toggle');
            if (subtreeWrapper) subtreeWrapper.classList.add('is-expanded');
            if (toggle) toggle.classList.add('open');
        }

        if (parentId === null) {
            if (!this.expandedFolders[id]) this.expandedFolders[id] = {};
        } else {
            const parentNode = this._findStateNode(parentId);
            if (parentNode) {
                if (!parentNode[id]) parentNode[id] = {};
            }
        }
    },

    /**
     * Closes a folder: Updates DOM and State.
     * @param {number} id - The folder ID to close.
     * @param {number|null} parentId - The parent folder ID.
     */
    close(id, parentId) {
        const li = this.fileTreeEl.querySelector(`li[data-id="${id}"]`);
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
            const parentNode = this._findStateNode(parentId);
            if (parentNode && parentNode[id]) {
                delete parentNode[id];
            }
        }
    },

    /**
     * Syncs the tree state with the target path (Smart Expansion).
     * @param {Array<number>} targetPathIds - The list of ancestor IDs (ordered from root to leaf).
     * @param {object} AppState - To get parentId map.
     */
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

        if (this.expandedFolders[rootId]) {
            traverseAndCompare(this.expandedFolders[rootId], 1); 
        }

        // 2. Identify folders to open based on the target path
        let currentLevel = this.expandedFolders;
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
            if (folder) this.close(id, folder.parent_id);
        });

        toOpen.forEach(item => {
            this.open(item.id, item.parentId);
        });
    },

    // --- Helpers ---

    _findStateNode(id) {
        if (this.expandedFolders[id]) return this.expandedFolders[id];
        
        const queue = Object.values(this.expandedFolders);
        while (queue.length > 0) {
            const current = queue.shift();
            if (current[id]) return current[id];
            Object.values(current).forEach(child => queue.push(child));
        }
        return null;
    },

    _isFolderExpanded(id) {
        if (this.expandedFolders[id]) return true;
        
        const queue = Object.values(this.expandedFolders);
        while (queue.length > 0) {
            const current = queue.shift();
            if (current[id]) return true;
            Object.values(current).forEach(child => queue.push(child));
        }
        return false;
    },

    _showTooltip(targetEl, text, isRoot, isActive) {
        const tooltip = document.getElementById('tree-tooltip');
        if (!tooltip) return;
        const icon = isRoot ? 'fa-hdd' : 'fa-folder';
        tooltip.innerHTML = `<i class="fas ${icon}"></i><span>${text}</span>`;
        isActive ? tooltip.classList.add('active-folder') : tooltip.classList.remove('active-folder');
        const rect = targetEl.getBoundingClientRect();
        tooltip.style.left = `${rect.left}px`;
        tooltip.style.top = `${rect.top}px`;
        tooltip.style.minWidth = `${rect.width}px`; 
        tooltip.style.display = 'flex';
    },

    _hideTooltip() {
        const tooltip = document.getElementById('tree-tooltip');
        if (tooltip) tooltip.style.display = 'none';
    },

    updateSelection(AppState) {
        const currentActive = this.fileTreeEl.querySelector('.tree-item.active');
        if (currentActive) currentActive.classList.remove('active');
        const newActive = this.fileTreeEl.querySelector(`.tree-item[data-id="${AppState.currentFolderId}"]`);
        if (newActive) newActive.classList.add('active');
    }
};
