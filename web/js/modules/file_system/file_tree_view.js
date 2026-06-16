const FileTreeView = {
    fileTreeEl: document.getElementById('file-tree'),

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
                    FileTreeHandler.close(folder.id, parentId);
                } else {
                    FileTreeHandler.open(folder.id, parentId);
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
                    FileTreeHandler.close(folder.id, parentId);
                } else {
                    FileTreeHandler.open(folder.id, parentId);
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
            ghost.innerHTML = `<i class="fas fa-folder"></i> <span>${UIManager.escapeHtml(folder.name)}</span>`;
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
            const isValid = ActionController.isValidMove(AppState.draggedItems, folder.id);
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
                        FileTreeHandler.open(folder.id, folder.parent_id);
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
                ActionController.executeMove(AppState.draggedItems, folder.id);
            }
        });
    
        // Content
        const contentDiv = document.createElement('div');
        contentDiv.className = 'folder-content';
        const icon = isRoot ? 'fa-hdd' : 'fa-folder'; 
        contentDiv.innerHTML = `<i class="fas ${icon} folder-icon"></i><span class="folder-name">${UIManager.escapeHtml(folder.name)}</span>`;
        
        contentDiv.addEventListener('mouseenter', (e) => {
            const nameSpan = contentDiv.querySelector('.folder-name');
            if (nameSpan.scrollWidth > nameSpan.clientWidth) {
                FileTreeView._showTooltip(contentDiv, folder.name, isRoot, folder.id === AppState.currentFolderId);
            }
        });
        
        contentDiv.addEventListener('mouseleave', () => {
            FileTreeView._hideTooltip();
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
            const isExpanded = FileTreeModel._isFolderExpanded(folder.id);
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
                const childLi = FileTreeView._buildTreeItem(childFolder, AppState, navigateTo, childrenOf, previouslyExpanded, pendingExpansions, pendingCollapses, false, level + 1);
                ul.appendChild(childLi);
            });
            
            subtreeWrapper.appendChild(ul);
            li.appendChild(subtreeWrapper);
        }
    
        return li;
    },

    updateSelection(AppState) {
        const currentActive = FileTreeView.fileTreeEl.querySelector('.tree-item.active');
        if (currentActive) currentActive.classList.remove('active');
        const newActive = FileTreeView.fileTreeEl.querySelector(`.tree-item[data-id="${AppState.currentFolderId}"]`);
        if (newActive) newActive.classList.add('active');
    },

    _showTooltip(targetEl, text, isRoot, isActive) {
        const tooltip = document.getElementById('tree-tooltip');
        if (!tooltip) return;
        const icon = isRoot ? 'fa-hdd' : 'fa-folder';
        tooltip.innerHTML = `<i class="fas ${icon}"></i><span>${UIManager.escapeHtml(text)}</span>`;
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

};
