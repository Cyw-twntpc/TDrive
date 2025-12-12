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
        
        // Start from the parent of the current folder to avoid expanding the current folder itself
        const currentFolder = AppState.folderMap.get(currentId);
        if (currentFolder) {
            currentId = currentFolder.parent_id;
        } else {
            currentId = null;
        }

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
            const rootItem = this._buildTreeItem(rootFolder, AppState, navigateTo, childrenOf, ancestors, true, 0);
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
     * @param {number} [level=0] - The nesting level for dynamic indentation.
     * @returns {HTMLLIElement} The created list item element.
     * @private
     */
    _buildTreeItem(folder, AppState, navigateTo, childrenOf, ancestors, isRoot = false, level = 0) {
        const li = document.createElement('li');
        const itemDiv = document.createElement('div');
        itemDiv.className = 'tree-item';
        itemDiv.dataset.id = folder.id;
        
        // Define hasSubFolders early for use in event listeners
        const hasSubFolders = childrenOf.has(folder.id) && childrenOf.get(folder.id).length > 0;

        // 1. Indentation Spacer (Non-interactive visual padding)
        const indentSpacer = document.createElement('div');
        indentSpacer.className = 'tree-indent-spacer';
        // Calculate indentation: Base padding (8px) + Level * Indent per level (18px)
        const indentWidth = 8 + (level * 18);
        indentSpacer.style.width = `${indentWidth}px`;
        
        // 2. Wrapper for Content (Interactive Area: Icon + Name + Right Space)
        const wrapperDiv = document.createElement('div');
        wrapperDiv.className = 'tree-item-wrapper';

        // --- Navigation Click (Wrapper only) ---
        wrapperDiv.addEventListener('click', (e) => {
            navigateTo(folder.id);
        });

        // --- Create Toggle Action Helper Early ---
        let toggle;
        let toggleAction = () => {}; 

        if (hasSubFolders) {
            toggle = document.createElement('span');
            toggle.className = 'folder-toggle';
            toggle.innerHTML = '<i class="fas fa-caret-right"></i>';
            
            toggleAction = () => {
                const subTree = li.querySelector('ul');
                if (subTree) {
                    subTree.classList.toggle('collapsed');
                    toggle.classList.toggle('open');
                }
            };

            toggle.addEventListener('click', (e) => {
                e.stopPropagation(); // Prevent navigation when toggling
                toggleAction();
            });
        } else {
            toggle = document.createElement('span');
            toggle.className = 'folder-toggle-placeholder';
        }

        // --- Double Click to Toggle (Wrapper only) ---
        if (hasSubFolders) {
            wrapperDiv.addEventListener('dblclick', (e) => {
                e.preventDefault(); // Prevent text selection
                toggleAction();
            });
        }

        // --- Drag Start Logic (Wrapper only) ---
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
            
            // --- Custom Drag Ghost ---
            const ghost = document.createElement('div');
            ghost.id = 'drag-ghost';
            ghost.innerHTML = `<i class="fas fa-folder"></i> <span>${folder.name}</span>`;
            
            document.body.appendChild(ghost);
            
            e.dataTransfer.setDragImage(ghost, 0, 0);
            
            setTimeout(() => {
                document.body.removeChild(ghost);
            }, 0);

            itemDiv.classList.add('dragging');
        });

        wrapperDiv.addEventListener('dragend', () => {
            AppState.isDragging = false;
            AppState.draggedItems = [];
            itemDiv.classList.remove('dragging');
        });

        // --- Drop Target & Auto-Expand Logic (Bound to the full row) ---
        itemDiv.addEventListener('dragover', (e) => {
            if (!AppState.isDragging) return;

            const isValid = ActionHandler.isValidMove(AppState.draggedItems, folder.id);

            if (!isValid) {
                e.dataTransfer.dropEffect = 'none';
                return; 
            }

            e.preventDefault(); 
            e.stopPropagation(); 
            e.dataTransfer.dropEffect = 'move';
            itemDiv.classList.add('drop-target'); 

            // Auto-Expand Logic
            if (hasSubFolders && toggle && toggle.classList.contains('folder-toggle') && !toggle.classList.contains('open')) {
                if (!AppState.dragHoverTimer) {
                    AppState.dragHoverTimer = setTimeout(() => {
                        toggleAction(); // Use the helper
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
    
        const contentDiv = document.createElement('div');
        contentDiv.className = 'folder-content';
        const icon = isRoot ? 'fa-hdd' : 'fa-folder'; 
        contentDiv.innerHTML = `<i class="fas ${icon} folder-icon"></i><span class="folder-name">${folder.name}</span>`;
        
        // --- Custom Tooltip Logic ---
        contentDiv.addEventListener('mouseenter', (e) => {
            const nameSpan = contentDiv.querySelector('.folder-name');
            if (nameSpan.scrollWidth > nameSpan.clientWidth) {
                this._showTooltip(contentDiv, folder.name, isRoot, folder.id === AppState.currentFolderId);
            }
        });
        
        contentDiv.addEventListener('mouseleave', () => {
            this._hideTooltip();
        });
    
        // Assemble Structure
        wrapperDiv.appendChild(contentDiv);
        
        itemDiv.appendChild(indentSpacer);
        itemDiv.appendChild(toggle);
        itemDiv.appendChild(wrapperDiv);
        
        li.appendChild(itemDiv);
    
        if (hasSubFolders) {
            const ul = document.createElement('ul');
            // 3. If the current folder is an ancestor of the active folder OR it is the root, expand it.
            if (ancestors.has(folder.id) || isRoot) {
                toggle.classList.add('open');
            } else {
                ul.classList.add('collapsed');
            }

            const sortedChildrenIds = childrenOf.get(folder.id).sort((a, b) => {
                const nameA = AppState.folderMap.get(a).name;
                const nameB = AppState.folderMap.get(b).name;
                return nameA.localeCompare(nameB, 'zh-Hans-CN-u-co-pinyin');
            });
    
            sortedChildrenIds.forEach(childId => {
                const childFolder = AppState.folderMap.get(childId);
                const childLi = this._buildTreeItem(childFolder, AppState, navigateTo, childrenOf, ancestors, false, level + 1);
                ul.appendChild(childLi);
            });
            li.appendChild(ul);
        }
    
        return li;
    },
    
    /**
     * Shows the custom tooltip for a truncated folder item.
     * @param {HTMLElement} targetEl - The folder-content element being hovered.
     * @param {string} text - The full folder name.
     * @param {boolean} isRoot - Whether the folder is the root (affects icon).
     * @param {boolean} isActive - Whether the folder is currently active (affects styling).
     * @private
     */
    _showTooltip(targetEl, text, isRoot, isActive) {
        const tooltip = document.getElementById('tree-tooltip');
        if (!tooltip) return;

        const icon = isRoot ? 'fa-hdd' : 'fa-folder';
        tooltip.innerHTML = `<i class="fas ${icon}"></i><span>${text}</span>`;
        
        if (isActive) {
            tooltip.classList.add('active-folder');
        } else {
            tooltip.classList.remove('active-folder');
        }

        const rect = targetEl.getBoundingClientRect();
        
        // Position the tooltip exactly over the target element
        tooltip.style.left = `${rect.left}px`;
        tooltip.style.top = `${rect.top}px`;
        // Ensure minimum width matches the target, but allow it to grow
        tooltip.style.minWidth = `${rect.width}px`; 
        
        tooltip.style.display = 'flex';
    },

    /**
     * Hides the custom tooltip.
     * @private
     */
    _hideTooltip() {
        const tooltip = document.getElementById('tree-tooltip');
        if (tooltip) {
            tooltip.style.display = 'none';
        }
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